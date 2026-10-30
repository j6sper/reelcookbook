"""First-time setup, login, Settings (household members, password) and the Share menu."""
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
def settings(request: Request, user=Depends(auth.require_user), message: str = ""):
    return page(request, "settings.html", user, active="settings", members=auth.list_users(), message=message,
                writer_on=writer.enabled(), model=config.CLAUDE_MODEL, units=config.UNITS)


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
