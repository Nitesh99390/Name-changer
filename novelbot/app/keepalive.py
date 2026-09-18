"""Self keep-alive.

Honest note about Render's free tier: an idle Web Service is spun down after
~15 minutes of *no inbound traffic*, and a process that has been spun down
cannot ping itself. So:

* this in-process task is a *cheap* extra that keeps the service warm while it
  is already awake (and proves the loop is alive);
* to actually stay awake 24/7 you must add an **external** monitor
  (cron-job.org / UptimeRobot / GitHub Actions) that GETs
  ``https://<your-service>.onrender.com/health`` every 10 minutes.

Both paths are safe: the endpoint is dependency-free and returns 200 in ~1 ms.
"""

from __future__ import annotations

import asyncio
from typing import Optional

import httpx

from .config import get_settings
from .logging_setup import get_logger

log = get_logger("keepalive")

_task: Optional[asyncio.Task] = None


async def _loop(url: str, interval: int) -> None:
    log.info("keepalive started", extra={"url": url, "interval_s": interval})
    async with httpx.AsyncClient(timeout=10.0) as client:
        while True:
            try:
                await asyncio.sleep(interval)
                response = await client.get(url)
                log.info("keepalive ping", extra={"status": response.status_code})
            except asyncio.CancelledError:
                log.info("keepalive stopped")
                raise
            except Exception as exc:
                # A failed ping is expected when the container is cold - never fatal.
                log.warning("keepalive ping failed", extra={"err": type(exc).__name__})


def start_keepalive() -> Optional[asyncio.Task]:
    global _task
    settings = get_settings()
    if not settings.keepalive_enabled or _task is not None:
        return _task
    base = settings.webhook_base or f"http://127.0.0.1:{settings.port}"
    url = base.rstrip("/") + "/health"
    _task = asyncio.create_task(_loop(url, max(60, settings.keepalive_interval)))
    return _task


async def stop_keepalive() -> None:
    global _task
    if _task is not None:
        _task.cancel()
        try:
            await _task
        except (asyncio.CancelledError, Exception):  # pragma: no cover
            pass
        _task = None
