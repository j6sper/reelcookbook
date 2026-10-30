"""First-time setup, login, Settings (household members, password) and the Share menu."""
import asyncio
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from . import auth, config, social, writer
from .templating import page

router = APIRouter()


def safe_next(url):
    """Only allow redirects to our own pages after login."""
    return url if url.startswith("/") and not url.startswith("//") else "/recipes"


# --- First-time setup: only works while there are no accounts yet ---

@router.get("/setup")
def setup_form(request: Request):
    if auth.user_count() > 0:
        return RedirectResponse("/login", status_code=303)
    return page(request, "setup.html")


@router.post("/setup")
def setup_submit(request: Request, display_name: str = Form(), username: str = Form(),
                 password: str = Form(), password2: str = Form()):
    if auth.user_count() > 0:
        return RedirectResponse("/login", status_code=303)
    error = check_new_account(username, display_name, password, password2)
    if error:
        return page(request, "setup.html", error=error, display_name=display_name, username=username)
    auth.create_user(username, display_name, password)
    user = auth.authenticate(username, password)
    response = RedirectResponse("/recipes", status_code=303)
    auth.start_session(response, request, user["id"])
    return response


def check_new_account(username, display_name, password, password2):
    if not username.strip() or not display_name.strip():
        return "Please fill in a name and a username."
    if len(password) < 8:
        return "Passwords need at least 8 characters."
    if password != password2:
        return "The two passwords don't match."
    if any(u["username"].lower() == username.strip().lower() for u in auth.list_users()):
        return "That username is already taken."
    return None


# --- Login / logout ---

@router.get("/login")
def login_form(request: Request, next: str = "/recipes"):
    if auth.user_count() == 0:
        return RedirectResponse("/setup", status_code=303)
    return page(request, "login.html", next=safe_next(next))


@router.post("/login")
async def login_submit(request: Request, username: str = Form(), password: str = Form(), next: str = Form("/recipes")):
    user, error = await auth.login_attempt(request, username, password)
    if user is None:
        return page(request, "login.html", error=error, username=username, next=safe_next(next))
    response = RedirectResponse(safe_next(next), status_code=303)
    auth.start_session(response, request, user["id"])
    return response


@router.post("/logout")
def logout(request: Request):
    response = RedirectResponse("/login", status_code=303)
    auth.end_session(request, response)
    return response


# --- Shared from another app (Android Share menu, or a /share?url=... link) ---

@router.get("/share")
def share(user=Depends(auth.require_user), title: str = "", text: str = "", url: str = ""):
    shared = " ".join([url, text, title])
    found = social.find_links(shared)
    if found:
        added, existing = social.queue(found, user["id"])
        msg = f"Added {added} to the queue." if added else "That post is already in the queue."
        return RedirectResponse("/import?message=" + quote(msg), status_code=303)
    link = next((w for w in shared.split() if w.startswith(("https://", "http://"))), "")
    if link:   # a recipe website
        return RedirectResponse("/recipes/new?url=" + quote(link), status_code=303)
    return RedirectResponse("/import?message=" + quote("No link found in what was shared."), status_code=303)


# --- Settings ---

@router.get("/settings")
def settings(request: Request, user=Depends(auth.require_user), message: str = "", ok: str = ""):
    claude = writer.settings()
    response = page(request, "settings.html", user, active="settings", members=auth.list_users(), message=message,
                    ok=ok, claude=claude, writer_on=writer.enabled(claude), modes=writer.MODES,
                    models={**writer.MODELS, **({} if claude["model"] in writer.MODELS else {claude["model"]: claude["model"]})},
                    token_hint=hint(claude["oauth_token"]), key_hint=hint(claude["api_key"]))
    response.headers["Cache-Control"] = "no-store"
    return response


def hint(secret):
    """Enough of a saved token or key to recognise it, never the whole thing."""
    return f"…{secret[-4:]}" if secret else ""


def back(message, worked=None):
    return RedirectResponse(f"/settings?message={quote(message)}" + ("&ok=1" if worked else "") + "#claude",
                            status_code=303)


@router.post("/settings/claude")
async def save_claude(user=Depends(auth.require_user), mode: str = Form("off"), oauth_token: str = Form(""),
                      api_key: str = Form(""), model: str = Form(""), units: str = Form("metric")):
    token, key = "".join(oauth_token.split()), "".join(api_key.split())   # pasted text often has spaces or line breaks
    if token.startswith("sk-ant-api") or key.startswith("sk-ant-oat"):
        return back("That looks like it went in the wrong box: subscription tokens start sk-ant-oat, "
                    "API keys start sk-ant-api. Nothing was changed.")
    if token and not token.startswith("sk-ant-"):
        return back("That isn't a Claude token: it should start sk-ant-oat. Nothing was changed.")
    if key and not key.startswith("sk-ant-"):
        return back("That isn't a Claude API key: it should start sk-ant-api. Nothing was changed.")
    changes = {"mode": mode if mode in writer.MODES else "off", "model": model.strip() or config.CLAUDE_MODEL,
               "units": "us" if units == "us" else "metric"}
    if token:
        changes["oauth_token"] = token
    if key:
        changes["api_key"] = key
    claude = {**writer.settings(), **changes}
    if changes["mode"] == "off":
        writer.save_settings(**changes)
        return back("Saved. Claude is off: you write imported recipes up yourself.")
    if not writer.enabled(claude):
        return back("To use " + ("your Claude subscription, paste a token" if changes["mode"] == "subscription"
                                 else "an API key, paste the key") + " (see the steps below). Nothing was changed.")
    worked, message = await asyncio.to_thread(writer.test_connection, claude)
    writer.save_settings(**changes)   # saved either way, so a slip can be fixed without pasting it all again
    return back(message if worked else "Saved, but the test didn't work: " + message, worked)


@router.post("/settings/claude/forget")
def forget_claude(user=Depends(auth.require_user), what: str = Form("")):
    if what in ("oauth_token", "api_key"):
        writer.save_settings(**{what: ""})
    return back("Removed." + (" (The one in .env is still used: delete it there too.)"
                              if writer.settings()[what] else ""))


@router.post("/settings/members")
def add_member(user=Depends(auth.require_user), display_name: str = Form(), username: str = Form(),
               password: str = Form(), password2: str = Form()):
    error = check_new_account(username, display_name, password, password2)
    if error:
        return RedirectResponse(f"/settings?message={quote(error)}#members", status_code=303)
    auth.create_user(username, display_name, password)
    msg = f"Added {display_name.strip()}. They can now log in as {username.strip()}."
    return RedirectResponse(f"/settings?message={quote(msg)}#members", status_code=303)


@router.post("/settings/password")
def change_password(user=Depends(auth.require_user), current: str = Form(), password: str = Form(),
                    password2: str = Form()):
    if auth.authenticate(user["username"], current) is None:
        msg = "Your current password wasn't right."
    elif len(password) < 8:
        msg = "Passwords need at least 8 characters."
    elif password != password2:
        msg = "The two new passwords don't match."
    else:
        auth.set_password(user["id"], password)
        msg = "Password changed."
    return RedirectResponse(f"/settings?message={quote(msg)}#password", status_code=303)
