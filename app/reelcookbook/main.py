"""Reel Cookbook: starts everything up and connects the pieces."""
import asyncio
import logging
import mimetypes
from contextlib import asynccontextmanager
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from . import accounts, auth, config, db, recipes, social, writer

mimetypes.add_type("font/woff2", ".woff2")   # so browsers know the font files are fonts

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("reelcookbook")


async def run_every(seconds, job):
    """Run a background job now, then every `seconds`. Errors are logged, never fatal."""
    while True:
        try:
            await job()
        except Exception:
            log.exception("Background job %s failed; will retry", job.__name__)
        await asyncio.sleep(seconds)


@asynccontextmanager
async def lifespan(app):
    db.migrate(db.CORE_MIGRATIONS + auth.MIGRATIONS + recipes.MIGRATIONS + social.MIGRATIONS)
    tasks = [asyncio.create_task(run_every(seconds, job)) for seconds, job in social.JOBS]
    tasks.append(asyncio.create_task(writer.run()))
    yield
    for task in tasks:
        task.cancel()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=config.STATIC_DIR), name="static")
app.include_router(accounts.router)
app.include_router(social.router)
app.include_router(recipes.router)


@app.exception_handler(auth.LoginRequired)
def login_required(request: Request, exc):
    next_url = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    return RedirectResponse(f"/login?next={quote(next_url)}", status_code=303)


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/manifest.webmanifest")
def manifest():
    return JSONResponse({
        "name": config.APP_NAME,
        "short_name": config.APP_NAME,
        "start_url": "/recipes",
        "display": "standalone",
        "background_color": "#0b0c10",
        "theme_color": "#0b0c10",
        "icons": [
            {"src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png"},
        ],
        # Android: the app shows up in the Share menu of Instagram, TikTok and the browser
        "share_target": {"action": "/share", "method": "GET", "params": {"title": "title", "text": "text", "url": "url"}},
    }, media_type="application/manifest+json")


@app.get("/sw.js")
def service_worker():
    # Installable apps need a service worker; this one does nothing else
    return Response("self.addEventListener('fetch', () => {});", media_type="text/javascript")
