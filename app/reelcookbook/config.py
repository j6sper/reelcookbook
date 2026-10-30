"""Settings, read from environment variables (see .env)."""
import os
import time
from pathlib import Path
from zoneinfo import ZoneInfo

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
STATIC_DIR = Path(__file__).parent / "static"

APP_NAME = os.environ.get("APP_NAME", "Reel Cookbook").strip() or "Reel Cookbook"
TIMEZONE = os.environ.get("TIMEZONE", "UTC")
TZ = ZoneInfo(TIMEZONE)

# Claude writes the recipes from each post. Without a key, posts are still downloaded and
# transcribed, and you write the recipe up yourself.
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-5-5").strip()

# "metric" (g, ml, °C) or "us" (cups, oz, °F): how Claude writes quantities
UNITS = "us" if os.environ.get("UNITS", "metric").strip().lower() in ("us", "imperial") else "metric"

# Changes every time the app starts, so browsers fetch fresh CSS after an update
VERSION = str(int(time.time()))
