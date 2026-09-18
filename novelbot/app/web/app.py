"""aiohttp application: Mini App, health probe, JSON API.

Endpoints
---------
GET  /                 Mini App (static HTML + telegram-web-app.js)
GET  /health           Render health check (fast, never touches the DB)
GET  /api/health       JSON health + runtime stats
GET  /api/mapping      active mapping version/checksum + sample pairs
POST /api/preview      convert a short sample of text (demo box)
GET  /api/me           the caller's settings + stats   (needs initData)
POST /api/settings     update the caller's settings    (needs initData)
GET  /api/stats        global aggregate counters
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict

from aiohttp import web

from ..config import get_settings
from ..db import repository as repo
from ..errors import BotError
from ..logging_setup import get_logger
from .security import TelegramUser, validate_init_data

log = get_logger("web.app")

STATIC_DIR = Path(__file__).resolve().parent / "static"
PREVIEW_MAX_CHARS = 4000
STARTED = time.monotonic()


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _json(payload: Any, status: int = 200) -> web.Response:
    return web.json_response(payload, status=status, dumps=lambda o: __import__("json").dumps(o, ensure_ascii=False))


def _init_data(request: web.Request) -> str:
    header = request.headers.get("X-Init-Data")
    if header:
        return header
    return request.query.get("initData", "")


def _auth(request: web.Request) -> TelegramUser:
    settings = get_settings()
    user = validate_init_data(_init_data(request), settings.bot_token)
    if user is None:
        raise web.HTTPUnauthorized(
            text='{"ok":false,"error":"invalid_init_data"}',
            content_type="application/json",
        )
    return user


async def _body(request: web.Request) -> Dict[str, Any]:
    try:
        data = await request.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


# --------------------------------------------------------------------------
# handlers
# --------------------------------------------------------------------------
async def index(request: web.Request) -> web.Response:
    page = STATIC_DIR / "index.html"
    if not page.exists():  # pragma: no cover
        return _json({"ok": False, "error": "mini_app_missing"}, status=500)
    return web.FileResponse(page)


async def health(request: web.Request) -> web.Response:
    """Render's health check. Deliberately dependency-free and instant."""
    return web.Response(text="ok", content_type="text/plain")


async def api_health(request: web.Request) -> web.Response:
    from ..runtime import get_runtime
    from ..services.jobs import get_job_manager

    settings = get_settings()
    runtime = get_runtime()
    manager = get_job_manager()
    db_ok = False
    try:
        from ..db import get_supabase

        db_ok = await get_supabase().ping()
    except Exception:  # pragma: no cover
        db_ok = False

    return _json(
        {
            "ok": True,
            "uptime_s": int(time.monotonic() - STARTED),
            "names": runtime.registry.info(),
            "jobs": manager.stats(),
            "db": {"enabled": settings.storage_enabled, "reachable": db_ok},
            "limits": {
                "max_file_mb": settings.max_file_mb,
                "rate_limit_per_hour": settings.rate_limit_per_hour,
                "max_concurrent_jobs": settings.max_concurrent_jobs,
            },
        }
    )


async def api_mapping(request: web.Request) -> web.Response:
    from ..runtime import get_runtime

    runtime = get_runtime()
    mapping = runtime.registry.mapping
    sample = [
        {"from": old, "to": new}
        for old, new in list(mapping.items())[:40]
    ]
    return _json({"ok": True, "info": runtime.registry.info(), "sample": sample})


async def api_preview(request: web.Request) -> web.Response:
    """Convert a short snippet so the user can see the mapping before sending a file.

    Uses the same two-pass pipeline as the bot (discover names in the text,
    merge them into the curated mapping, replace in one pass), so what the user
    sees here is exactly what they get for the real file.
    """
    from ..core.discovery import augment_mapping
    from ..runtime import get_runtime

    body = await _body(request)
    text = str(body.get("text") or "")[:PREVIEW_MAX_CHARS]
    if not text.strip():
        return _json({"ok": False, "error": "empty_text"}, status=400)
    settings = get_settings()
    runtime = get_runtime()
    allow_latin = bool(body.get("latin_names", True))
    keep_original = bool(body.get("keep_original", False))

    mapping, stats = augment_mapping(
        runtime.registry,
        text,
        threshold=1,
        max_names=settings.max_pairs,
    )
    converter = runtime.converter_with(
        allow_latin, mapping, str(stats.get("checksum") or runtime.registry.digest)
    )
    result = converter.convert(text, keep_original=keep_original)
    return _json(
        {
            "ok": True,
            "output": result.text,
            "replacements": result.replacements,
            "unique_names": result.unique_names,
            "elapsed_ms": result.elapsed_ms,
            "discovered": int(stats.get("added") or 0),
            "checksum": str(stats.get("checksum") or runtime.registry.digest),
            "top": [{"from": o, "to": n, "hits": h} for o, n, h in result.top_names],
        }
    )


async def api_me(request: web.Request) -> web.Response:
    user = _auth(request)
    settings = get_settings()
    await repo.touch_user(
        user.id,
        username=user.username,
        first_name=user.first_name,
        last_name=user.last_name,
        language_code=user.language_code,
    )
    from ..services.ratelimit import get_rate_limiter

    user_settings = await repo.get_user_settings(user.id)
    history = await repo.user_job_history(user.id, limit=10)
    remaining = await get_rate_limiter().remaining(user.id)
    return _json(
        {
            "ok": True,
            "user": {
                "id": user.id,
                "first_name": user.first_name,
                "username": user.username,
                "language_code": user.language_code,
            },
            "settings": user_settings,
            "quota": {
                "limit": settings.rate_limit_per_hour,
                "remaining": remaining,
            },
            "history": history,
            "return_to_bot": settings.return_to_bot,
            "db": settings.storage_enabled,
        }
    )


async def api_settings(request: web.Request) -> web.Response:
    user = _auth(request)
    body = await _body(request)
    incoming = body.get("settings") if isinstance(body.get("settings"), dict) else body
    saved = await repo.save_user_settings(user.id, incoming or {})
    return _json({"ok": True, "settings": saved})


async def api_stats(request: web.Request) -> web.Response:
    stats = await repo.global_stats()
    return _json({"ok": True, "stats": stats})


# --------------------------------------------------------------------------
# error handling
# --------------------------------------------------------------------------
@web.middleware
async def error_middleware(request: web.Request, handler):
    try:
        return await handler(request)
    except web.HTTPException as exc:
        if exc.status >= 500:
            log.warning("http error", extra={"path": request.path, "status": exc.status})
        raise
    except BotError as exc:
        log.warning("domain error", extra={"path": request.path, "code": exc.code})
        return _json({"ok": False, "error": exc.code}, status=400)
    except Exception:  # noqa: BLE001 - the web layer must never 500 silently
        log.exception("unhandled http error", extra={"path": request.path})
        return _json({"ok": False, "error": "internal_error"}, status=500)


@web.middleware
async def timing_middleware(request: web.Request, handler):
    started = time.monotonic()
    response = await handler(request)
    elapsed = int((time.monotonic() - started) * 1000)
    if request.path.startswith("/api"):
        log.info(
            "api call",
            extra={
                "path": request.path,
                "method": request.method,
                "status": response.status,
                "ms": elapsed,
            },
        )
    response.headers["X-Response-Time"] = f"{elapsed}ms"
    return response


# --------------------------------------------------------------------------
# factory
# --------------------------------------------------------------------------
def create_app() -> web.Application:
    app = web.Application(middlewares=[error_middleware, timing_middleware])
    app.router.add_get("/", index)
    app.router.add_get("/health", health)
    app.router.add_get("/ping", health)
    app.router.add_get("/api/health", api_health)
    app.router.add_get("/api/mapping", api_mapping)
    app.router.add_post("/api/preview", api_preview)
    app.router.add_get("/api/me", api_me)
    app.router.add_post("/api/settings", api_settings)
    app.router.add_get("/api/stats", api_stats)
    if STATIC_DIR.exists():
        app.router.add_static("/static/", STATIC_DIR, show_index=False)
    log.info("aiohttp app created", extra={"static": str(STATIC_DIR)})
    return app
