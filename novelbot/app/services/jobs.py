"""Job orchestration: one conversion at a time per user, N globally.

Responsibilities
----------------
* bound global concurrency so the 512 MB container never OOMs;
* bound per-user concurrency (one job) so status editing stays coherent;
* enforce a wall-clock timeout and *always* free the slot in ``finally``;
* record metadata in Supabase and delete every temp file afterwards.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Dict, Optional

from ..config import get_settings
from ..errors import BotError, BusyError, InternalError, JobTimeoutError
from ..logging_setup import get_logger

log = get_logger("services.jobs")


@dataclass
class JobHandle:
    user_id: int
    chat_id: int
    job_id: Optional[str] = None
    started: float = field(default_factory=time.monotonic)
    cancelled: bool = False

    @property
    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self.started) * 1000)


class JobManager:
    def __init__(self, max_concurrent: int = 2, timeout_seconds: int = 240) -> None:
        self._semaphore = asyncio.Semaphore(max(1, max_concurrent))
        self._user_locks: Dict[int, asyncio.Lock] = {}
        self._active: Dict[int, JobHandle] = {}
        self.timeout = max(30, timeout_seconds)
        self._guard = asyncio.Lock()

    # ------------------------------------------------------------------
    async def _lock_for(self, user_id: int) -> asyncio.Lock:
        async with self._guard:
            lock = self._user_locks.get(user_id)
            if lock is None:
                lock = asyncio.Lock()
                self._user_locks[user_id] = lock
            return lock

    def is_busy(self, user_id: int) -> bool:
        lock = self._user_locks.get(user_id)
        return bool(lock and lock.locked())

    async def run(
        self,
        user_id: int,
        chat_id: int,
        work: Callable[[JobHandle], Awaitable[None]],
        *,
        job_id: Optional[str] = None,
    ) -> None:
        """Run ``work`` under both limits, with timeout and guaranteed cleanup."""
        user_lock = await self._lock_for(user_id)
        if user_lock.locked():
            raise BusyError("user already running a job")

        async with user_lock:
            handle = JobHandle(user_id=user_id, chat_id=chat_id, job_id=job_id)
            self._active[user_id] = handle
            try:
                async with self._semaphore:
                    await asyncio.wait_for(work(handle), timeout=self.timeout)
            except asyncio.TimeoutError as exc:
                raise JobTimeoutError(f"timeout after {self.timeout}s") from exc
            except asyncio.CancelledError:
                handle.cancelled = True
                log.warning("job cancelled", extra={"user_id": user_id})
                raise
            finally:
                self._active.pop(user_id, None)
                async with self._guard:
                    if not user_lock.locked() and user_lock is not self._user_locks.get(user_id):
                        self._user_locks.pop(user_id, None)

    async def cancel(self, user_id: int) -> bool:
        handle = self._active.get(user_id)
        if handle is None:
            return False
        handle.cancelled = True
        task = asyncio.current_task()
        log.info("cancel requested", extra={"user_id": user_id})
        return True

    def stats(self) -> Dict[str, int]:
        return {
            "active_users": len(self._active),
            "free_slots": self._semaphore._value,  # noqa: SLF001 - read-only metric
        }

    @contextlib.asynccontextmanager
    async def shutdown(self):
        """Drain running jobs on SIGTERM before the container is killed."""
        try:
            yield
        finally:
            for uid, handle in list(self._active.items()):
                handle.cancelled = True
                log.info("draining job", extra={"user_id": uid})
            deadline = time.monotonic() + 15
            while self._active and time.monotonic() < deadline:
                await asyncio.sleep(0.25)


_manager: Optional[JobManager] = None


def get_job_manager() -> JobManager:
    """Process-wide singleton (initialised lazily on first use)."""
    return init_job_manager()


def init_job_manager() -> JobManager:
    global _manager
    if _manager is None:
        settings = get_settings()
        _manager = JobManager(
            max_concurrent=settings.max_concurrent_jobs,
            timeout_seconds=settings.job_timeout_seconds,
        )
        log.info(
            "job manager ready",
            extra={
                "max_concurrent": settings.max_concurrent_jobs,
                "timeout_s": settings.job_timeout_seconds,
            },
        )
    return _manager
