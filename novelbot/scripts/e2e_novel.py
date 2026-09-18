"""End-to-end check of the *conversion* pipeline on a synthetic novel.

This is the test that matters: it feeds a realistic Chinese novel through the
same code path the bot uses and asserts that the output is a clean,
cascade-free translation of names - and that nothing is left behind.

    python scripts/e2e_novel.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("TELEGRAM_API_ID", "123456")
os.environ.setdefault("TELEGRAM_API_HASH", "0" * 32)
os.environ.setdefault("BOT_TOKEN", "123456:AAA-this-is-only-a-selftest-token-000000")
os.environ.setdefault("SUPABASE_ENABLED", "false")
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="novelbot-e2e-"))

# A synthetic novel. Names repeat the way real prose repeats them, and it
# contains the traps that broke v1: a short latin key inside a longer word,
# a name split across a line, and function characters next to names.
NAMES = ["王云", "李雪", "陈子墨", "林雨柔", "赵天翔", "孙若曦", "周浩然", "吴静怡"]
FILLER = [
    "{a}看着{b}，轻声说道：你终于来了。",
    "{b}摇了摇头，没有回答{a}的问题。",
    "那一夜，{c}独自站在山巅，想着{d}的笑容。",
    "{d}忽然回头，看见{e}正从远处走来。",
    "Elizabeth and Liang and Delhi and Validation stayed untouched.",
    "{a}对{c}说：这件事，只有你我二人知道。",
    "{f}把剑收了起来，转身离开了{g}。",
    "{h}叹了口气，{b}的事情，终究是瞒不住了。",
]


def build_novel() -> str:
    lines = ["第一章 山雨欲来", ""]
    for i in range(900):
        template = FILLER[i % len(FILLER)]
        lines.append(
            template.format(
                a=NAMES[i % len(NAMES)],
                b=NAMES[(i + 1) % len(NAMES)],
                c=NAMES[(i + 2) % len(NAMES)],
                d=NAMES[(i + 3) % len(NAMES)],
                e=NAMES[(i + 4) % len(NAMES)],
                f=NAMES[(i + 5) % len(NAMES)],
                g=NAMES[(i + 6) % len(NAMES)],
                h=NAMES[(i + 7) % len(NAMES)],
            )
        )
    return "\n".join(lines)


def main() -> int:
    from app.core.discovery import augment_mapping, extract_candidates
    from app.core.names import load_pools
    from app.runtime import get_runtime, init_runtime
    from app.services.files import decode_bytes, iter_text_chunks

    results: list[tuple[bool, str]] = []

    def check(name: str, ok: bool, extra: str = "") -> None:
        results.append((bool(ok), name))
        print(f"{'✅' if ok else '❌'} {name}{(' - ' + extra) if extra else ''}")

    import asyncio

    asyncio.run(init_runtime())
    runtime = get_runtime()

    novel = build_novel()
    check("synthetic novel built", len(novel) > 20_000, f"{len(novel):,} characters")

    # ---- pass 1: discovery ------------------------------------------
    pools = load_pools(ROOT / "resources")
    found = extract_candidates(
        novel, [n for n, _ in pools.zh_surnames], threshold=3, max_names=1500
    )
    found_names = {name for name, _ in found}
    missed = [n for n in NAMES if n not in found_names]
    check("every planted character name discovered", not missed,
          f"missed: {missed}" if missed else f"{len(found)} candidates")
    check("discovery did not flood with junk", len(found) < 400, f"{len(found)} candidates")

    # ---- pass 2: merged mapping + conversion ------------------------
    mapping, stats = augment_mapping(runtime.registry, novel, threshold=3, max_names=1500)
    check("mapping augmented", stats["added"] > 0,
          f"base {len(runtime.registry.mapping)} -> merged {len(mapping)} "
          f"(+{stats['added']}) checksum {stats['checksum']}")

    converter = runtime.converter_with(True, mapping, str(stats["checksum"]))
    started = time.perf_counter()
    out = converter.convert(novel)
    elapsed = time.perf_counter() - started
    check("conversion produced replacements", out.replacements > 1000,
          f"{out.replacements:,} replacements / {out.unique_names} distinct names")
    check("conversion is fast enough for the free tier", elapsed < 5.0,
          f"{elapsed:.2f}s for {len(novel):,} chars")

    # ---- correctness assertions ------------------------------------
    leftover = [n for n in NAMES if n in out.text]
    check("no planted Chinese name left in the output", not leftover,
          f"leftover: {leftover}" if leftover else "")

    # The latin trap must be byte-identical.
    trap = "Elizabeth and Liang and Delhi and Validation stayed untouched."
    check("latin words in the novel untouched", trap in out.text)

    # Determinism: same novel, same result.
    mapping2, stats2 = augment_mapping(runtime.registry, novel, threshold=3, max_names=1500)
    out2 = runtime.converter_with(True, mapping2, str(stats2["checksum"])).convert(novel)
    check("conversion is deterministic", out.text == out2.text,
          f"checksum {stats['checksum']} vs {stats2['checksum']}")

    # No cascade: running the converter on its own output must be a no-op.
    second = converter.convert(out.text)
    check("second pass is a no-op (no cascade)", second.replacements == 0,
          f"{second.replacements} extra replacements")

    # Structure preserved: same line count, same length class.
    check("line structure preserved", out.text.count("\n") == novel.count("\n"),
          f"{out.text.count(chr(10))} lines")
    check("output is valid UTF-8 text", isinstance(out.text, str) and len(out.text) > 0)

    # ---- chunked path (what the bot actually runs) ------------------
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as fh:
        fh.write(novel)
        tmp = Path(fh.name)
    text = decode_bytes(tmp.read_bytes())
    out_path = tmp.with_suffix(".out.txt")
    total = 0
    with out_path.open("w", encoding="utf-8", newline="\n") as handle:
        for chunk in iter_text_chunks(tmp, max_chars=4_000_000, chunk_size=150_000):
            res = converter.convert(chunk)
            handle.write(res.text)
            total += res.replacements
    chunked = out_path.read_text(encoding="utf-8")
    check("chunked path == single-shot path", chunked == out.text,
          f"{total:,} replacements over chunks")
    check("chunked output has no leftover names",
          not any(n in chunked for n in NAMES))
    tmp.unlink(missing_ok=True)
    out_path.unlink(missing_ok=True)

    # ---- show a sample ---------------------------------------------
    print("\n--- sample of the converted prose ---")
    for line in out.text.splitlines()[2:7]:
        print("   ", line)
    print("--- name map (discovered) ---")
    for old, new in list(stats["sample"]):
        print(f"    {old}  ->  {new}")

    failed = [n for ok, n in results if not ok]
    print("\n" + "=" * 64)
    print(f"{len(results) - len(failed)}/{len(results)} checks passed")
    if failed:
        for name in failed:
            print(f"  - {name}")
        return 1
    print("End-to-end conversion verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
