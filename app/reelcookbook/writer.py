"""Claude writes the recipes. When a post has been downloaded (social.py), its caption, transcript,
linked recipe page and pictures go to Claude in one request, and the answer comes back as a recipe
in a fixed shape (structured output). It's saved as a draft for the household to check.
Claude also adds tags and macros to recipes saved from websites or typed in.

Needs ANTHROPIC_API_KEY. Without it, nothing here runs and imports wait for someone to write them up.
"""
import asyncio
import base64
import json
import logging
import time

import anthropic

from . import config, recipes, social
from .db import get_db, set_setting, setting

log = logging.getLogger(__name__)

MAX_TRIES = 3                # per import / recipe, before it's left for a person
BACKOFF = 5 * 60             # after a rate limit or an outage, wait this long
FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}
KNOWN_TITLES = 300           # recent recipe titles shown to Claude, so it can spot duplicates

_loop = None
_wake = asyncio.Event()
_client = None


def enabled():
    return bool(config.ANTHROPIC_API_KEY)


def wake():
    """Start on any waiting work now (safe to call from any thread)."""
    if _loop is not None:
        _loop.call_soon_threadsafe(_wake.set)


def status():
    s = setting("writer:status") or {}
    if s.get("state") == "running" and time.time() - s.get("at", 0) > 30 * 60:   # e.g. restarted mid-run
        s.update(state="idle", message="")
    return s


def set_status(state, message=""):
    set_setting("writer:status", {"state": state, "message": message, "at": time.time()})


class Stop(Exception):
    """Nothing more can be done until the settings change (bad key, no credit) or time passes (rate limit)."""
    def __init__(self, message, wait=None):
        super().__init__(message)
        self.wait = wait


# --- What Claude is asked ---

UNITS = {"metric": "metric quantities (g, ml, °C), converting cups and ounces where it's clear",
         "us": "US quantities (cups, tbsp, oz, °F), as the creator gave them where possible"}

SYSTEM = f"""You turn cooking posts from Instagram and TikTok, and screenshots of recipes, into clear recipes \
for a household's recipe collection.

You get the post's caption, a transcript of what's said in the video, any recipe web page the caption links \
to, and pictures: the cover, still frames from the video, or the photo slides. Read on-screen text in the \
pictures too; creators often show quantities there instead of saying them.

Keep the creator's recipe and method. Use {UNITS[config.UNITS]}. One ingredient per line, with its quantity \
first ("200 g chicken thigh, diced"). One step per line, in order, without numbering. If quantities or times \
are missing, give sensible estimates and say what you estimated in notes. Keep notes short; leave them empty \
if there is nothing worth saying.

Tags: cuisine is one of {", ".join(recipes.CUISINES)} (empty if none fits). proteins are the meat or base, \
any that apply. tags are any that apply. {recipes.TAG_GUIDE}
Macros are per serving: kcal, protein_g, carbs_g, fat_g, with servings = how many it makes. macros_source is \
"recipe" if the creator or app gave the numbers, else "estimated": work them out from the ingredients and \
quantities (a sensible estimate beats leaving them out). Use -1 only when it's truly impossible to say, and \
0 for an unknown servings or total_minutes.

If the post isn't a recipe (or not enough of one to cook from), or it's the same dish from the same creator as \
one already in the collection, set is_recipe to false and give a short skip_reason."""

DETAILS = {   # the tags and macros, shared by both schemas
    "cuisine": {"type": "string", "enum": recipes.CUISINES + [""]},
    "proteins": {"type": "array", "items": {"type": "string", "enum": recipes.PROTEINS}},
    "tags": {"type": "array", "items": {"type": "string", "enum": recipes.TAGS}},
    "servings": {"type": "integer"},
    "kcal": {"type": "number"},
    "protein_g": {"type": "number"},
    "carbs_g": {"type": "number"},
    "fat_g": {"type": "number"},
    "macros_source": {"type": "string", "enum": ["recipe", "estimated"]},
}


def schema(properties):
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def recipe_schema(pictures):
    return schema({
        "is_recipe": {"type": "boolean"},
        "skip_reason": {"type": "string"},
        "title": {"type": "string"},
        "ingredients": {"type": "array", "items": {"type": "string"}},
        "steps": {"type": "array", "items": {"type": "string"}},
        "notes": {"type": "string"},
        "total_minutes": {"type": "integer"},
        # The picture that best shows the finished dish ("" for none, e.g. screenshots of an app)
        "photo": {"type": "string", "enum": pictures + [""]},
        # For screenshots: the app or book they came from, if you can tell (e.g. "MyFitnessPal")
        "source_name": {"type": "string"},
        **DETAILS,
    })


def known_titles():
    with get_db() as db:
        rows = db.execute("SELECT title, site FROM recipes ORDER BY created_at DESC LIMIT ?", (KNOWN_TITLES,)).fetchall()
    return "\n".join(f"- {r['title']}" + (f" ({r['site']})" if r["site"] else "") for r in rows) or "(none yet)"


def import_content(item):
    """The user message for one import: its text, then each picture with its file name."""
    details = {
        "platform": item["platform"], "url": item["url"] if item["platform"] != "screenshot" else "",
        "creator": item["uploader"], "caption": item["caption"],
        "spoken_transcript": item["transcript"] or "(not transcribed)",
        "linked_recipe_page": json.loads(item["linked_recipe"]) if item["linked_recipe"] else None,
    }
    if item["platform"] == "screenshot":
        intro = ("These are screenshots of one recipe that someone in the household uploaded (e.g. from an app, "
                 "a cookbook or a website). The caption is their note. Read the recipe and any nutrition shown.")
    else:
        intro = "Here's a saved post. Write it up as a recipe."
    content = [{"type": "text", "text": f"{intro}\n\n{json.dumps(details, ensure_ascii=False, indent=1)}\n\n"
                                        f"Recipes already in the collection:\n{known_titles()}"}]
    for path in social.frame_paths(item):
        content += [{"type": "text", "text": f"Picture {path.name}:"},
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                 "data": base64.standard_b64encode(path.read_bytes()).decode()}}]
    return content


# --- Calling Claude ---

def ask(content, output_schema):
    """One request to Claude; returns the parsed JSON answer. Raises Stop for problems that affect
    every request, ValueError for a problem with this one."""
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    extra = {}
    if config.CLAUDE_MODEL in FALLBACK_MODELS:
        # If a safety check declines the request, the API retries it on a suitable model by itself
        extra = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
    try:
        response = _client.beta.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=16000,
            system=SYSTEM,
            messages=[{"role": "user", "content": content}],
            output_config={"effort": "medium", "format": {"type": "json_schema", "schema": output_schema}},
            **extra,
        )
    except (anthropic.AuthenticationError, anthropic.PermissionDeniedError):
        raise Stop("Claude didn't accept the API key. Check ANTHROPIC_API_KEY in .env, then restart the app.")
    except anthropic.NotFoundError:
        raise Stop(f"Claude doesn't know the model “{config.CLAUDE_MODEL}”. Check CLAUDE_MODEL in .env.")
    except anthropic.RateLimitError:
        raise Stop("Claude is busy (rate limit). Trying again in a few minutes.", wait=BACKOFF)
    except anthropic.BadRequestError as e:
        if "credit balance" in str(e).lower():
            raise Stop("The Anthropic account is out of credit. Top it up at console.anthropic.com, then tap Write now.")
        raise ValueError(f"Claude couldn't take this one ({e.message[:160]})")
    except anthropic.APIStatusError as e:
        if e.status_code >= 500:
            raise Stop("Claude is having problems right now. Trying again in a few minutes.", wait=BACKOFF)
        raise ValueError(f"Claude couldn't take this one (error {e.status_code})")
    except anthropic.APIConnectionError:
        raise Stop("Couldn't reach Claude (no internet?). Trying again in a few minutes.", wait=BACKOFF)
    if response.stop_reason == "refusal":
        raise ValueError("Claude declined to read this post")
    if response.stop_reason == "max_tokens":
        raise ValueError("Claude's answer was too long")
    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        return json.loads(text)
    except ValueError:
        raise ValueError("Claude's answer wasn't readable")


def known(v):
    return v if v is not None and v >= 0 else None


def save_details(recipe_id, answer):
    recipes.set_details(recipe_id, answer["cuisine"], answer["proteins"], answer["tags"], answer["servings"] or None,
                        known(answer["kcal"]), known(answer["protein_g"]), known(answer["carbs_g"]),
                        known(answer["fat_g"]), answer["macros_source"])


def write_import(item):
    """Turn one downloaded post into a draft recipe (or skip it). Returns a short label for the status line."""
    pictures = [p.name for p in social.frame_paths(item)]
    answer = ask(import_content(item), recipe_schema(pictures))
    if not answer["is_recipe"] or not answer["title"].strip() or not answer["ingredients"]:
        social.mark(item["id"], "skipped", note=(answer["skip_reason"] or "Not a recipe")[:200])
        return None
    recipe_id = social.save_from_import(item, answer["title"], answer["ingredients"], answer["steps"], answer["notes"],
                                        answer["photo"], item["created_by"], draft=True, source_name=answer["source_name"])
    save_details(recipe_id, answer)
    if answer["total_minutes"] > 0:
        with get_db() as db:
            db.execute("UPDATE recipes SET total_minutes = ? WHERE id = ?", (answer["total_minutes"], recipe_id))
    return answer["title"].strip()


def tag_recipe(r):
    """Add tags and macros to a recipe from a website or typed in."""
    text = json.dumps({k: r[k] for k in ("title", "site", "ingredients", "steps", "yields", "notes", "total_minutes")},
                      ensure_ascii=False, indent=1)
    answer = ask([{"type": "text", "text": "Add the tags and per-serving macros for this saved recipe.\n\n" + text}],
                 schema(DETAILS))
    save_details(r["id"], answer)


# --- The background loop ---

def next_import():
    with get_db() as db:
        item = db.execute("SELECT * FROM social_imports WHERE status = 'ready' AND writer_attempts < ? "
                          "ORDER BY created_at LIMIT 1", (MAX_TRIES,)).fetchone()
        if item is None:
            return None
        claimed = db.execute("""UPDATE social_imports SET status = 'writing', writer_attempts = writer_attempts + 1,
                                updated_at = ? WHERE id = ? AND status = 'ready'""", (time.time(), item["id"])).rowcount
    return social.get(item["id"]) if claimed else None


def give_back(item, error):
    """Claude couldn't write this one: try again later, or leave it for a person after MAX_TRIES."""
    with get_db() as db:
        final = item["writer_attempts"] >= MAX_TRIES
        db.execute("UPDATE social_imports SET status = ?, error = ?, updated_at = ? WHERE id = ?",
                   ("failed" if final else "ready", error[:200] if final else "", time.time(), item["id"]))


_untag_failures = {}   # recipe id → failed tries (kept in memory; a restart gives them another go)


def next_untagged():
    with get_db() as db:
        ids = [row["id"] for row in db.execute("SELECT id FROM recipes WHERE tagged_at IS NULL ORDER BY created_at")]
    return next((recipes.get_recipe(i) for i in ids if _untag_failures.get(i, 0) < MAX_TRIES), None)


def work_through():
    """Write up every waiting import, then tag every untagged recipe. Runs in a thread."""
    saved, skipped, tagged, failed = [], 0, 0, 0
    try:
        while (item := next_import()) is not None:
            set_status("running", "Writing up " + (item["uploader"] or "a post") + "…")
            try:
                title = write_import(item)
            except ValueError as e:
                log.warning("Import %s: %s", item["id"], e)
                give_back(item, str(e))
                failed += 1
                continue
            except Exception:
                give_back(item, "Something went wrong writing this one up")
                raise
            if title:
                saved.append(title)
            else:
                skipped += 1
        while (r := next_untagged()) is not None:
            set_status("running", f"Adding tags and macros to {r['title']}…")
            try:
                tag_recipe(r)
                tagged += 1
            except ValueError as e:
                log.warning("Tagging recipe %s: %s", r["id"], e)
                _untag_failures[r["id"]] = _untag_failures.get(r["id"], 0) + 1
    except Stop:
        with get_db() as db:   # anything claimed goes back to the pile
            db.execute("UPDATE social_imports SET status = 'ready', writer_attempts = MAX(writer_attempts - 1, 0) "
                       "WHERE status = 'writing'")
        raise
    finally:
        if saved or skipped or tagged or failed:
            parts = []
            if saved:
                parts.append(f"Saved {len(saved)} draft{'s' if len(saved) != 1 else ''} ({', '.join(saved[:3])}"
                             f"{', …' if len(saved) > 3 else ''})")
            if skipped:
                parts.append(f"skipped {skipped} (not recipes or already saved)")
            if tagged:
                parts.append(f"tagged {tagged}")
            if failed:
                parts.append(f"{failed} need another try")
            message = "; ".join(parts)
            set_status("done", message[0].upper() + message[1:] + ".")
    return bool(saved or skipped or tagged or failed)


async def run():
    """Started with the app: waits to be woken (or checks every few minutes) and works through anything waiting."""
    global _loop
    _loop = asyncio.get_running_loop()
    with get_db() as db:   # recover from a restart part-way through
        db.execute("UPDATE social_imports SET status = 'ready' WHERE status = 'writing'")
    if not enabled():
        log.info("No ANTHROPIC_API_KEY: imports will wait to be written up by hand")
        return
    if status().get("state") in ("running", "error"):
        set_status("idle")
    while True:
        try:
            await asyncio.wait_for(_wake.wait(), timeout=180)
        except TimeoutError:
            pass
        _wake.clear()
        try:
            await asyncio.to_thread(work_through)
        except Stop as e:
            log.warning("Claude stopped: %s", e)
            set_status("error", str(e))
            if e.wait:
                await asyncio.sleep(e.wait)
                wake()
            else:
                _wake.clear()   # wait for someone to tap Write now (or a restart with a new key)
                await _wake.wait()
                _wake.set()
        except Exception:
            log.exception("Writing recipes failed; will try again")
            set_status("error", "Something went wrong. Trying again in a few minutes.")
            await asyncio.sleep(BACKOFF)
