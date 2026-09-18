"""Discover the character names that *actually occur* in a novel.

Why this exists
---------------
A curated pool alone cannot cover every name a novel uses. If the pool produced
``王明`` but the novel uses ``王云``, you get **zero replacements** - the file
comes back unchanged. This module closes that gap with a two-pass pipeline:

    pass 1  scan the decoded text for frequent surname-anchored tokens
            (王云 / 李雪 / 王小虎 ...) and count them
    pass 2  merge those discoveries into the curated mapping, assigning each one
            a deterministic Indian name, then run ONE regex pass

Precision heuristics (all cheap, all explainable):

* a candidate must start with a known surname;
* candidates that contain a **function character** (了 的 是 在 和 我 你 ...)
  are rejected outright - real names do not contain them;
* a candidate must occur **>= N times** (threshold). Real character names
  repeat hundreds of times; incidental compounds like 王国 do not;
* **prefix dominance**: if 王小虎 dominates 王小 (shares the prefix, similar
  count), the shorter one is dropped;
* assignment is a **deterministic hash** of the Chinese name, so the same novel
  always yields the same Indian names, and two Chinese names never collapse
  into one Indian name.

Because replacement is a single regex pass, a merged mapping *cannot* cascade -
that is why no O(n^2) safety net is run on the merged mapping (it is only run on
the small curated mapping).
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from typing import Dict, Iterable, List, Sequence, Tuple

from ..logging_setup import get_logger
from .names import NamePools, checksum

log = get_logger("core.discovery")

# A maximal run of CJK characters. Names are found *inside* these runs, which
# is what makes "王云看到李雪" (one single run) work correctly.
CJK_RUN = re.compile(r"[\u3400-\u4DBF\u4E00-\u9FFF\uF900-\uFAFF]+")

# Characters that never appear in a personal name but are extremely common in
# prose. Any candidate containing one is rejected.
FUNCTION_CHARS = frozenset(
    "了的着是在和与我你他她它们不有这那就都很还也把被让给对从向以及"
    "之后前中里上下出起来去过来吧呢啊呀哦嘛吗么会能要可没为但而且因此"
    "说笑道问看听想知见做走来过站立坐卧吃喝拉撒睡"
)

MIN_NAME_LEN = 2
MAX_NAME_LEN = 3


def extract_candidates(
    text: str,
    surnames: Sequence[str],
    *,
    threshold: int = 3,
    max_names: int = 600,
    exclude: Iterable[str] = (),
) -> List[Tuple[str, int]]:
    """Return ``[(chinese_name, count), ...]`` filtered and ranked.

    Runs once per novel. Cost is O(number of characters), not O(n^2).
    """
    if not text:
        return []

    exclude_set = set(exclude)
    surname_set = set(surnames)
    two_char = {s for s in surname_set if len(s) >= 2}
    counts: Counter[str] = Counter()

    for match in CJK_RUN.finditer(text):
        run = match.group(0)
        length = len(run)
        for index, char in enumerate(run):
            # Longest surname first so 欧阳-style compounds win over 欧.
            surname = ""
            if char in two_char and run[index:index + 2] in two_char:
                surname = run[index:index + 2]
            elif char in surname_set:
                surname = char
            if not surname:
                continue

            after = run[index + len(surname): index + len(surname) + 2]
            for tail_len in (1, 2):
                if len(after) < tail_len:
                    continue
                candidate = surname + after[:tail_len]
                if not (MIN_NAME_LEN <= len(candidate) <= MAX_NAME_LEN):
                    continue
                if any(ch in FUNCTION_CHARS for ch in candidate):
                    continue
                if candidate in exclude_set:
                    continue
                counts[candidate] += 1
            if surname and len(surname) == 2 and len(after) >= 1:
                # single given character after a 2-char surname
                candidate = surname + after[:1]
                if len(candidate) >= 2 and not any(ch in FUNCTION_CHARS for ch in candidate):
                    if candidate not in exclude_set:
                        counts[candidate] += 1

    ranked = [(name, count) for name, count in counts.items() if count >= threshold]
    if not ranked:
        log.info("name discovery found nothing above threshold", extra={"threshold": threshold})
        return []

    ranked.sort(key=lambda item: (-item[1], item[0]))
    ranked = ranked[: max(1, max_names)]

    # Prefix dominance: drop 王小 when 王小虎 is just as frequent.
    selected = [name for name, _ in ranked]
    lookup = dict(ranked)
    dropped = 0
    kept: List[str] = []
    for name in selected:
        dominated = any(
            other != name
            and other.startswith(name)
            and len(other) > len(name)
            and lookup.get(other, 0) >= 0.5 * lookup.get(name, 0)
            for other in selected
        )
        if dominated:
            dropped += 1
            continue
        kept.append(name)

    result = [(name, lookup[name]) for name in kept]
    log.info(
        "name discovery done",
        extra={
            "candidates": len(counts),
            "kept": len(result),
            "dropped_by_dominance": dropped,
            "threshold": threshold,
        },
    )
    return result


def assign_names(
    names: Sequence[str],
    pools: NamePools,
    *,
    seed: str,
    used_values: set[str] | None = None,
) -> Dict[str, str]:
    """Deterministically map each Chinese name to a *unique* Indian name."""
    if not pools.in_first:
        return {}
    used = set(used_values or ())
    total_slots = len(pools.in_first) * max(1, len(pools.in_last))
    assigned: Dict[str, str] = {}

    for name in sorted(set(names)):
        digest = hashlib.sha256(f"{seed}|{name}".encode("utf-8")).digest()
        start = int.from_bytes(digest[:8], "big")
        value = None
        for probe in range(total_slots):
            index = start + probe
            first = pools.in_first[index % len(pools.in_first)]
            if pools.in_last:
                last = pools.in_last[(index // len(pools.in_first)) % len(pools.in_last)]
                candidate = f"{first} {last}"
            else:
                candidate = first
            if candidate not in used:
                value = candidate
                break
        if value is None:  # pragma: no cover - pool exhausted
            continue
        used.add(value)
        assigned[name] = value
    return assigned


def augment_mapping(
    registry,
    text: str,
    *,
    threshold: int = 3,
    max_names: int = 600,
) -> Tuple[Dict[str, str], Dict[str, object]]:
    """Merge curated mapping + names discovered in ``text``.

    Returns ``(mapping, stats)`` where ``stats["checksum"]`` identifies the
    exact mapping used (recorded with the job, so a result can be reproduced).
    """
    base = registry.mapping
    stats: Dict[str, object] = {
        "found": 0,
        "added": 0,
        "threshold": threshold,
        "checksum": registry.digest,
        "sample": [],
    }
    pools: NamePools = registry.pools
    if not text or not pools.ready:
        return base, stats

    candidates = extract_candidates(
        text,
        [name for name, _ in pools.zh_surnames],
        threshold=threshold,
        max_names=max_names,
        exclude=base.keys(),
    )
    if not candidates:
        return base, stats

    assigned = assign_names(
        [name for name, _ in candidates],
        pools,
        seed=registry.seed,
        used_values=set(base.values()),
    )
    if not assigned:
        return base, stats

    merged = dict(base)
    merged.update(assigned)
    stats.update(
        found=len(candidates),
        added=len(assigned),
        checksum=checksum(merged),
        sample=[(name, merged[name]) for name in list(assigned)[:8]],
    )
    log.info(
        "mapping augmented with discovered names",
        extra={
            "base_keys": len(base),
            "discovered": len(assigned),
            "merged_keys": len(merged),
            "checksum": stats["checksum"],
        },
    )
    return merged, stats
