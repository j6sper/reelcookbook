"""Recipes: saved from Instagram/TikTok posts (see social.py), from a website, or typed in.
Each has tags and per-serving macros (filled in by Claude, or by hand) for filtering."""
import asyncio
import json
import logging
import re
import time
from urllib.parse import quote, urlencode, urlsplit

import httpx
from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, Response

from . import auth
from .config import DATA_DIR
from .db import get_db
from .templating import page

log = logging.getLogger(__name__)

MIGRATIONS = [
    ("recipes-001", """
        CREATE TABLE recipes (
            id INTEGER PRIMARY KEY,
            title TEXT NOT NULL,
            url TEXT NOT NULL DEFAULT '',
            site TEXT NOT NULL DEFAULT '',
            image_file TEXT,
            ingredients TEXT NOT NULL DEFAULT '[]',   -- JSON list of lines
            steps TEXT NOT NULL DEFAULT '[]',         -- JSON list of lines
            total_minutes INTEGER,
            yields TEXT NOT NULL DEFAULT '',
            notes TEXT NOT NULL DEFAULT '',
            draft INTEGER NOT NULL DEFAULT 0,         -- imported, not yet checked
            cuisine TEXT NOT NULL DEFAULT '',
            proteins TEXT NOT NULL DEFAULT '[]',      -- JSON list, from PROTEINS
            tags TEXT NOT NULL DEFAULT '[]',          -- JSON list, from TAGS
            servings INTEGER,
            kcal REAL,                                -- per serving, like the macros
            protein_g REAL,
            carbs_g REAL,
            fat_g REAL,
            macros_source TEXT NOT NULL DEFAULT '',   -- from MACRO_SOURCES
            tagged_at REAL,                           -- when tags were last set (empty: Claude tags it)
            created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
            created_at REAL NOT NULL
        );
    """),
]

# Tags: Claude fills them in for every new recipe; anyone can change them on the edit page.
CUISINES = ["Italian", "Asian", "Mexican", "Indian", "Middle Eastern", "Mediterranean", "American", "Other"]
PROTEINS = ["Chicken", "Beef", "Pork", "Lamb", "Turkey", "Fish", "Seafood", "Eggs", "Vegetarian"]
TAGS = ["High protein", "Meal prep", "Quick", "Low calorie", "Breakfast", "Snack", "Dessert"]
TAG_GUIDE = ("High protein: at least 30 g protein per serving, or clearly made for fitness / hitting macros. "
             "Quick: ready in 30 minutes or less. Low calorie: under 500 kcal per serving (mains). "
             "Meal prep: made in a batch to portion out.")
MACRO_SOURCES = {"recipe": "From the recipe", "estimated": "Estimated by Claude", "entered": "Entered by hand"}
SORTS = {"new": "Newest", "protein": "Most protein", "kcal": "Fewest calories", "ratio": "Most protein per calorie",
         "name": "A to Z"}

IMAGE_DIR = DATA_DIR / "recipe-images"
SOCIAL_DIR = DATA_DIR / "social"     # stills kept from Instagram/TikTok imports
PHOTO_MAX_PX = 1200
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_UPLOAD_BYTES = 25 * 1024 * 1024    # photos uploaded from a phone
IMAGE_TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif"}
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
                  "(KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Accept-Language": "en;q=0.9",
}
# Things most kitchens already have: shown unticked in the ingredient checklist
STAPLES = re.compile(
    r"to taste|salt and (black )?pepper|"
    r"^[\W\d/½¼¾⅓]*(?:(?:cups?|tbsp|tsp|ml|l|litres?|pinch(?: of)?|a pinch of)\s+)?"
    r"(?:cold |warm |hot |boiling |tap |iced |sea |kosher |table |fine |flaked |cracked |freshly ground |ground )*"
    r"(?:water|ice|salt|black pepper|white pepper|pepper)\b(?!\s*(?:corns?|flakes))", re.I)


def clean_lines(text):
    return [line.strip() for line in text.splitlines() if line.strip()]


# --- Reading a recipe from a website ---

def scrape(html, url):
    """Pull the recipe out of a web page. Raises ValueError if there isn't one."""
    from recipe_scrapers import scrape_html

    try:
        scraper = scrape_html(html, org_url=url, supported_only=False)
    except Exception as e:   # the library's own "no recipe here" errors
        raise ValueError("no recipe found") from e

    def get(name, default=None):
        try:
            value = getattr(scraper, name)()
            return value if value else default
        except Exception:
            return default

    title, ingredients = get("title"), get("ingredients", [])
    if not title or not ingredients:
        raise ValueError("no recipe found")
    return {
        "title": title.strip()[:200],
        "ingredients": [i.strip() for i in ingredients if i and i.strip()],
        "steps": [s.strip() for s in get("instructions_list", []) if s and s.strip()],
        "image": get("image"),
        "total_minutes": get("total_time") if isinstance(get("total_time"), int) else None,
        "yields": str(get("yields", "")),
        "site": (urlsplit(url).hostname or "").removeprefix("www."),
    }


async def fetch_page(client, url):
    response = await client.get(url, headers=BROWSER_HEADERS)
    response.raise_for_status()
    return response.text


async def save_image(client, recipe_id, image_url):
    """Keep our own copy of the photo, so it still shows if the website changes."""
    try:
        response = await client.get(image_url, headers=BROWSER_HEADERS)
        kind = response.headers.get("content-type", "").split(";")[0].strip()
        if response.status_code != 200 or kind not in IMAGE_TYPES or len(response.content) > MAX_IMAGE_BYTES:
            return None
        IMAGE_DIR.mkdir(parents=True, exist_ok=True)
        name = f"{recipe_id}.{IMAGE_TYPES[kind]}"
        (IMAGE_DIR / name).write_bytes(response.content)
        return name
    except httpx.HTTPError:
        return None


def store_photo(recipe_id, data):
    """Save a photo (uploaded, or a frame from the video) as the recipe's picture: turned the right way up,
    shrunk to PHOTO_MAX_PX and saved under a new name so phones don't keep showing the old one."""
    import io
    from PIL import Image, ImageOps
    with Image.open(io.BytesIO(data)) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        im.thumbnail((PHOTO_MAX_PX, PHOTO_MAX_PX))
        out = io.BytesIO()
        im.save(out, "JPEG", quality=85)
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    name = f"{recipe_id}-{int(time.time())}.jpg"
    (IMAGE_DIR / name).write_bytes(out.getvalue())
    old = get_recipe(recipe_id)["image_file"]
    with get_db() as db:
        db.execute("UPDATE recipes SET image_file = ? WHERE id = ?", (name, recipe_id))
    if old and old != name:
        (IMAGE_DIR / old).unlink(missing_ok=True)
    return name


def photo_choices(recipe_id):
    """Stills from the Instagram/TikTok post a recipe came from (cover, slides, video frames), to pick a photo from."""
    with get_db() as db:
        row = db.execute("SELECT id, frames FROM social_imports WHERE recipe_id = ?", (recipe_id,)).fetchone()
    if row is None:
        return None, []
    folder = SOCIAL_DIR / str(row["id"])
    names = (["cover.jpg"] if (folder / "cover.jpg").exists() else []) + json.loads(row["frames"] or "[]")
    return folder, [n for n in names if (folder / n).exists()]


def get_recipe(recipe_id):
    with get_db() as db:
        row = db.execute("SELECT * FROM recipes WHERE id = ?", (recipe_id,)).fetchone()
    if row is None:
        return None
    r = dict(row)
    r["ingredients"], r["steps"] = json.loads(r["ingredients"]), json.loads(r["steps"])
    r["proteins"], r["tags"] = json.loads(r["proteins"]), json.loads(r["tags"])
    return r


def pick(values, allowed):
    """Keep only known tag values, matched without caring about case, in the list's order."""
    wanted = {str(v).strip().lower() for v in values}
    return [a for a in allowed if a.lower() in wanted]


def number(value):
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return round(n, 1) if 0 <= n < 100000 else None


def set_details(recipe_id, cuisine="", proteins=(), tags=(), servings=None, kcal=None, protein_g=None,
                carbs_g=None, fat_g=None, macros_source=""):
    """Save a recipe's tags and per-serving macros (from Claude or the edit page)."""
    cuisine = next((c for c in CUISINES if c.lower() == (cuisine or "").strip().lower()), "")
    macros = [number(v) for v in (kcal, protein_g, carbs_g, fat_g)]
    source = macros_source if macros_source in MACRO_SOURCES and any(m is not None for m in macros) else ""
    servings = int(number(servings) or 0) or None
    with get_db() as db:
        db.execute("""UPDATE recipes SET cuisine = ?, proteins = ?, tags = ?, servings = ?, kcal = ?, protein_g = ?,
                      carbs_g = ?, fat_g = ?, macros_source = ?, tagged_at = ? WHERE id = ?""",
                   (cuisine, json.dumps(pick(proteins, PROTEINS)), json.dumps(pick(tags, TAGS)), servings, *macros,
                    source, time.time(), recipe_id))


def matches(r, f):
    """Does a recipe (a dict from get_recipe) pass the Recipes page filters?"""
    text = f["q"].lower()
    if text and text not in r["title"].lower() and text not in " ".join(r["ingredients"]).lower():
        return False
    if f["cuisine"] and r["cuisine"] != f["cuisine"]:
        return False
    if f["protein"] and f["protein"] not in r["proteins"]:
        return False
    if f["tag"] and f["tag"] not in r["tags"]:
        return False
    if f["min_protein"] is not None and (r["protein_g"] is None or r["protein_g"] < f["min_protein"]):
        return False
    if f["max_kcal"] is not None and (r["kcal"] is None or r["kcal"] > f["max_kcal"]):
        return False
    return True


def sort_key(sort):
    big = float("inf")
    return {
        "protein": lambda r: -(r["protein_g"] or 0),
        "kcal": lambda r: r["kcal"] if r["kcal"] is not None else big,
        "ratio": lambda r: -((r["protein_g"] or 0) / r["kcal"]) if r["kcal"] else big,
        "name": lambda r: r["title"].lower(),
    }.get(sort, lambda r: -r["created_at"])


# --- Pages ---

router = APIRouter()


@router.get("/")
def home():
    return RedirectResponse("/recipes", status_code=303)


@router.get("/recipes")
def recipes_page(request: Request, user=Depends(auth.require_user), q: str = "", cuisine: str = "", protein: str = "",
                 tag: str = "", min_protein: str = "", max_kcal: str = "", sort: str = "new"):
    f = {"q": q.strip(), "cuisine": cuisine if cuisine in CUISINES else "", "protein": protein if protein in PROTEINS else "",
         "tag": tag if tag in TAGS else "", "min_protein": number(min_protein) if min_protein.strip() else None,
         "max_kcal": number(max_kcal) if max_kcal.strip() else None, "sort": sort if sort in SORTS else "new"}
    with get_db() as db:
        ids = [row["id"] for row in db.execute("SELECT id FROM recipes")]
    everything = [get_recipe(i) for i in ids]
    shown = sorted((r for r in everything if matches(r, f)), key=sort_key(f["sort"]))
    filtering = any(f[k] not in ("", None) for k in ("q", "cuisine", "protein", "tag", "min_protein", "max_kcal"))
    from . import writer

    def link(**changes):
        """This page's address with some filters changed (for the chips)."""
        merged = {**f, **changes}
        return "/recipes?" + urlencode({k: (f"{v:g}" if isinstance(v, float) else v) for k, v in merged.items()
                                        if v not in ("", None) and not (k == "sort" and v == "new")})

    return page(request, "recipes.html", user, active="recipes", recipes=shown, f=f, filtering=filtering, link=link,
                total=len(everything), cuisines=CUISINES, proteins=PROTEINS, tags=TAGS, sorts=SORTS,
                untagged=sum(1 for r in everything if r["tagged_at"] is None), writer_on=writer.enabled())


@router.get("/recipes/new")
def new_form(request: Request, user=Depends(auth.require_user), url: str = ""):
    return page(request, "recipe_new.html", user, active="recipes", url=url)


@router.post("/recipes/import")
async def import_recipe(request: Request, user=Depends(auth.require_user), url: str = Form("")):
    url = url.strip()
    if not url.startswith(("https://", "http://")):
        return page(request, "recipe_new.html", user, active="recipes", url=url,
                    error="Please paste the full web address, starting with https://")
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            html = await fetch_page(client, url)
            recipe = await asyncio.to_thread(scrape, html, url)
            with get_db() as db:
                recipe_id = db.execute(
                    """INSERT INTO recipes (title, url, site, ingredients, steps, total_minutes, yields, created_by, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (recipe["title"], url, recipe["site"], json.dumps(recipe["ingredients"]), json.dumps(recipe["steps"]),
                     recipe["total_minutes"], recipe["yields"], user["id"], time.time())).lastrowid
            if recipe["image"]:
                image_file = await save_image(client, recipe_id, recipe["image"])
                if image_file:
                    with get_db() as db:
                        db.execute("UPDATE recipes SET image_file = ? WHERE id = ?", (image_file, recipe_id))
    except httpx.HTTPStatusError as e:
        error = f"That website said no (error {e.response.status_code}). Some sites block apps like this one."
    except httpx.HTTPError:
        error = "Couldn't reach that website."
    except ValueError:
        error = "Couldn't find a recipe on that page."
    except Exception:
        log.exception("Recipe import failed for %s", url)
        error = "Something went wrong reading that page."
    else:
        from . import writer
        writer.wake()   # Claude adds the tags and macros
        return RedirectResponse(f"/recipes/{recipe_id}", status_code=303)
    return page(request, "recipe_new.html", user, active="recipes", url=url,
                error=error + " You can type it in below instead.")


@router.post("/recipes/manual")
def create_manual(user=Depends(auth.require_user), title: str = Form(""), ingredients: str = Form(""),
                  steps: str = Form(""), url: str = Form("")):
    if not title.strip():
        return RedirectResponse("/recipes/new", status_code=303)
    url = url.strip() if url.strip().startswith(("https://", "http://")) else ""
    with get_db() as db:
        recipe_id = db.execute(
            "INSERT INTO recipes (title, url, site, ingredients, steps, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (title.strip()[:200], url, (urlsplit(url).hostname or "").removeprefix("www."),
             json.dumps(clean_lines(ingredients)), json.dumps(clean_lines(steps)), user["id"], time.time())).lastrowid
    from . import writer
    writer.wake()   # Claude adds the tags and macros
    return RedirectResponse(f"/recipes/{recipe_id}", status_code=303)


@router.get("/recipes/{recipe_id}")
def recipe_page(request: Request, recipe_id: int, user=Depends(auth.require_user), message: str = ""):
    r = get_recipe(recipe_id)
    if r is None:
        return RedirectResponse("/recipes", status_code=303)
    ingredients = [{"text": i, "staple": bool(STAPLES.search(i))} for i in r["ingredients"]]
    return page(request, "recipe.html", user, active="recipes", r=r, ingredients=ingredients, message=message)


@router.get("/recipes/{recipe_id}/image")
def recipe_image(recipe_id: int, user=Depends(auth.require_user)):
    r = get_recipe(recipe_id)
    if r is None or not r["image_file"] or not (IMAGE_DIR / r["image_file"]).exists():
        return Response(status_code=404)
    return FileResponse(IMAGE_DIR / r["image_file"], headers={"Cache-Control": "private, max-age=604800"})


@router.post("/recipes/{recipe_id}/confirm")
def confirm(recipe_id: int, user=Depends(auth.require_user)):
    """A draft (e.g. from Instagram or TikTok) has been checked."""
    with get_db() as db:
        db.execute("UPDATE recipes SET draft = 0 WHERE id = ?", (recipe_id,))
    return RedirectResponse(f"/recipes/{recipe_id}", status_code=303)


@router.get("/recipes/{recipe_id}/edit")
def edit_form(request: Request, recipe_id: int, user=Depends(auth.require_user), message: str = ""):
    r = get_recipe(recipe_id)
    if r is None:
        return RedirectResponse("/recipes", status_code=303)
    _, choices = photo_choices(recipe_id)
    return page(request, "recipe_edit.html", user, active="recipes", r=r, choices=choices, message=message,
                cuisines=CUISINES, proteins=PROTEINS, tags=TAGS, sources=MACRO_SOURCES)


@router.post("/recipes/{recipe_id}/edit")
async def update(request: Request, recipe_id: int, user=Depends(auth.require_user)):
    form = await request.form()
    r = get_recipe(recipe_id)
    title = str(form.get("title", "")).strip()
    if r is not None and title:
        with get_db() as db:
            db.execute("UPDATE recipes SET title = ?, ingredients = ?, steps = ?, notes = ?, draft = 0 WHERE id = ?",
                       (title[:200], json.dumps(clean_lines(str(form.get("ingredients", "")))),
                        json.dumps(clean_lines(str(form.get("steps", "")))), str(form.get("notes", "")).strip()[:5000],
                        recipe_id))
        macros = {k: number(form.get(k)) if str(form.get(k, "")).strip() else None
                  for k in ("kcal", "protein_g", "carbs_g", "fat_g")}
        changed = any(macros[k] != r[k] for k in macros)
        filled = form.get("cuisine") or form.getlist("protein") or form.getlist("tag") or any(v is not None for v in macros.values())
        if r["tagged_at"] is not None or filled:    # otherwise leave it for Claude to tag
            set_details(recipe_id, cuisine=str(form.get("cuisine", "")), proteins=form.getlist("protein"),
                        tags=form.getlist("tag"), servings=form.get("servings"), **macros,
                        macros_source="entered" if changed else r["macros_source"])
    return RedirectResponse(f"/recipes/{recipe_id}", status_code=303)


@router.get("/recipes/{recipe_id}/choices/{name}")
def photo_choice(recipe_id: int, name: str, user=Depends(auth.require_user)):
    folder, names = photo_choices(recipe_id)
    if name not in names:
        return Response(status_code=404)
    return FileResponse(folder / name, headers={"Cache-Control": "private, max-age=604800"})


def back_to_edit(recipe_id, message):
    return RedirectResponse(f"/recipes/{recipe_id}/edit?message={quote(message)}#photo", status_code=303)


@router.post("/recipes/{recipe_id}/photo")
async def upload_photo(recipe_id: int, user=Depends(auth.require_user), file: UploadFile = File(...)):
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if get_recipe(recipe_id) is None:
        return RedirectResponse("/recipes", status_code=303)
    if len(data) > MAX_UPLOAD_BYTES:
        return back_to_edit(recipe_id, "That photo is too big (over 25 MB).")
    try:
        await asyncio.to_thread(store_photo, recipe_id, data)
    except Exception:
        return back_to_edit(recipe_id, "Couldn't read that file as a photo.")
    return RedirectResponse(f"/recipes/{recipe_id}", status_code=303)


@router.post("/recipes/{recipe_id}/photo/pick")
async def pick_photo(recipe_id: int, user=Depends(auth.require_user), name: str = Form("")):
    folder, names = photo_choices(recipe_id)
    if name in names:
        await asyncio.to_thread(store_photo, recipe_id, (folder / name).read_bytes())
    return RedirectResponse(f"/recipes/{recipe_id}", status_code=303)


@router.post("/recipes/{recipe_id}/photo/remove")
def remove_photo(recipe_id: int, user=Depends(auth.require_user)):
    r = get_recipe(recipe_id)
    if r is not None and r["image_file"]:
        (IMAGE_DIR / r["image_file"]).unlink(missing_ok=True)
        with get_db() as db:
            db.execute("UPDATE recipes SET image_file = NULL WHERE id = ?", (recipe_id,))
    return back_to_edit(recipe_id, "Photo removed.")


@router.post("/recipes/{recipe_id}/delete")
def delete(recipe_id: int, user=Depends(auth.require_user)):
    r = get_recipe(recipe_id)
    if r is not None:
        if r["image_file"]:
            (IMAGE_DIR / r["image_file"]).unlink(missing_ok=True)
        with get_db() as db:
            db.execute("DELETE FROM recipes WHERE id = ?", (recipe_id,))
    return RedirectResponse("/recipes", status_code=303)
