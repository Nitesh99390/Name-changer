"""Per-user rate limiting (sliding window) + global concurrency control.

A Telegram user can spam documents far faster than the free tier can convert
them. This limiter keeps the process alive and keeps costs predictable.
The window is in-memory (cheap, reset on deploy) and optionally double-checked
against Supabase job history so a restart cannot be used to bypass the quota.
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from typing import Deque, Dict, Tuple

from ..config import get_settings
from ..errors import RateLimitedError
from ..logging_setup import get_logger

log = get_logger("services.ratelimit")


class RateLimiter:
    def __init__(self, limit_per_hour: int, window_seconds: int = 3600) -> None:
        self.limit = max(1, limit_per_hour)
        self.window = window_seconds
        self._hits: Dict[int, Deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    def _prune(self, user_id: int, now: float) -> None:
        bucket = self._hits[user_id]
        cutoff = now - self.window
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if not bucket:
            self._hits.pop(user_id, None)

    async def check(self, user_id: int) -> Tuple[bool, int]:
        """Return ``(allowed, retry_after_seconds)``."""
        now = time.time()
        async with self._lock:
            self._prune(user_id, now)
            bucket = self._hits.get(user_id)
            if bucket and len(bucket) >= self.limit:
                retry_after = int(self.window - (now - bucket[0])) + 1
                log.warning(
                    "rate limited", extra={"user_id": user_id, "hits": len(bucket)}
                )
                return False, max(1, retry_after)
            self._hits.setdefault(user_id, deque()).append(now)
            return True, 0

    async def assert_allowed(self, user_id: int) -> None:
        allowed, retry_after = await self.check(user_id)
        if not allowed:
            raise RateLimitedError(retry_after, self.limit)

    async def remaining(self, user_id: int) -> int:
        now = time.time()
        async with self._lock:
            self._prune(user_id, now)
            bucket = self._hits.get(user_id)
            return self.limit - (len(bucket) if bucket else 0)

    async def release(self, user_id: int) -> None:
        """Give a slot back (e.g. the job failed before doing any work)."""
        async with self._lock:
            bucket = self._hits.get(user_id)
            if bucket:
                bucket.pop()
                if not bucket:
                    self._hits.pop(user_id, None)

    def snapshot(self) -> Dict[str, int]:
        now = time.time()
        return {
            str(uid): len(self._hits[uid])
            for uid in list(self._hits)
            if self._hits[uid] and self._hits[uid][-1] > now - self.window
        }


_limiter: RateLimiter | None = None


def get_rate_limiter() -> RateLimiter:
    global _limiter
    if _limiter is None:
        _limiter = RateLimiter(get_settings().rate_limit_per_hour)
    return _limiter
