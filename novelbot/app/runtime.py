"""Process-wide runtime: name registry + compiled converters.

Created once at boot and shared by the bot handlers and the HTTP API, so the
Mini App can preview the exact same mapping the bot uses.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Dict, Optional

from .config import get_settings
from .core.converter import NameConverter
from .core.names import NameRegistry, load_pools, merge_remote_pools
from .logging_setup import get_logger

log = get_logger("runtime")

DYNAMIC_CACHE_SIZE = 6


@dataclass
class Runtime:
    registry: NameRegistry
    started_at: float = field(default_factory=time.monotonic)
    _converters: Dict[bool, NameConverter] = field(default_factory=dict)
    _dynamic: "OrderedDict[tuple, NameConverter]" = field(default_factory=OrderedDict)

    def converter_for(self, allow_latin: bool) -> NameConverter:
        """Cached converter for the curated mapping (latin on/off)."""
        key = bool(allow_latin)
        converter = self._converters.get(key)
        if converter is None:
            settings = get_settings()
            converter = NameConverter(
                self.registry,
                max_key_length=settings.max_key_length,
                allow_latin=key,
            )
            self._converters[key] = converter
        converter.refresh()
        return converter

    def converter_with(
        self, allow_latin: bool, mapping: Dict[str, str], cache_key: str
    ) -> NameConverter:
        """Converter for a per-job mapping (curated + names found in the novel).

        Cached by the mapping checksum in a small bounded LRU, so converting the
        same novel twice does not pay for regex compilation again.
        """
        key = (bool(allow_latin), cache_key)
        cached = self._dynamic.get(key)
        if cached is not None:
            self._dynamic.move_to_end(key)
            return cached

        settings = get_settings()
        converter = NameConverter(
            self.registry,
            max_key_length=settings.max_key_length,
            allow_latin=allow_latin,
            mapping=mapping,
        )
        self._dynamic[key] = converter
        while len(self._dynamic) > DYNAMIC_CACHE_SIZE:
            self._dynamic.popitem(last=False)
        return converter

    @property
    def converter(self) -> NameConverter:
        return self.converter_for(True)

    def stats(self) -> Dict[str, object]:
        return {
            "names": self.registry.info(),
            "converters": {
                ("latin" if k else "hanzi"): v.key_count for k, v in self._converters.items()
            },
            "dynamic_cached": len(self._dynamic),
            "uptime_s": int(time.monotonic() - self.started_at),
        }


_runtime: Optional[Runtime] = None


async def init_runtime() -> Runtime:
    """Load bundled resources, optionally merge remote extras, build registry."""
    global _runtime
    if _runtime is not None:
        return _runtime

    settings = get_settings()
    from .config import RESOURCES_DIR

    pools = load_pools(RESOURCES_DIR)
    if settings.names_remote_urls:
        added = await merge_remote_pools(pools, settings.names_remote_urls)
        log.info("remote pools merged", extra={"added": added})

    registry = NameRegistry(
        pools,
        version=settings.names_version,
        seed=settings.names_seed,
        pair_count=min(settings.max_pairs, max(400, pools.max_pairs)),
    )
    _runtime = Runtime(registry=registry)
    _runtime.converter_for(True)
    log.info("runtime ready", extra=_runtime.stats())
    return _runtime


def get_runtime() -> Runtime:
    if _runtime is None:  # pragma: no cover - boot order guard
        raise RuntimeError("runtime not initialised")
    return _runtime
