"""Recipes from Instagram Reels and TikTok.

The app does the fetching: yt-dlp gets the post (Instagram uses the owner's login cookies),
the caption is kept, speech is transcribed locally with Whisper when the caption doesn't
already hold the recipe, a few still frames are taken, and any recipe website linked in the
caption is read. Claude then writes the recipe and saves it as a draft (writer.py). Videos are
deleted straight after; only text, the frames and a thumbnail are kept.

Links are queued by pasting them, sharing them to the app, or uploading an Instagram/TikTok data export.
"""
import asyncio
import io
import json
import logging
import os
import random
import re
import shutil
import subprocess
import tempfile
import time
import wave
import zipfile
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, Response

from . import auth, recipes, writer
from .config import DATA_DIR
from .db import get_db
from .templating import page

log = logging.getLogger(__name__)

MIGRATIONS = [
    ("social-001", """
        CREATE TABLE social_imports (
            id INTEGER PRIMARY KEY,
            url TEXT NOT NULL UNIQUE,
            platform TEXT NOT NULL,                  -- instagram, tiktok or screenshot
            status TEXT NOT NULL DEFAULT 'queued',   -- queued, working, ready, writing, saved, skipped, failed
            uploader TEXT NOT NULL DEFAULT '',
            caption TEXT NOT NULL DEFAULT '',
            transcript TEXT NOT NULL DEFAULT '',
            linked_recipe TEXT NOT NULL DEFAULT '',  -- JSON, if the caption links to a recipe website
            frames TEXT NOT NULL DEFAULT '[]',       -- JSON list of image file names
            error TEXT NOT NULL DEFAULT '',
            recipe_id INTEGER REFERENCES recipes(id) ON DELETE SET NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
    """),
    ("social-002-writer", """
        ALTER TABLE social_imports ADD COLUMN writer_attempts INTEGER NOT NULL DEFAULT 0;   -- Claude's tries at writing it up
    """),
]

WORK_DIR = DATA_DIR / "social"                       # frames and thumbnails, per import
SECRETS_DIR = DATA_DIR / "secrets"
INSTAGRAM_COOKIES = SECRETS_DIR / "instagram-cookies.txt"
MODEL_DIR = DATA_DIR / "models"
WHISPER_MODEL = "base"            # small enough for 1 CPU; "small" is more accurate but much slower
FRAMES = 6
FRAME_WIDTH = 720                 # wide enough to read on-screen text and to use as the recipe photo
MAX_SLIDES = 20                   # photo posts: every slide (Instagram allows up to 20), for Claude to read
SLIDE_MAX_PX = 900                # slide text is big; this keeps 20 slides light enough for Claude
SHOT_MAX_PX = 1600                # screenshots: kept sharp enough to read small print
MAX_SHOTS = 10
IG_APP_ID = "936619743392459"     # the instagram.com website's own app id, needed by its web API
SHORTCODE = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
MAX_SECONDS = 15 * 60             # don't download anything longer than 15 minutes
INSTAGRAM_GAP = (90, 150)         # seconds between Instagram downloads, so the account isn't flagged
MAX_ATTEMPTS = 3

URL_PATTERNS = [
    ("instagram", r"https?://(?:www\.)?instagram\.com/(?:[\w.]+/)?(?:reels?|p|tv)/[\w-]+"),
    ("tiktok", r"https?://(?:www\.|m\.)?tiktok\.com/@[\w.-]*/video/\d+"),
    ("tiktok", r"https?://(?:vm|vt)\.tiktok\.com/[\w]+"),
    ("tiktok", r"https?://(?:www\.)?tiktokv?\.com/share/video/\d+"),
]
_last_instagram = 0.0
_whisper = None


# --- Finding links ---

def normalise(platform, url):
    url = url.split("?")[0].rstrip("/")
    if platform == "instagram":
        m = re.search(r"/(reels?|p|tv)/([\w-]+)", url)
        kind = "reel" if m.group(1).startswith("reel") else m.group(1)
        return f"https://www.instagram.com/{kind}/{m.group(2)}/"
    m = re.search(r"/video/(\d+)", url)
    return f"https://www.tiktok.com/@/video/{m.group(1)}" if m else url


def find_links(text):
    """Instagram and TikTok post links in any text (pasted links, or a data export)."""
    found = []
    for platform, pattern in URL_PATTERNS:
        for m in re.finditer(pattern, text):
            url = normalise(platform, m.group(0))
            if (platform, url) not in found:
                found.append((platform, url))
    return found


def links_from_export(filename, data):
    """Links from an Instagram/TikTok 'download your data' file (a .zip, .json or .html)."""
    texts = []
    if filename.lower().endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for name in z.namelist():
                if name.lower().endswith((".json", ".html", ".txt")) and z.getinfo(name).file_size < 50_000_000:
                    texts.append(z.read(name).decode("utf-8", errors="replace"))
    else:
        texts.append(data.decode("utf-8", errors="replace"))
    return find_links("\n".join(texts).replace("\\/", "/"))


def queue(links, user_id):
    """Add links to the import queue. Returns (added, already there)."""
    added = 0
    t = time.time()
    with get_db() as db:
        for platform, url in links:
            cur = db.execute("""INSERT OR IGNORE INTO social_imports (url, platform, created_by, created_at, updated_at)
                                VALUES (?, ?, ?, ?, ?)""", (url, platform, user_id, t, t))
            added += cur.rowcount
    return added, len(links) - added


# --- Processing one post (runs in a background thread) ---

def recipe_like(text):
    """Does the caption already hold the recipe? Then there's no need to transcribe."""
    lines = [l for l in text.splitlines() if l.strip()]
    measures = len(re.findall(r"\b\d+(?:[./]\d+)?\s*(?:g|kg|ml|l|cups?|tbsp|tsp|tablespoons?|teaspoons?)\b", text, re.I))
    return (re.search(r"ingredients?", text, re.I) and len(lines) >= 5) or measures >= 4


def transcribe(audio_path):
    global _whisper
    if _whisper is None:
        MODEL_DIR.mkdir(parents=True, exist_ok=True)
        # The model downloader keeps its cache in the home folder, which the container can't write to
        os.environ.setdefault("HF_HOME", str(MODEL_DIR / "hf"))
        from faster_whisper import WhisperModel
        _whisper = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8", download_root=str(MODEL_DIR),
                                cpu_threads=0)
    # Hand Whisper the raw sound from our 16 kHz mono WAV (skips its own audio decoder)
    import numpy as np
    with wave.open(str(audio_path), "rb") as w:
        samples = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
    segments, _ = _whisper.transcribe(samples, vad_filter=True, beam_size=1)
    return " ".join(s.text.strip() for s in segments).strip()


def grab_frames(video, out_dir, duration):
    """A handful of evenly spaced still frames, small enough to send to Claude."""
    names = []
    for i in range(FRAMES):
        at = max(0.5, duration * (i + 0.5) / FRAMES) if duration else i * 3 + 0.5
        name = f"frame-{i + 1}.jpg"
        r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{at:.2f}", "-i", str(video), "-frames:v", "1",
                            "-vf", f"scale={FRAME_WIDTH}:-2", "-q:v", "4", str(out_dir / name)], capture_output=True, timeout=60)
        if r.returncode == 0 and (out_dir / name).exists():
            names.append(name)
    return names


def save_jpeg(data, path, max_px):
    """Save an image as a JPEG no bigger than max_px on its longest side."""
    from PIL import Image, ImageOps
    with Image.open(io.BytesIO(data)) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        im.thumbnail((max_px, max_px))
        im.save(path, "JPEG", quality=85)


def fetch_cover(url, path):
    """The creator's own cover picture for a video: usually a better recipe photo than a random frame."""
    import httpx
    try:
        r = httpx.get(url, timeout=20, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        save_jpeg(r.content, path, 1080)
        return True
    except Exception:
        log.warning("Couldn't fetch the cover picture")
        return False


def instagram_photos(item, out_dir):
    """A photo post (one picture or a carousel of slides): the video downloader can't read these, so ask
    instagram.com's web API with the saved login. Returns (caption, uploader, slide file names)."""
    import http.cookiejar
    import httpx
    if not INSTAGRAM_COOKIES.exists():
        raise RuntimeError("login required for photo posts")
    code = re.search(r"/(?:p|reels?|tv)/([\w-]+)", item["url"]).group(1)[:11]
    media_id = 0
    for ch in code:
        media_id = media_id * 64 + SHORTCODE.index(ch)
    jar = http.cookiejar.MozillaCookieJar(str(INSTAGRAM_COOKIES))
    jar.load(ignore_discard=True, ignore_expires=True)
    cookies = {c.name: c.value for c in jar if "instagram.com" in c.domain}
    headers = {"X-IG-App-ID": IG_APP_ID, "X-CSRFToken": cookies.get("csrftoken", ""), "Referer": "https://www.instagram.com/",
               "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) "
                             "Version/17.0 Safari/605.1.15"}
    with httpx.Client(timeout=30, cookies=cookies, headers=headers, follow_redirects=True) as client:
        r = client.get(f"https://www.instagram.com/api/v1/media/{media_id}/info/")
        if r.status_code in (401, 403) or "login" in str(r.url):
            raise RuntimeError("login required")
        r.raise_for_status()
        post = r.json()["items"][0]
        names = []
        for i, slide in enumerate((post.get("carousel_media") or [post])[:MAX_SLIDES]):
            sizes = (slide.get("image_versions2") or {}).get("candidates") or []
            if not sizes:
                continue
            # The smallest picture that's still at least 1080 wide (text on slides needs to be readable)
            best = min((c for c in sizes if c.get("width", 0) >= 1080), key=lambda c: c["width"], default=sizes[0])
            name = f"slide-{i + 1}.jpg"
            save_jpeg(client.get(best["url"], headers={"Referer": ""}).content, out_dir / name, SLIDE_MAX_PX)
            names.append(name)
    caption = ((post.get("caption") or {}).get("text") or "").strip()
    return caption, (post.get("user") or {}).get("username", ""), names


def process(item):
    """Fetch, read and transcribe one post. Returns the fields to save."""
    import yt_dlp
    out_dir = WORK_DIR / str(item["id"])
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="reelcookbook-"))
    try:
        opts = {"quiet": True, "no_warnings": True, "noprogress": True, "noplaylist": True, "outtmpl": str(tmp / "media.%(ext)s"),
                "format": "worst[ext=mp4][vcodec!=none][acodec!=none]/worst[ext=mp4]/worst",
                "match_filter": yt_dlp.utils.match_filter_func(f"duration <=? {MAX_SECONDS}"),   # "?" = allow unknown length
                "socket_timeout": 30, "retries": 2}
        if item["platform"] == "instagram" and INSTAGRAM_COOKIES.exists():
            opts["cookiefile"] = str(INSTAGRAM_COOKIES)
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(item["url"], download=True)
        except Exception as e:
            if item["platform"] == "instagram" and "no video" in str(e).lower():
                caption, uploader, slides = instagram_photos(item, out_dir)
                if not slides and not caption:
                    raise RuntimeError("this photo post has no pictures or caption") from e
                if slides:
                    shutil.copy(out_dir / slides[0], out_dir / "thumb.jpg")
                return {"status": "ready", "caption": caption[:20000], "uploader": uploader[:100], "transcript": "",
                        "frames": json.dumps(slides), "error": "", "linked_recipe": linked_recipe(caption)}
            raise
        if info.get("_type") == "playlist" and info.get("entries"):
            info = next((e for e in info["entries"] if e), info)
        caption = (info.get("description") or info.get("title") or "").strip()
        uploader = info.get("uploader") or info.get("channel") or info.get("uploader_id") or ""
        media = next(iter(sorted(tmp.glob("media.*"))), None)
        if media is None and not caption:
            raise RuntimeError("no video was downloaded and the post has no caption")
        frames, transcript = [], ""
        if media:
            frames = grab_frames(media, out_dir, info.get("duration") or 0)
            if not recipe_like(caption):
                audio = tmp / "audio.wav"
                r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(media), "-vn", "-ac", "1", "-ar", "16000",
                                    str(audio)], capture_output=True, timeout=300)
                if r.returncode == 0:
                    transcript = transcribe(audio)
        if info.get("thumbnail") and media:
            fetch_cover(info["thumbnail"], out_dir / "cover.jpg")
        if not (out_dir / "thumb.jpg").exists():   # the recipe photo, unless Claude picks a better one
            first = out_dir / "cover.jpg" if (out_dir / "cover.jpg").exists() else (out_dir / frames[0] if frames else None)
            if first:
                shutil.copy(first, out_dir / "thumb.jpg")
        return {"status": "ready", "caption": caption[:20000], "uploader": uploader[:100], "transcript": transcript[:30000],
                "frames": json.dumps(frames), "error": "", "linked_recipe": linked_recipe(caption)}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)   # never keep the video


def linked_recipe(caption):
    """If the caption links to a recipe website, read that recipe too."""
    import httpx
    for url in re.findall(r"https?://[^\s)\]]+", caption):
        if re.search(r"instagram\.com|tiktok\.com|linktr\.ee|bit\.ly", url):
            continue
        try:
            with httpx.Client(timeout=20, follow_redirects=True) as client:
                html = client.get(url, headers=recipes.BROWSER_HEADERS).text
            return json.dumps({**recipes.scrape(html, url), "url": url})
        except Exception:
            continue
    return ""


def friendly_error(e):
    text = str(e)
    if "login" in text.lower() or "cookies" in text.lower() or "rate-limit" in text.lower():
        return "Instagram wants a login: the saved Instagram cookies are missing or have expired."
    if "no video" in text.lower():
        return "This post has no video (it may be photos only)."
    if "private" in text.lower():
        return "This post is private or has been removed."
    if "duration" in text.lower():
        return "This video is longer than 15 minutes, so it was skipped."
    if "404" in text or "not found" in text.lower():
        return "This post can't be found (it may have been deleted)."
    reason = re.sub(r"\s*\(caused by.*$", "", text.replace("ERROR:", "")).strip()
    return "Couldn't download this post: " + reason[:140]


def claim(import_id):
    """Mark a queued post as being worked on. Only one worker can claim it."""
    with get_db() as db:
        return db.execute("""UPDATE social_imports SET status = 'working', attempts = attempts + 1, updated_at = ?
                             WHERE id = ? AND status = 'queued'""", (time.time(), import_id)).rowcount == 1


async def run_one(import_id):
    """Process one claimed post and save the result."""
    global _last_instagram
    item = get(import_id)
    if item["platform"] == "instagram":
        _last_instagram = time.time()
    try:
        result = await asyncio.to_thread(process, item)
    except Exception as e:
        log.warning("Import %s failed: %s", item["url"], e)
        final = item["attempts"] >= MAX_ATTEMPTS or any(w in str(e).lower() for w in ("private", "no video", "duration"))
        result = {"status": "failed" if final else "queued", "error": friendly_error(e)}
    sets = ", ".join(f"{k} = :{k}" for k in result)
    with get_db() as db:
        db.execute(f"UPDATE social_imports SET {sets}, updated_at = :t WHERE id = :id",
                   {**result, "t": time.time(), "id": import_id})
    log.info("Import %s → %s", item["url"], result["status"])
    if result["status"] == "ready":
        writer.wake()


async def work():
    """Every 30 seconds: process the next queued post (one at a time, gently for Instagram)."""
    with get_db() as db:
        db.execute("UPDATE social_imports SET status = 'queued' WHERE status = 'working' AND updated_at < ?",
                   (time.time() - 1800,))   # recover from a restart part-way through
        item = db.execute("""SELECT * FROM social_imports WHERE status = 'queued' AND attempts < ?
                             ORDER BY created_at LIMIT 1""", (MAX_ATTEMPTS,)).fetchone()
    if item is None:
        return
    if item["platform"] == "instagram" and time.time() - _last_instagram < random.uniform(*INSTAGRAM_GAP):
        return
    if claim(item["id"]):
        await run_one(item["id"])


# --- Reading results ---

def get(import_id):
    with get_db() as db:
        return db.execute("SELECT * FROM social_imports WHERE id = ?", (import_id,)).fetchone()


def by_url(url):
    links = find_links(url)
    if not links:
        return None
    with get_db() as db:
        return db.execute("SELECT * FROM social_imports WHERE url = ?", (links[0][1],)).fetchone()


def frame_paths(item):
    """The pictures Claude sees: the cover (if any), then the video stills, slides or screenshots."""
    folder = WORK_DIR / str(item["id"])
    names = (["cover.jpg"] if (folder / "cover.jpg").exists() else []) + json.loads(item["frames"] or "[]")
    return [folder / name for name in names if (folder / name).exists()]


def add_screenshots(images, note, user_id):
    """Screenshots of one recipe (from MyFitnessPal, a cookbook, a website...): queued straight away as
    ready for Claude, who reads them. Returns the import id."""
    import uuid
    with get_db() as db:
        import_id = db.execute(
            """INSERT INTO social_imports (url, platform, status, caption, created_by, created_at, updated_at)
               VALUES (?, 'screenshot', 'working', ?, ?, ?, ?)""",
            (f"screenshot:{uuid.uuid4().hex}", note.strip()[:2000], user_id, time.time(), time.time())).lastrowid
    out_dir = WORK_DIR / str(import_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    names = []
    for i, data in enumerate(images[:MAX_SHOTS]):
        name = f"shot-{i + 1}.jpg"
        try:
            save_jpeg(data, out_dir / name, SHOT_MAX_PX)
            names.append(name)
        except Exception:
            log.warning("Screenshot %s of import %s isn't a picture", i + 1, import_id)
    with get_db() as db:
        if names:
            db.execute("UPDATE social_imports SET status = 'ready', frames = ?, updated_at = ? WHERE id = ?",
                       (json.dumps(names), time.time(), import_id))
        else:
            db.execute("DELETE FROM social_imports WHERE id = ?", (import_id,))
    if not names:
        shutil.rmtree(out_dir, ignore_errors=True)
        return None
    shutil.copy(out_dir / names[0], out_dir / "thumb.jpg")   # only for the queue list; not used as the recipe photo
    writer.wake()
    return import_id


def mark(import_id, status, recipe_id=None, note=""):
    with get_db() as db:
        db.execute("UPDATE social_imports SET status = ?, recipe_id = COALESCE(?, recipe_id), error = ?, updated_at = ? WHERE id = ?",
                   (status, recipe_id, note, time.time(), import_id))


def counts():
    with get_db() as db:
        return dict(db.execute("SELECT status, COUNT(*) FROM social_imports GROUP BY status").fetchall())


# --- Pages ---

router = APIRouter(prefix="/import")


@router.get("")
def social_page(request: Request, user=Depends(auth.require_user), message: str = ""):
    with get_db() as db:
        items = db.execute("SELECT * FROM social_imports ORDER BY updated_at DESC LIMIT 60").fetchall()
    return page(request, "import.html", user, active="import", items=items, counts=counts(), message=message,
                has_cookies=INSTAGRAM_COOKIES.exists(), writer=writer.status(), writer_on=writer.enabled(),
                max_shots=MAX_SHOTS)


@router.post("/write-now")
def write_now(user=Depends(auth.require_user)):
    writer.wake()
    return RedirectResponse("/import?message=" + quote("Claude is starting on them now."), status_code=303)


@router.post("/links")
def add_links(user=Depends(auth.require_user), links: str = Form("")):
    found = find_links(links)
    added, existing = queue(found, user["id"])
    msg = f"Added {added} to the queue." + (f" {existing} were already there." if existing else "") if found \
        else "No Instagram or TikTok links found in that text."
    return RedirectResponse("/import?message=" + quote(msg), status_code=303)


@router.post("/screenshots")
async def upload_screenshots(request: Request, user=Depends(auth.require_user)):
    form = await request.form()
    files = [f for f in form.getlist("files") if hasattr(f, "read")][:MAX_SHOTS]
    images = [data for data in [await f.read(30_000_001) for f in files] if 0 < len(data) <= 30_000_000]
    import_id = await asyncio.to_thread(add_screenshots, images, str(form.get("note", "")), user["id"]) if images else None
    if import_id and not writer.enabled():
        return RedirectResponse(f"/import/{import_id}/write", status_code=303)
    msg = (f"Got {len(images)} screenshot{'s' if len(images) != 1 else ''}. Claude will turn {'them' if len(images) != 1 else 'it'} "
           "into a draft recipe in a minute or two.") if import_id else "Those files didn't look like pictures."
    return RedirectResponse("/import?message=" + quote(msg), status_code=303)


@router.post("/export")
async def upload_export(user=Depends(auth.require_user), file: UploadFile = File(...)):
    data = await file.read(200_000_001)
    if len(data) > 200_000_000:
        return RedirectResponse("/import?message=" + quote("That file is too big (over 200 MB)."), status_code=303)
    try:
        found = await asyncio.to_thread(links_from_export, file.filename or "", data)
    except zipfile.BadZipFile:
        found = []
    added, existing = queue(found, user["id"])
    msg = f"Found {len(found)} saved posts: {added} added to the queue, {existing} already there."
    return RedirectResponse("/import?message=" + quote(msg), status_code=303)


@router.post("/cookies")
async def upload_cookies(user=Depends(auth.require_user), file: UploadFile = File(...)):
    data = (await file.read(1_000_001)).decode("utf-8", errors="replace")
    if "instagram.com" not in data or "sessionid" not in data:
        return RedirectResponse("/import?message=" + quote(
            "That doesn't look like an Instagram cookies.txt file (it needs to include your Instagram login)."), status_code=303)
    SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    SECRETS_DIR.chmod(0o700)
    INSTAGRAM_COOKIES.write_text(data)
    INSTAGRAM_COOKIES.chmod(0o600)
    with get_db() as db:   # give failed Instagram posts another go
        db.execute("UPDATE social_imports SET status = 'queued', attempts = 0, error = '' WHERE platform = 'instagram' AND status = 'failed'")
    return RedirectResponse("/import?message=" + quote("Instagram login saved."), status_code=303)


@router.post("/cookies/delete")
def delete_cookies(user=Depends(auth.require_user)):
    INSTAGRAM_COOKIES.unlink(missing_ok=True)
    return RedirectResponse("/import?message=" + quote("Instagram login removed."), status_code=303)


@router.post("/{import_id}/retry")
def retry(import_id: int, user=Depends(auth.require_user)):
    with get_db() as db:
        db.execute("""UPDATE social_imports SET status = CASE WHEN platform = 'screenshot' THEN 'ready' ELSE 'queued' END,
                      attempts = 0, writer_attempts = 0, error = '' WHERE id = ?""", (import_id,))
    writer.wake()
    return RedirectResponse("/import", status_code=303)


@router.post("/{import_id}/skip")
def skip(import_id: int, user=Depends(auth.require_user)):
    mark(import_id, "skipped", note="Skipped by hand.")
    return RedirectResponse("/import", status_code=303)


@router.get("/{import_id}/thumb")
def thumb(import_id: int, user=Depends(auth.require_user)):
    path = WORK_DIR / str(import_id) / "thumb.jpg"
    return FileResponse(path) if path.exists() else Response(status_code=404)


@router.get("/{import_id}/pictures/{name}")
def picture(import_id: int, name: str, user=Depends(auth.require_user)):
    item = get(import_id)
    paths = {p.name: p for p in frame_paths(item)} if item else {}
    return FileResponse(paths[name], headers={"Cache-Control": "private, max-age=604800"}) if name in paths \
        else Response(status_code=404)


# --- Writing the recipe up by hand (when there's no Claude API key, or Claude couldn't) ---

@router.get("/{import_id}/write")
def write_form(request: Request, import_id: int, user=Depends(auth.require_user)):
    item = get(import_id)
    if item is None or item["status"] not in ("ready", "skipped", "failed"):
        return RedirectResponse("/import", status_code=303)
    linked = json.loads(item["linked_recipe"]) if item["linked_recipe"] else None
    return page(request, "import_write.html", user, active="import", item=item, linked=linked,
                pictures=[p.name for p in frame_paths(item)])


@router.post("/{import_id}/write")
async def write_submit(import_id: int, user=Depends(auth.require_user), title: str = Form(""),
                       ingredients: str = Form(""), steps: str = Form(""), notes: str = Form(""), photo: str = Form("")):
    item = get(import_id)
    if item is None or not title.strip():
        return RedirectResponse(f"/import/{import_id}/write", status_code=303)
    recipe_id = await asyncio.to_thread(save_from_import, item, title, recipes.clean_lines(ingredients),
                                        recipes.clean_lines(steps), notes, photo, user["id"], draft=False)
    writer.wake()   # Claude adds the tags and macros, if it's set up
    return RedirectResponse(f"/recipes/{recipe_id}", status_code=303)


def save_from_import(item, title, ingredients, steps, notes, photo, user_id, draft=True, source_name=""):
    """Save a recipe made from an import (by Claude, or typed up by hand), credit the creator,
    give it the chosen picture (or the cover) and mark the import as saved. Returns the recipe id."""
    if item["platform"] == "screenshot":
        url, site = "", source_name.strip()[:60] or "Screenshots"
    else:
        url = item["url"]
        site = ("Instagram" if item["platform"] == "instagram" else "TikTok") + (f" · {item['uploader']}" if item["uploader"] else "")
    with get_db() as db:
        recipe_id = db.execute(
            """INSERT INTO recipes (title, url, site, ingredients, steps, notes, draft, created_by, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (title.strip()[:200], url, site[:100], json.dumps([i.strip() for i in ingredients if i.strip()]),
             json.dumps([s.strip() for s in steps if s.strip()]), notes.strip()[:5000], 1 if draft else 0, user_id,
             time.time())).lastrowid
    mark(item["id"], "saved", recipe_id)
    paths = {p.name: p for p in frame_paths(item)}
    chosen = paths.get(photo.strip())
    if chosen is None and item["platform"] != "screenshot":
        # Screenshots only get a photo when one is picked; posts fall back to the cover / first still
        chosen = WORK_DIR / str(item["id"]) / "thumb.jpg"
    if chosen and chosen.exists():
        recipes.store_photo(recipe_id, chosen.read_bytes())
    return recipe_id


JOBS = [(30, work)]
