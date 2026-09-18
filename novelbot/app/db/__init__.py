"""Persistence layer (Supabase REST).

Design rule enforced here and in ``sql/schema.sql``:
    ONLY metadata is stored - users, job counters, settings, mapping versions.
    Novel text, uploaded files and converted files are NEVER persisted.
"""

from .client import SupabaseClient, close_supabase, get_supabase

__all__ = ["SupabaseClient", "get_supabase", "close_supabase"]
