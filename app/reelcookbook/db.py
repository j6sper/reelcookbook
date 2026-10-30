"""SQLite database: one file at data/reelcookbook.db."""
import json
import sqlite3
from contextlib import contextmanager

from .config import DATA_DIR

DB_PATH = DATA_DIR / "reelcookbook.db"

CORE_MIGRATIONS = [
    ("core-001-settings", """
        CREATE TABLE settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
    """),
]


def connect():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def get_db():
    """Use as `with get_db() as db:` — commits on success, rolls back on error."""
    conn = connect()
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def migrate(migrations):
    """Run each (name, sql) migration once, in order."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with get_db() as db:
        db.execute("PRAGMA journal_mode = WAL")
        db.execute("""CREATE TABLE IF NOT EXISTS migrations (
            name TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
        done = {row["name"] for row in db.execute("SELECT name FROM migrations")}
    for name, sql in migrations:
        if name in done:
            continue
        # The migration and its record are saved together, or not at all.
        record = "INSERT INTO migrations (name) VALUES ('{}');".format(name.replace("'", "''"))
        conn = connect()
        try:
            conn.executescript(f"BEGIN;\n{sql}\n{record}\nCOMMIT;")
        finally:
            conn.close()


def setting(key, default=None):
    """A stored app setting (as JSON)."""
    with get_db() as db:
        row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def set_setting(key, value):
    with get_db() as db:
        db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, json.dumps(value)))
