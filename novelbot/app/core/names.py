"""Name pools + deterministic mapping registry.

Why the old approach was broken
-------------------------------
The original code did ``content.replace(old, new)`` in a plain loop over a
dict. Two fatal problems:

1. **Order matters and dict order is arbitrary** - ``王`` -> ``Raj`` followed by
   a later key that starts with ``Raj`` re-replaces the *replacement* text
   (replacement chaining / cascading corruption).
2. **Substring replacement** - ``Li`` inside ``Liang`` / ``Elizabeth`` was
   rewritten mid-word, destroying the prose.

This module fixes both: the mapping is built once, keys are deduplicated and
never appear as another key's value, and the replacer is a single
longest-match-first regex pass (see :mod:`app.core.converter`).

Offline-safe by design: bundled TSV/TXT resources are the primary source. The
remote URLs are *optional extras* - a dead URL is skipped, never fatal (the
original hardcoded two GitHub raw URLs that both return HTTP 404).
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Sequence, Tuple

import httpx

from ..errors import NameDataError
from ..logging_setup import get_logger

log = get_logger("core.names")

CJK_RANGES: Tuple[Tuple[int, int], ...] = (
    (0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xF900, 0xFAFF),
    (0x20000, 0x2A6DF), (0x2F800, 0x2FA1F),
)


def has_cjk(text: str) -> bool:
    return any(
        any(lo <= ord(ch) <= hi for lo, hi in CJK_RANGES) for ch in text
    )


@dataclass
class NamePools:
    zh_surnames: List[Tuple[str, str]] = field(default_factory=list)   # (hanzi, pinyin)
    zh_given: List[Tuple[str, str]] = field(default_factory=list)
    in_first: List[str] = field(default_factory=list)
    in_last: List[str] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return bool(self.zh_surnames and self.zh_given and self.in_first)

    @property
    def max_pairs(self) -> int:
        return min(
            len(self.zh_surnames) * len(self.zh_given),
            len(self.in_first) * max(1, len(self.in_last)),
        )


def _read_lines(path: Path) -> List[str]:
    if not path.exists():
        return []
    raw = path.read_text(encoding="utf-8", errors="ignore")
    return [line.strip() for line in raw.splitlines() if line.strip() and not line.startswith("#")]


def _read_tsv(path: Path) -> List[Tuple[str, str]]:
    pairs: List[Tuple[str, str]] = []
    for line in _read_lines(path):
        parts = [p.strip() for p in line.replace(",", "\t").split("\t") if p.strip()]
        if not parts:
            continue
        pairs.append((parts[0], parts[1] if len(parts) > 1 else parts[0]))
    return pairs


def load_pools(resources_dir: Path) -> NamePools:
    """Load bundled resources (primary source)."""
    pools = NamePools(
        zh_surnames=_read_tsv(resources_dir / "zh_surnames.tsv"),
        zh_given=_read_tsv(resources_dir / "zh_given.tsv"),
        in_first=_read_lines(resources_dir / "in_first.txt"),
        in_last=_read_lines(resources_dir / "in_last.txt"),
    )
    log.info(
        "name pools loaded",
        extra={
            "zh_surnames": len(pools.zh_surnames),
            "zh_given": len(pools.zh_given),
            "in_first": len(pools.in_first),
            "in_last": len(pools.in_last),
            "capacity": pools.max_pairs,
        },
    )
    return pools


async def merge_remote_pools(pools: NamePools, urls: Sequence[str], timeout: float = 6.0) -> int:
    """Optionally append extra names from remote URLs. Failures are skipped."""
    added = 0
    if not urls:
        return 0
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        for url in urls:
            try:
                response = await client.get(url)
                if response.status_code != 200:
                    log.warning("remote name source skipped", extra={"url": url, "status": response.status_code})
                    continue
                lines = [ln.strip() for ln in response.text.splitlines() if ln.strip()]
                if not lines:
                    continue
                target = pools.in_first if len(pools.in_first) <= len(pools.zh_given) else pools.in_last
                known = set(target)
                fresh = [ln for ln in lines if ln not in known and len(ln) < 40]
                target.extend(fresh)
                added += len(fresh)
                log.info("remote name source merged", extra={"url": url, "added": len(fresh)})
            except Exception as exc:
                log.warning("remote name source failed", extra={"url": url, "err": type(exc).__name__})
    return added


def build_mapping(
    pools: NamePools,
    *,
    seed: str,
    count: int = 400,
    include_latin: bool = True,
) -> Dict[str, str]:
    """Build a deterministic, collision-free ``old -> new`` mapping.

    Guarantees:
      * deterministic for a given ``seed`` (same novel in => same novel out)
      * every value is unique, so no replacement can cascade into another key
      * keys are unique and include both the Hanzi and the pinyin form
    """
    if not pools.ready:
        raise NameDataError("name pools are empty")

    rng = random.Random(seed)
    total = max(1, min(count, pools.max_pairs))

    # Deterministic combinatorial expansion of Chinese names.
    zh_names: List[Tuple[str, str]] = []
    seen_zh: set[str] = set()
    guard = 0
    while len(zh_names) < total and guard < total * 40:
        guard += 1
        surname, s_py = rng.choice(pools.zh_surnames)
        given, g_py = rng.choice(pools.zh_given)
        hanzi = f"{surname}{given}"
        if hanzi in seen_zh:
            continue
        seen_zh.add(hanzi)
        zh_names.append((hanzi, f"{s_py} {g_py}"))

    # Deterministic Indian names (unique full names).
    indian: List[str] = []
    seen_in: set[str] = set()
    guard = 0
    while len(indian) < len(zh_names) and guard < len(zh_names) * 40:
        guard += 1
        first = rng.choice(pools.in_first)
        last = rng.choice(pools.in_last) if pools.in_last else ""
        full = f"{first} {last}".strip()
        if full in seen_in:
            continue
        seen_in.add(full)
        indian.append(full)

    mapping: Dict[str, str] = {}
    used_values: set[str] = set()

    for (hanzi, pinyin), indian_name in zip(zh_names, indian):
        if indian_name in used_values:
            continue
        used_values.add(indian_name)
        mapping[hanzi] = indian_name
        if include_latin:
            # Pinyin key - only when it is not already taken by another key.
            if pinyin not in mapping and pinyin not in used_values:
                mapping[pinyin] = indian_name
                used_values.add(pinyin)
            # Given-name-only pinyin is a very common novel pattern ("Wei said").
            given_only = pinyin.split(" ")[-1]
            if given_only not in mapping and given_only not in used_values and len(given_only) >= 2:
                mapping[given_only] = indian_name.split(" ")[0]
                used_values.add(given_only)

    # Hard safety net: no mapping *value* may contain another mapping *key*.
    keys = set(mapping)
    mapping = {
        k: v
        for k, v in mapping.items()
        if not any(other != k and other in v for other in keys)
    }
    log.info(
        "mapping built",
        extra={"seed": seed, "pairs": len(mapping), "checksum": checksum(mapping)},
    )
    return mapping


def checksum(mapping: Dict[str, str]) -> str:
    digest = hashlib.sha256()
    for key in sorted(mapping):
        digest.update(key.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(mapping[key].encode("utf-8"))
        digest.update(b"\x01")
    return digest.hexdigest()[:16]


class NameRegistry:
    """Holds the active mapping and can regenerate it on demand."""

    def __init__(self, pools: NamePools, *, version: str, seed: str, pair_count: int = 400) -> None:
        self.pools = pools
        self.version = version
        self.seed = seed
        self.pair_count = pair_count
        self.mapping: Dict[str, str] = {}
        self.digest: str = ""
        self.refresh(seed)

    def refresh(self, seed: str | None = None, count: int | None = None) -> Dict[str, str]:
        if seed:
            self.seed = seed
        self.mapping = build_mapping(
            self.pools,
            seed=self.seed,
            count=count or self.pair_count,
            include_latin=True,
        )
        self.digest = checksum(self.mapping)
        return self.mapping

    def info(self) -> Dict[str, object]:
        return {
            "version": self.version,
            "seed": self.seed,
            "pairs": len(self.mapping),
            "checksum": self.digest,
            "capacity": self.pools.max_pairs,
        }
