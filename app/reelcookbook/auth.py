"""Household logins: users, password hashing and login sessions."""
import asyncio
import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque

from fastapi import Request

from .db import get_db

COOKIE_NAME = "reelcookbook_session"
SESSION_DAYS = 365

MIGRATIONS = [
    ("auth-001-users-sessions", """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            username TEXT NOT NULL UNIQUE COLLATE NOCASE,
            display_name TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE sessions (
            token_hash TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            expires_at REAL NOT NULL
        );
    """),
]


class LoginRequired(Exception):
    """Raised when a page needs a logged-in user; main.py turns it into a redirect."""


def hash_password(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt$16384$8$1${salt.hex()}${digest.hex()}"


def check_password(password, stored):
    try:
        _, n, r, p, salt, digest = stored.split("$")
        test = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p))
    except ValueError:
        return False
    return hmac.compare_digest(test.hex(), digest)


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


_token_hash = token_hash


def user_count():
    with get_db() as db:
        return db.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def create_user(username, display_name, password):
    with get_db() as db:
        db.execute(
            "INSERT INTO users (username, display_name, password_hash) VALUES (?, ?, ?)",
            (username.strip(), display_name.strip(), hash_password(password)),
        )


def authenticate(username, password):
    with get_db() as db:
        user = db.execute("SELECT * FROM users WHERE username = ?", (username.strip(),)).fetchone()
    if user and check_password(password, user["password_hash"]):
        return user
    return None


def set_password(user_id, password):
    with get_db() as db:
        db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(password), user_id))


def list_users():
    with get_db() as db:
        return db.execute("SELECT id, username, display_name FROM users ORDER BY display_name").fetchall()


def start_session(response, request, user_id):
    token = secrets.token_urlsafe(32)
    with get_db() as db:
        db.execute("DELETE FROM sessions WHERE expires_at < ?", (time.time(),))
        db.execute(
            "INSERT INTO sessions (token_hash, user_id, expires_at) VALUES (?, ?, ?)",
            (_token_hash(token), user_id, time.time() + SESSION_DAYS * 86400),
        )
    response.set_cookie(
        COOKIE_NAME, token,
        max_age=SESSION_DAYS * 86400,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
    )


def end_session(request, response):
    token = request.cookies.get(COOKIE_NAME)
    if token:
        with get_db() as db:
            db.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))
    response.delete_cookie(COOKIE_NAME)


def get_user(request: Request):
    """The logged-in user, or None."""
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    with get_db() as db:
        return db.execute(
            """SELECT users.id, users.username, users.display_name FROM sessions
               JOIN users ON users.id = sessions.user_id
               WHERE sessions.token_hash = ? AND sessions.expires_at > ?""",
            (_token_hash(token), time.time()),
        ).fetchone()


def require_user(request: Request):
    """FastAPI dependency for pages that need a login."""
    user = get_user(request)
    if user is None:
        raise LoginRequired()
    return user


# --- Wrong-password protection ---

LOCK_AFTER = 5            # failed attempts...
LOCK_WINDOW = 15 * 60     # ...within 15 minutes locks that username (and that address) for 15 minutes
_failures = defaultdict(deque)


def _recent(key):
    q = _failures[key]
    while q and q[0] < time.time() - LOCK_WINDOW:
        q.popleft()
    return q


async def login_attempt(request, username, password):
    """(user, None) if the password is right; (None, message) if not or while locked out."""
    keys = ["user:" + username.strip().lower(), "ip:" + (request.client.host if request.client else "?")]
    if any(len(_recent(k)) >= LOCK_AFTER for k in keys):
        await asyncio.sleep(1)
        return None, "Too many wrong attempts. Please wait 15 minutes and try again."
    user = await asyncio.to_thread(authenticate, username, password)
    if user is None:
        for k in keys:
            _recent(k).append(time.time())
        await asyncio.sleep(1)   # slows down guessing
        return None, "Wrong username or password."
    _failures.pop(keys[0], None)
    return user, None
