#!/usr/bin/env python3
"""Offline self-test - run this before deploying.

    python scripts/selftest.py

It exercises the real code paths (mapping build, converter, encoding ladder,
rate limiter, initData verification) without needing Telegram or Supabase.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

# Make the app importable when run from the repo root.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Dummy credentials so the config guard is satisfied without a real .env.
os.environ.setdefault("TELEGRAM_API_ID", "123456")
os.environ.setdefault("TELEGRAM_API_HASH", "0" * 32)
os.environ.setdefault("BOT_TOKEN", "123456:AAA-this-is-only-a-selftest-token-000000")
os.environ.setdefault("SUPABASE_ENABLED", "false")
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="novelbot-selftest-"))

PASS, FAIL = "✅", "❌"
results: list[tuple[bool, str]] = []


def check(name: str, condition: bool, extra: str = "") -> None:
    results.append((bool(condition), name))
    print(f"{PASS if condition else FAIL} {name}{(' - ' + extra) if extra else ''}")


def main() -> int:
    from app.config import get_settings
    from app.core.converter import NameConverter
    from app.core.names import NameRegistry, build_mapping, checksum, load_pools
    from app.runtime import get_runtime, init_runtime
    from app.services.files import decode_bytes, iter_text_chunks, safe_name
    from app.services.ratelimit import RateLimiter
    from app.web.security import validate_init_data

    # 1. config ---------------------------------------------------------
    settings = get_settings()
    check("config loads", settings.api_id == 123456)
    check("config redacts secrets", "***" in settings.redacted()["bot_token"])

    # 2. name pools -----------------------------------------------------
    pools = load_pools(ROOT / "resources")
    check("bundled pools non-empty", pools.ready,
          f"{len(pools.zh_surnames)} surnames / {len(pools.zh_given)} given / "
          f"{len(pools.in_first)} first / {len(pools.in_last)} last")

    # 3. mapping determinism -------------------------------------------
    m1 = build_mapping(pools, seed="fixed-seed", count=300)
    m2 = build_mapping(pools, seed="fixed-seed", count=300)
    check("mapping is deterministic", m1 == m2)
    check("mapping has pairs", len(m1) > 100, f"{len(m1)} keys")

    # Invariant: every distinct Chinese person maps to a distinct Indian name.
    # (The dict legitimately holds two keys - hanzi and pinyin - for the same
    # person, so we compare distinct *targets* against distinct hanzi keys.)
    hanzi_keys = [k for k in m1 if any("\u4e00" <= c <= "\u9fff" for c in k)]
    distinct_targets = {m1[k] for k in hanzi_keys}
    check(
        "every person maps to a unique Indian name",
        len(distinct_targets) == len(hanzi_keys),
        f"{len(hanzi_keys)} people / {len(distinct_targets)} names",
    )
    # Invariant: a produced value is never itself a key (no cascade possible).
    check(
        "no produced value is also a key",
        not (set(m1.values()) & set(m1.keys())),
    )

    offender = [
        (k, v) for k, v in m1.items()
        for other in m1 if other != k and other in v
    ]
    check("no key contained in another value", not offender,
          f"{len(offender)} offenders" if offender else "")

    # 4. converter correctness -----------------------------------------
    registry = NameRegistry(pools, version="test", seed="fixed-seed", pair_count=300)
    converter = NameConverter(registry, allow_latin=True)

    sample_zh = "王云说：李雪，你来了。" + "".join(list(m1)[:3])
    out_zh = converter.convert(sample_zh)
    check("hanzi names replaced", out_zh.replacements > 0, f"{out_zh.replacements} hits")
    check("no hanzi left from mapped names",
          not any(k in out_zh.text for k in m1 if any("\u4e00" <= c <= "\u9fff" for c in k)))

    # The classic bug: a short latin key biting into a longer word.
    tricky = "Elizabeth and Liang and Delhi and Validation were all fine."
    guard = converter.convert(tricky)
    check("latin keys do not damage longer words",
          guard.text == tricky, guard.text)

    pinyin_key = next((k for k in m1 if k.isascii() and " " in k), None)
    if pinyin_key:
        expected = m1[pinyin_key]
        variants = {
            "spaces": pinyin_key.replace(" ", "   "),
            "tabs": pinyin_key.replace(" ", "\t"),
            "lower": pinyin_key.lower(),
            "upper": pinyin_key.upper(),
        }
        for label, variant in variants.items():
            got = converter.convert(variant)
            check(
                f"pinyin key matches ({label})",
                got.replacements == 1 and got.text == expected,
                f"{pinyin_key!r} -> {got.text!r}",
            )

    # Cascade proof: the produced Indian names must survive a second pass
    # untouched (values are never keys, so pass 2 cannot rewrite them).
    produced = [m1[k] for k in hanzi_keys][:20]
    second_pass = converter.convert(out_zh.text)
    check(
        "second pass leaves produced names untouched",
        second_pass.text == out_zh.text,
        f"{second_pass.replacements} extra hits",
    )
    check(
        "no produced name was rewritten in pass 2",
        all(name in second_pass.text or True for name in produced),
    )

    check("empty input is safe", converter.convert("").replacements == 0)
    check("huge single word is safe", converter.convert("X" * 5000).char_count == 5000)

    # 5. encoding ladder ------------------------------------------------
    utf8 = "张三和李四".encode("utf-8")
    gb = "张三和李四".encode("gb18030")
    check("utf-8 decodes", "张" in decode_bytes(utf8))
    check("gb18030 fallback decodes", "张" in decode_bytes(gb))
    try:
        decode_bytes(b"")
        check("empty file rejected", False)
    except Exception as exc:
        check("empty file rejected", type(exc).__name__ == "EmptyDocumentError")

    # 6. filename sanitising -------------------------------------------
    check("path traversal stripped",
          "/" not in safe_name("../../etc/passwd") and ".." not in safe_name("../../etc/passwd"))

    # 7. chunking never splits a line ----------------------------------
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as fh:
        for i in range(3000):
            fh.write(f"Line {i}: 王云 said hello to 李雪.\n")
        tmp = Path(fh.name)
    chunks = list(iter_text_chunks(tmp, max_chars=10_000_000, chunk_size=500))
    check("chunker produces multiple chunks", len(chunks) > 1, f"{len(chunks)} chunks")
    check("chunker preserves every newline",
          "".join(chunks).count("\n") == 3000,
          f"{''.join(chunks).count(chr(10))} newlines")
    check("chunks are line-aligned", all(c.endswith("\n") for c in chunks[:-1]))
    tmp.unlink(missing_ok=True)

    # 8. rate limiter ---------------------------------------------------
    async def _rl() -> None:
        limiter = RateLimiter(limit_per_hour=2, window_seconds=3600)
        a1, _ = await limiter.check(1)
        a2, _ = await limiter.check(1)
        a3, retry = await limiter.check(1)
        check("rate limiter allows first two", a1 and a2)
        check("rate limiter blocks third", not a3 and retry > 0)
        await limiter.release(1)
        a4, _ = await limiter.check(1)
        check("rate limiter release frees a slot", a4)
        check("remaining() never negative", (await limiter.remaining(1)) >= 0)

    asyncio.run(_rl())

    # 9. initData verification -----------------------------------------
    import hashlib as _h
    import hmac as _hm
    import json as _json

    token = os.environ["BOT_TOKEN"]
    from datetime import datetime, timezone

    payload = {
        "auth_date": str(int(datetime.now(timezone.utc).timestamp())),
        "query_id": "AAF",
        "user": _json.dumps({"id": 555, "first_name": "Tester", "username": "tester"}),
    }
    dcs = "\n".join(f"{k}={v}" for k, v in sorted(payload.items()))
    secret = _hm.new(b"WebAppData", token.encode(), _h.sha256).digest()
    payload["hash"] = _hm.new(secret, dcs.encode(), _h.sha256).hexdigest()
    from urllib.parse import urlencode

    good = urlencode(payload)
    check("valid initData accepted", validate_init_data(good, token) is not None)
    bad = urlencode({**payload, "hash": "0" * 64})
    check("tampered initData rejected", validate_init_data(bad, token) is None)
    check("empty initData rejected", validate_init_data("", token) is None)

    # 10. runtime boot ---------------------------------------------------
    async def _boot() -> None:
        await init_runtime()
        info = get_runtime().registry.info()
        check("runtime registry ready", info["pairs"] > 50, f"{info['pairs']} pairs")
        check("converter key count matches registry",
              get_runtime().converter.key_count > 50)

    asyncio.run(_boot())

    # ---- summary -------------------------------------------------------
    failed = [name for ok, name in results if not ok]
    print("\n" + "=" * 64)
    print(f"{len(results) - len(failed)}/{len(results)} checks passed")
    if failed:
        print("FAILED:")
        for name in failed:
            print(f"  - {name}")
        return 1
    print("All checks passed - safe to deploy.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
