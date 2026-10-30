"""Shared Jinja2 templates and display helpers."""
from datetime import datetime

from fastapi.templating import Jinja2Templates

from . import config

templates = Jinja2Templates(directory=config.STATIC_DIR.parent / "templates")


def now():
    return datetime.now(config.TZ)


def from_timestamp(ts):
    """A stored Unix time → 'Today 9:41 pm' / 'Wed 15 Oct 9:41 pm'"""
    dt = datetime.fromtimestamp(ts, config.TZ)
    days = (dt.date() - now().date()).days
    day = "Today" if days == 0 else "Yesterday" if days == -1 else f"{dt:%a} {dt.day} {dt:%b}"
    return f"{day} {dt:%I:%M}".replace(" 0", " ") + (" am" if dt.hour < 12 else " pm")


def page(request, name, user=None, **context):
    """Render a full page (with the navigation when logged in)."""
    return templates.TemplateResponse(request, name, {"user": user, **context})


templates.env.globals.update(version=config.VERSION, app_name=config.APP_NAME)
templates.env.filters.update(from_timestamp=from_timestamp)
