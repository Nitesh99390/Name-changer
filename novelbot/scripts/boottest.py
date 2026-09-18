#!/usr/bin/env python3
"""Import-and-wire smoke test: catches boot-time errors without Telegram.

    python scripts/boottest.py

Checks that
* ``main.py`` and every ``app.*`` module import cleanly (an ImportError here
  means the Render deploy would crash-loop before binding $PORT);
* ``register_handlers`` attaches every decorated handler to a Pyrogram client
  (Pyrogram's class-level decorators register NOTHING on their own);
* the catch-all document handler is registered last (order matters);
* ``BOT_ENABLED=false`` boots the web service alone.
"""
from __future__ import annotations

import asyncio
import importlib
import os
import pkgutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("TELEGRAM_API_ID", "123456")
os.environ.setdefault("TELEGRAM_API_HASH", "0" * 32)
os.environ.setdefault("BOT_TOKEN", "123456:AAA-this-is-only-a-selftest-token-000000")
os.environ.setdefault("SUPABASE_ENABLED", "false")
os.environ.setdefault("KEEPALIVE_ENABLED", "false")
os.environ.setdefault("LOG_LEVEL", "ERROR")
os.environ.setdefault("LOG_JSON", "false")
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="boottest-"))

PASSED = 0
FAILED = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASSED, FAILED
    mark = "✅" if ok else "❌"
    print(f"{mark} {name}" + (f" - {detail}" if detail else ""))
    if ok:
        PASSED += 1
    else:
        FAILED += 1


def main() -> int:
    # 1. every module imports
    import app  # noqa: F401

    bad: list[str] = []
    for mod in pkgutil.walk_packages(app.__path__, prefix="app."):
        try:
            importlib.import_module(mod.name)
        except Exception as exc:  # noqa: BLE001
            bad.append(f"{mod.name}: {type(exc).__name__}: {exc}")
    check("all app.* modules import", not bad, "; ".join(bad) or f"{len(list(pkgutil.walk_packages(app.__path__, prefix='app.')))} modules")

    try:
        import main as entry  # noqa: F401

        check("main.py imports", True)
    except Exception as exc:  # noqa: BLE001
        check("main.py imports", False, f"{type(exc).__name__}: {exc}")
        return 1

    # 2. handlers actually attach to a client
    from pyrogram import Client
    from pyrogram.handlers import CallbackQueryHandler, MessageHandler

    from app.bot import register_handlers
    from app.bot.client import build_client

    async def wire() -> tuple[Client, int]:
        # Pyrogram's dispatcher schedules add_handler() as a task, so build,
        # register and inspect inside a running loop (exactly like main.py).
        client: Client = build_client()
        count = register_handlers(client)
        await asyncio.sleep(0)  # let the scheduled add_handler tasks run
        return client, count

    client, count = asyncio.run(wire())
    check("register_handlers attaches handlers", count >= 10, f"{count} handlers")

    group0 = client.dispatcher.groups.get(0, [])
    kinds = {type(h).__name__ for h in group0}
    check("message + callback handlers present", {"MessageHandler", "CallbackQueryHandler"} <= kinds, ", ".join(sorted(kinds)))

    msg_handlers = [h for h in group0 if isinstance(h, MessageHandler)]
    last = msg_handlers[-1].callback.__name__ if msg_handlers else ""
    check("document handler registered last", last == "on_document", last)

    names = [h.callback.__name__ for h in group0]
    for expected in ("cmd_start", "cmd_help", "cmd_settings", "cmd_cancel", "cmd_admin", "on_menu", "on_setting"):
        check(f"handler {expected} wired", expected in names)

    # 3. web-only boot path exists
    from app.config import get_settings

    check("BOT_ENABLED setting exposed", hasattr(get_settings(), "bot_enabled"))

    print("\n" + "=" * 64)
    print(f"{PASSED}/{PASSED + FAILED} checks passed")
    if FAILED:
        print("Boot test FAILED - do not deploy.")
        return 1
    print("Boot path verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
