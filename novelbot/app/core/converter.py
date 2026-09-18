"""The replacer - one regex pass, no cascades, no mid-word damage.

The original bot used ``content.replace(key, value)`` in a loop. That is wrong
in two independent ways:

* **cascade corruption** - once ``王`` became ``Raj``, a later iteration whose
  key was a substring of ``Raj`` rewrote the replacement text again;
* **mid-word damage** - ``Li`` matched inside ``Liang``, ``Delhi``, ``Elizabeth``.

Both are impossible here because we build a single compiled pattern and run one
``pattern.sub`` pass over the text:

1. keys are sorted **longest first**, and Python's alternation takes the first
   branch that matches, so the longest key wins at every position;
2. Hanzi keys are matched literally;
3. Latin/pinyin keys are wrapped in look-arounds ``(?<![A-Za-z0-9])`` /
   ``(?![A-Za-z0-9])`` so they can never bite into a longer word, and internal
   spaces become ``\\s+`` so ``Wang Wei`` still matches ``Wang   Wei``;
4. because the pass is single-shot, replacement text is never re-scanned.
"""

from __future__ import annotations

import re
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Tuple

from ..logging_setup import get_logger
from .names import NameRegistry, checksum, has_cjk

log = get_logger("core.converter")

_WORD_CHARS = r"A-Za-z0-9_"
CJK_RE = re.compile(
    r"[\u3400-\u4DBF\u4E00-\u9FFF\uF900-\uFAFF\U00020000-\U0002A6DF]"
)


def _is_latin_key(key: str) -> bool:
    return not CJK_RE.search(key)


@dataclass
class ConversionResult:
    text: str
    char_count: int
    replacements: int
    unique_names: int
    elapsed_ms: int
    mapping_version: str
    mapping_checksum: str
    top_names: List[Tuple[str, str, int]] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return self.replacements > 0

    def summary_line(self) -> str:
        return (
            f"{self.replacements} replacements / {self.unique_names} distinct names "
            f"in {self.elapsed_ms} ms"
        )


class NameConverter:
    """Compiled, reusable replacer bound to a :class:`NameRegistry`."""

    def __init__(
        self,
        registry: NameRegistry,
        *,
        max_key_length: int = 24,
        allow_latin: bool = True,
        mapping: Dict[str, str] | None = None,
    ) -> None:
        self._registry = registry
        self._explicit = mapping
        self._max_key_length = max(4, max_key_length)
        self._allow_latin = allow_latin
        self._pattern: re.Pattern[str] | None = None
        self._mapping: Dict[str, str] = {}
        self._lookup: Dict[str, str] = {}
        self._flags = re.IGNORECASE
        self._compiled_digest = ""
        self._source_digest = ""
        self._compile()

    # ------------------------------------------------------------------
    # key normalisation (case-insensitive + whitespace-insensitive lookup)
    # ------------------------------------------------------------------
    @staticmethod
    def _norm(value: str) -> str:
        """Collapse internal whitespace and casefold.

        This is what lets ``Wang   Wei`` (multiple spaces / tabs / newlines in
        the source novel) still resolve to the ``Wang Wei`` key, and lets a
        lower-case ``wang wei`` match too.
        """
        return " ".join(value.split()).casefold()

    # ------------------------------------------------------------------
    # compilation
    # ------------------------------------------------------------------
    def _select_keys(self, mapping: Dict[str, str]) -> List[str]:
        keys: List[str] = []
        for key in mapping:
            if not key or key != key.strip() or "=" in key:
                continue
            if len(key) > self._max_key_length and _is_latin_key(key):
                continue
            if len(key) > 8 and not _is_latin_key(key):
                continue
            if _is_latin_key(key) and not self._allow_latin:
                continue
            keys.append(key)
        # Longest first => regex alternation resolves to the longest match.
        keys.sort(key=lambda k: (-len(k), k))
        return keys

    def _compile(self) -> None:
        mapping = self._explicit if self._explicit is not None else self._registry.mapping
        self._source_digest = (
            checksum(mapping) if self._explicit is not None else self._registry.digest
        )
        keys = self._select_keys(mapping)
        if not keys:
            self._pattern = None
            self._mapping = {}
            self._compiled_digest = self._source_digest
            log.warning("converter compiled with no keys")
            return

        branches: List[str] = []
        for key in keys:
            if _is_latin_key(key):
                body = r"\s+".join(re.escape(part) for part in key.split())
                branches.append(rf"(?<![{_WORD_CHARS}]){body}(?![{_WORD_CHARS}])")
            else:
                branches.append(re.escape(key))

        try:
            self._pattern = re.compile("|".join(branches), self._flags)
        except re.error as exc:  # pragma: no cover - defensive
            log.exception("regex compilation failed, falling back to literal scan")
            self._pattern = re.compile(
                "|".join(re.escape(k) for k in keys if not _is_latin_key(key))
            )
            log.warning("converter degraded to Hanzi-only", extra={"err": str(exc)})

        self._mapping = mapping
        # Pre-normalised lookup: O(1), whitespace- and case-insensitive.
        self._lookup = {self._norm(key): value for key, value in mapping.items()}
        self._compiled_digest = self._source_digest
        log.info(
            "converter compiled",
            extra={
                "keys": len(keys),
                "latin": sum(1 for k in keys if _is_latin_key(k)),
                "version": self._registry.version,
                "checksum": self._registry.digest,
            },
        )

    def refresh(self, force: bool = False) -> "NameConverter":
        """Recompile when the source mapping changed.

        A converter built with an explicit (per-job) mapping is self-contained,
        so it is never rebuilt from the registry.
        """
        if self._explicit is not None:
            if force:
                self._compile()
            return self
        if force or self._compiled_digest != self._registry.digest:
            self._compile()
        return self

    @property
    def mapping(self) -> Dict[str, str]:
        return self._mapping

    @property
    def key_count(self) -> int:
        return len(self._mapping)

    # ------------------------------------------------------------------
    # conversion
    # ------------------------------------------------------------------
    def convert(self, text: str, *, keep_original: bool = False) -> ConversionResult:
        started = time.perf_counter()
        self.refresh()

        counter: Counter[str] = Counter()
        if not text:
            return ConversionResult("", 0, 0, 0, 0, self._registry.version,
                                    self._compiled_digest)

        pattern = self._pattern
        mapping = self._mapping

        if pattern is None or not mapping:
            return ConversionResult(
                text, len(text), 0, 0,
                int((time.perf_counter() - started) * 1000),
                self._registry.version, self._compiled_digest,
            )

        lookup = self._lookup
        norm = self._norm

        def _replace(match: re.Match[str]) -> str:
            token = match.group(0)
            # One normalised lookup handles case, tabs and repeated spaces.
            target = lookup.get(norm(token))
            if target is None:
                return token
            counter[" ".join(token.split())] += 1
            if keep_original:
                return f"{target} ({' '.join(token.split())})"
            return target

        try:
            converted = pattern.sub(_replace, text)
        except Exception:  # pragma: no cover - never kill a job on regex
            log.exception("regex substitution failed, returning original text")
            return ConversionResult(
                text, len(text), 0, 0,
                int((time.perf_counter() - started) * 1000),
                self._registry.version, self._compiled_digest,
            )

        elapsed = int((time.perf_counter() - started) * 1000)
        top = [
            (old, mapping.get(old, "?"), hits)
            for old, hits in counter.most_common(8)
        ]
        result = ConversionResult(
            text=converted,
            char_count=len(text),
            replacements=sum(counter.values()),
            unique_names=len(counter),
            elapsed_ms=elapsed,
            mapping_version=self._registry.version,
            mapping_checksum=self._compiled_digest,
            top_names=top,
        )
        log.info(
            "conversion done",
            extra={
                "chars": result.char_count,
                "replacements": result.replacements,
                "unique": result.unique_names,
                "ms": result.elapsed_ms,
            },
        )
        return result

    # ------------------------------------------------------------------
    # chunked conversion for large files (bounded memory on 512 MB)
    # ------------------------------------------------------------------
    def convert_chunks(
        self,
        chunks: Iterable[str],
        out,
        *,
        keep_original: bool = False,
    ) -> ConversionResult:
        started = time.perf_counter()
        total_chars = total_repl = unique = 0
        seen: set[str] = set()
        version = self._registry.version
        digest = self._compiled_digest

        for chunk in chunks:
            if not chunk:
                continue
            result = self.convert(chunk, keep_original=keep_original)
            out.write(result.text)
            total_chars += result.char_count
            total_repl += result.replacements
            for old, _, _ in result.top_names:
                seen.add(old)
            unique += result.unique_names

        return ConversionResult(
            text="",
            char_count=total_chars,
            replacements=total_repl,
            unique_names=max(unique, len(seen)),
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            mapping_version=version,
            mapping_checksum=digest,
        )


def preview(text: str, mapping: Dict[str, str], limit: int = 5) -> str:
    """Tiny helper for the Mini App demo box."""
    sample = [f"{old} -> {new}" for old, new in list(mapping.items())[:limit]]
    return ", ".join(sample)
