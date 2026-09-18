"""Shared client + registry singletons (avoids import cycles)."""

from __future__ import annotations

from typing import Optional

from ..config import get_settings
from .supabase import SupabaseClient

_client: Optional[SupabaseClient] = None


def get_supabase() -> SupabaseClient:
    """Lazily build the process-wide Supabase client."""
    global _client
    if _client is None:
        settings = get_settings()
        _client = SupabaseClient(
            settings.supabase_url,
            settings.supabase_key,
            timeout=settings.supabase_timeout,
            retries=settings.supabase_retries,
            enabled=settings.supabase_enabled,
        )
    return _client


async def close_supabase() -> None:
    global _client
    if _client is not None:
        await _client.close()
