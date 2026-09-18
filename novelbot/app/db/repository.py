"""Domain-level persistence helpers.

Everything here is *metadata only*. There is deliberately no function that
writes novel text, converted text or uploaded files to the database - the
files live in ``/tmp`` for the lifetime of a single job and are then deleted.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..config import get_settings
from ..logging_setup import get_logger
from .supabase import SupabaseClient

log = get_logger("db.repository")

T_USER = "bot_users"
T_JOB = "conversion_jobs"
T_SETTING = "bot_settings"
T_MAPPING = "name_mappings"

DEFAULT_USER_SETTINGS: Dict[str, Any] = {
    "latin_names": True,      # also convert pinyin/latin keys
    "notify_progress": True,  # edit the status message while working
    "keep_original": False,   # append " (原)" marker after each replaced name
}


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _client() -> SupabaseClient:
    from .client import get_supabase

    return get_supabase()


async def touch_user(
    user_id: int,
    *,
    username: Optional[str] = None,
    first_name: Optional[str] = None,
    last_name: Optional[str] = None,
    language_code: Optional[str] = None,
) -> None:
    """Insert-or-update the caller. Never overwrites an existing settings blob."""
    row = {
        "user_id": user_id,
        "username": username,
        "first_name": (first_name or "")[:64] or None,
        "last_name": (last_name or "")[:64] or None,
        "language_code": (language_code or "")[:8] or None,
        "last_seen_at": utcnow_iso(),
    }
    await _client().upsert(T_USER, [{k: v for k, v in row.items() if v is not None}],
                           on_conflict="user_id")


async def get_user(user_id: int) -> Optional[Dict[str, Any]]:
    rows = await _client().select(
        T_USER, filters={"user_id": f"eq.{user_id}"}, limit=1, single=False
    )
    return rows[0] if rows else None


async def get_user_settings(user_id: int) -> Dict[str, Any]:
    user = await get_user(user_id)
    settings = dict(DEFAULT_USER_SETTINGS)
    if user and isinstance(user.get("settings"), dict):
        settings.update({k: v for k, v in user["settings"].items() if k in settings})
    return settings


async def save_user_settings(user_id: int, settings: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(DEFAULT_USER_SETTINGS)
    merged.update({k: v for k, v in settings.items() if k in DEFAULT_USER_SETTINGS})
    await _client().upsert(
        T_USER,
        [{"user_id": user_id, "settings": merged, "last_seen_at": utcnow_iso()}],
        on_conflict="user_id",
    )
    return merged


async def create_job(user_id: int, *, file_name: str, file_size: int, chat_id: int) -> Optional[str]:
    job_id = f"job_{int(time.time() * 1000):x}_{user_id}"
    rows = await _client().insert(
        T_JOB,
        [
            {
                "job_id": job_id,
                "user_id": user_id,
                "chat_id": chat_id,
                "status": "running",
                "file_name": (file_name or "")[:128],
                "file_size": int(file_size or 0),
                "created_at": utcnow_iso(),
            }
        ],
    )
    if not rows:
        return None
    return job_id


async def finish_job(
    job_id: Optional[str],
    *,
    status: str,
    char_count: int = 0,
    replacement_count: int = 0,
    duration_ms: int = 0,
    error_code: Optional[str] = None,
    mapping_version: Optional[str] = None,
    user_id: Optional[int] = None,
) -> None:
    if not job_id:
        return
    await _client().update(
        T_JOB,
        {
            "status": status,
            "char_count": int(char_count),
            "replacement_count": int(replacement_count),
            "duration_ms": int(duration_ms),
            "error_code": error_code,
            "mapping_version": mapping_version,
            "finished_at": utcnow_iso(),
        },
        filters={"job_id": f"eq.{job_id}"},
    )
    if status == "done" and user_id:
        # Aggregate counters live on the user row so /stats is one query.
        await _client().rpc("bump_user_counters", {"p_user_id": user_id})


async def count_jobs_since(user_id: int, since_iso: str) -> int:
    rows = await _client().select(
        T_JOB,
        columns="job_id",
        filters={"user_id": f"eq.{user_id}", "created_at": f"gte.{since_iso}"},
    )
    return len(rows or [])


async def user_job_history(user_id: int, limit: int = 10) -> List[Dict[str, Any]]:
    return (
        await _client().select(
            T_JOB,
            columns="job_id,status,file_name,char_count,replacement_count,duration_ms,created_at",
            filters={"user_id": f"eq.{user_id}"},
            order="created_at.desc",
            limit=limit,
        )
        or []
    )


async def register_mapping_version(
    version: str, *, seed: str, pair_count: int, source: str, checksum: str
) -> None:
    await _client().upsert(
        T_MAPPING,
        [
            {
                "version": version,
                "seed": seed,
                "pair_count": int(pair_count),
                "source": source[:64],
                "checksum": checksum,
                "created_at": utcnow_iso(),
            }
        ],
        on_conflict="version",
    )


async def get_setting(key: str) -> Optional[Any]:
    rows = await _client().select(
        T_SETTING, columns="value", filters={"key": f"eq.{key}"}, limit=1
    )
    return rows[0].get("value") if rows else None


async def set_setting(key: str, value: Any) -> None:
    await _client().upsert(
        T_SETTING,
        [{"key": key, "value": value, "updated_at": utcnow_iso()}],
        on_conflict="key",
    )


async def global_stats() -> Dict[str, Any]:
    """Aggregate counters for the Mini App / ``/stats``."""
    settings = get_settings()
    users = await _client().select(T_USER, columns="user_id", limit=1000)
    jobs = await _client().select(
        T_JOB,
        columns="status,char_count,replacement_count,duration_ms",
        order="created_at.desc",
        limit=1000,
    )
    done = [j for j in (jobs or []) if j.get("status") == "done"]
    return {
        "users": len(users or []),
        "jobs": len(jobs or []),
        "jobs_done": len(done),
        "chars": sum(int(j.get("char_count") or 0) for j in done),
        "replacements": sum(int(j.get("replacement_count") or 0) for j in done),
        "avg_ms": int(
            sum(int(j.get("duration_ms") or 0) for j in done) / len(done)
        ) if done else 0,
        "storage": "metadata-only",
        "mapping_version": settings.names_version,
        "db": _client().enabled,
    }
