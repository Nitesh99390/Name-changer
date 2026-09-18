#!/usr/bin/env python3
"""Boot the aiohttp app in-process (no Telegram, no Supabase) and hit the routes.

    python scripts/webtest.py
"""
import os, sys, tempfile, json, asyncio
from pathlib import Path

# Make the app importable when run from the repo root (or anywhere else).
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("TELEGRAM_API_ID","123456")
os.environ.setdefault("TELEGRAM_API_HASH","0"*32)
os.environ.setdefault("BOT_TOKEN","123456:AAA-this-is-only-a-selftest-token-000000")
os.environ.setdefault("SUPABASE_ENABLED","false")
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="webtest-"))

from aiohttp.test_utils import TestClient, TestServer
from app.logging_setup import setup_logging
from app.runtime import init_runtime
from app.services.jobs import init_job_manager
from app.web import create_app

async def main():
    setup_logging("ERROR", False)
    await init_runtime(); init_job_manager()
    app = create_app()
    async with TestClient(TestServer(app)) as c:
        r = await c.get("/health");       assert r.status==200 and (await r.text())=="ok", r.status
        print("  /health                 ->", r.status, (await r.text()))
        r = await c.get("/api/health");   d = await r.json(); assert d["ok"] and d["names"]["pairs"]>50
        print("  /api/health             ->", r.status, "pairs=", d["names"]["pairs"], "db=", d["db"])
        r = await c.get("/api/mapping");  d = await r.json(); assert len(d["sample"])==40
        print("  /api/mapping            ->", r.status, "sample=", len(d["sample"]), "checksum=", d["info"]["checksum"])
        r = await c.post("/api/preview", json={"text":"\u738b\u4e91\u770b\u7740\u674e\u96ea\u8bf4\uff1a\u4f60\u7ec8\u4e8e\u6765\u4e86\u3002"})
        d = await r.json()
        print("  /api/preview            ->", r.status, repr(d["output"]), d["replacements"], "hits")
        assert d["ok"] and d["replacements"]>0
        r = await c.post("/api/preview", json={}); assert r.status==400
        print("  /api/preview (empty)    ->", r.status, "(correctly rejected)")
        r = await c.get("/api/stats"); d = await r.json(); assert d["ok"]
        print("  /api/stats              ->", r.status, "storage=", d["stats"]["storage"])
        r = await c.get("/api/me"); assert r.status==401
        print("  /api/me (no initData)   ->", r.status, "(correctly unauthorized)")
        r = await c.post("/api/settings", json={"latin_names":False}); assert r.status==401
        print("  /api/settings (no auth) ->", r.status, "(correctly unauthorized)")
        r = await c.get("/"); assert r.status==200 and b"telegram-web-app.js" in (await r.read())
        print("  /  (Mini App)           ->", r.status, "telegram-web-app.js present")
        r = await c.get("/definitely-not-a-route"); assert r.status==404
        print("  404 handling            ->", r.status)
    print("\nALL WEB ROUTE CHECKS PASSED")

asyncio.run(main())
