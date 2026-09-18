"""Entry point: Pyrogram bot + aiohttp web service in ONE process/loop.

Render free tier constraints this file satisfies
----------------------------------------------
* binds ``0.0.0.0:$PORT`` (Render injects ``$PORT``; default 8000 locally);
* answers ``/health`` immediately so Render's probe passes;
* has NO background worker requirement (the free plan cannot run workers);
* writes only to ``/tmp`` (the disk is ephemeral);
* handles SIGTERM/SIGINT and drains running jobs before exit;
* a top-level guard means a crash in one subsystem is logged, not silent.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import sys
from typing import Optional

from aiohttp import web
from pyrogram import idle

from app import __version__
from app.bot.client import build_client, register_commands, set_bot_description, set_menu_button
from app.bot.handlers import help_text, register_handlers
from app.config import get_settings
from app.db import close_supabase, get_supabase
from app.db import repository as repo
from app.errors import BotError
from app.keepalive import start_keepalive, stop_keepalive
from app.logging_setup import get_logger, setup_logging
from app.runtime import init_runtime
from app.services.jobs import init_job_manager
from app.web import create_app

log = get_logger("main")

BANNER = r"""
  _   _                 _    __  __
 | \ | | _____   ____ _ | |  |  \/  | __ _
 |  \| |/ _ \ \ / / _` || |  | |\/| |/ _` |
 | |\  |  __/\ V / (_| || |__| |  | | (_| |
 |_| \_|\___| \_/ \__,_||_____|_|  |_|\__,_|
        Novel Name Converter Bot  v{version}
"""


async def _serve_web(port: int) -> web.AppRunner:
    app = create_app()
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info("web service listening", extra={"bind": f"0.0.0.0:{port}"})
    return runner


async def run() -> None:
    settings = get_settings()
    setup_logging(settings.log_level, settings.log_json)
    print(BANNER.format(version=__version__), flush=True)
    log.info("booting", extra={"config": settings.redacted(), "version": __version__})

    # ---- 1. data + domain layer -------------------------------------
    runtime = await init_runtime()
    manager = init_job_manager()
    supabase = get_supabase()
    await supabase.start()
    if settings.storage_enabled:
        await repo.register_mapping_version(
            runtime.registry.version,
            seed=runtime.registry.seed,
            pair_count=len(runtime.registry.mapping),
            source="bundled+remote",
            checksum=runtime.registry.digest,
        )

    # ---- 2. web service FIRST: Render's health probe must never wait on TG
    runner = await _serve_web(settings.port)

    # ---- 3. Telegram client -----------------------------------------
    client = None
    if settings.bot_enabled:
        client = build_client()
        count = register_handlers(client)
        if count == 0:  # pragma: no cover - defensive
            log.error("no Telegram handlers registered - the bot would be silent")
        await client.start()
        me = await client.get_me()
        log.info("telegram connected", extra={"username": me.username, "id": me.id})

        await register_commands(client)
        await set_menu_button(client, settings.webhook_base)
        await set_bot_description(client, help_text().replace("<b>", "").replace("</b>", ""))
    else:
        log.warning("BOT_ENABLED=false - Telegram client not started (web / Mini App only)")

    # ---- 4. keep-alive + idle until a signal arrives ------------------
    start_keepalive()

    stop_event = asyncio.Event()

    def _request_stop(*_: object) -> None:
        log.info("shutdown signal received")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, _request_stop)

    log.info("bot is running - press Ctrl+C to stop")
    try:
        await stop_event.wait()
    except asyncio.CancelledError:  # pragma: no cover
        pass

    # ---- 5. graceful shutdown ----------------------------------------
    log.info("shutting down")
    async with manager.shutdown():
        pass
    await stop_keepalive()
    if client is not None:
        with contextlib.suppress(Exception):
            await client.stop()
    with contextlib.suppress(Exception):
        await runner.cleanup()
    await close_supabase()
    log.info("bye")


def main() -> int:
    try:
        asyncio.run(run())
        return 0
    except BotError as exc:
        # Configuration problems are printed readably instead of as a traceback.
        setup_logging("INFO", False)
        log.error("startup failed", extra={"code": exc.code, "detail": exc.detail})
        print(f"\n❌ {exc.detail}\n   Fix the environment variables and restart.\n", file=sys.stderr)
        return 2
    except KeyboardInterrupt:  # pragma: no cover
        return 0
    except Exception:  # noqa: BLE001 - last line of defence
        setup_logging("INFO", False)
        log.exception("fatal error")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
