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

# Claude writes the recipes from each post. These are normally set in the app (Settings → Claude);
# values here are only used until something is saved there. Without either, posts are still
# downloaded and transcribed, and you write the recipe up yourself.
CLAUDE_CODE_OAUTH_TOKEN = "".join(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").split())   # from `claude setup-token`
ANTHROPIC_API_KEY = "".join(os.environ.get("ANTHROPIC_API_KEY", "").split())               # from the Claude Console
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "").strip() or "claude-opus-5-5"

# "metric" (g, ml, °C) or "us" (cups, oz, °F): how Claude writes quantities
UNITS = "us" if os.environ.get("UNITS", "metric").strip().lower() in ("us", "imperial") else "metric"

# Changes every time the app starts, so browsers fetch fresh CSS after an update
VERSION = str(int(time.time()))
