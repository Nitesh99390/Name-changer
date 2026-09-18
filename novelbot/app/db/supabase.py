"""Thin, resilient Supabase REST client.

No ORM, no SDK - just ``httpx`` against the PostgREST endpoint so the free
tier stays light. Every call is non-fatal: if Supabase is down or
unconfigured, the bot keeps converting files (only stats are lost).
"""

from __future__ import annotations

import asyncio
import random
from typing import Any, Dict, List, Optional, Sequence

import httpx

from ..logging_setup import get_logger

log = get_logger("db.supabase")

_RETRY_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


class SupabaseClient:
    """Minimal async PostgREST wrapper with retry + graceful degradation."""

    def __init__(
        self,
        url: str,
        key: str,
        *,
        timeout: float = 8.0,
        retries: int = 3,
        enabled: bool = True,
    ) -> None:
        self._url = (url or "").rstrip("/")
        self._key = key or ""
        self._timeout = timeout
        self._retries = max(1, retries)
        self._enabled = enabled
        self._client: Optional[httpx.AsyncClient] = None
        self._last_error: Optional[str] = None

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return bool(self._enabled and self._url and self._key)

    @property
    def last_error(self) -> Optional[str]:
        return self._last_error

    async def start(self) -> None:
        if not self.enabled:
            log.warning("supabase disabled - running in stateless mode")
            return
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=f"{self._url}/rest/v1",
                timeout=httpx.Timeout(self._timeout, connect=min(5.0, self._timeout)),
                limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
                headers=self._headers(),
            )
            log.info("supabase client ready", extra={"host": self._url.split("//")[-1]})

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:  # pragma: no cover - shutdown best effort
                pass
            self._client = None

    def _headers(self, prefer: Optional[str] = None) -> Dict[str, str]:
        headers = {
            "apikey": self._key,
            "Authorization": f"Bearer {self._key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if prefer:
            headers["Prefer"] = prefer
        return headers

    # ------------------------------------------------------------------
    # core request
    # ------------------------------------------------------------------
    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, str]] = None,
        payload: Any = None,
        prefer: Optional[str] = None,
    ) -> Optional[Any]:
        """Return parsed JSON or ``None``. Never raises."""
        if not self.enabled:
            return None
        if self._client is None:
            await self.start()
        if self._client is None:
            return None

        delay = 0.4
        for attempt in range(1, self._retries + 1):
            try:
                response = await self._client.request(
                    method,
                    path,
                    params=params,
                    json=payload,
                    headers=self._headers(prefer),
                )
                if response.status_code in _RETRY_STATUS and attempt < self._retries:
                    await asyncio.sleep(delay + random.uniform(0, 0.25))
                    delay *= 2
                    continue
                if response.status_code >= 400:
                    self._last_error = f"{response.status_code} {response.text[:180]}"
                    log.warning(
                        "supabase request failed",
                        extra={
                            "method": method,
                            "path": path,
                            "status": response.status_code,
                            "body": response.text[:180],
                        },
                    )
                    return None
                self._last_error = None
                if not response.content:
                    return None
                try:
                    return response.json()
                except ValueError:
                    return None
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt < self._retries:
                    await asyncio.sleep(delay + random.uniform(0, 0.25))
                    delay *= 2
                    continue
                self._last_error = type(exc).__name__
                log.warning(
                    "supabase unreachable", extra={"path": path, "err": type(exc).__name__}
                )
                return None
            except Exception as exc:  # pragma: no cover - defensive
                self._last_error = type(exc).__name__
                log.exception("supabase unexpected error", extra={"path": path})
                return None
        return None

    # ------------------------------------------------------------------
    # convenience verbs
    # ------------------------------------------------------------------
    async def select(
        self,
        table: str,
        *,
        columns: str = "*",
        filters: Optional[Dict[str, str]] = None,
        order: Optional[str] = None,
        limit: Optional[int] = None,
        single: bool = False,
    ) -> Optional[List[Dict[str, Any]]]:
        params: Dict[str, str] = {"select": columns}
        if filters:
            params.update(filters)
        if order:
            params["order"] = order
        if limit is not None:
            params["limit"] = str(limit)
        prefer = "application/vnd.pgrst.object+json" if single else None
        result = await self._request("GET", f"/{table}", params=params, prefer=prefer)
        if result is None:
            return None
        return [result] if isinstance(result, dict) else result

    async def upsert(
        self,
        table: str,
        rows: Sequence[Dict[str, Any]],
        *,
        on_conflict: Optional[str] = None,
    ) -> Optional[List[Dict[str, Any]]]:
        if not rows:
            return []
        params = {"on_conflict": on_conflict} if on_conflict else None
        return await self._request(
            "POST",
            f"/{table}",
            params=params,
            payload=list(rows),
            prefer="resolution=merge-duplicates,return=representation",
        )

    async def insert(
        self, table: str, rows: Sequence[Dict[str, Any]]
    ) -> Optional[List[Dict[str, Any]]]:
        if not rows:
            return []
        return await self._request(
            "POST", f"/{table}", payload=list(rows), prefer="return=representation"
        )

    async def update(
        self,
        table: str,
        values: Dict[str, Any],
        *,
        filters: Dict[str, str],
    ) -> Optional[List[Dict[str, Any]]]:
        return await self._request(
            "PATCH",
            f"/{table}",
            params=filters,
            payload=values,
            prefer="return=representation",
        )

    async def rpc(self, function: str, args: Optional[Dict[str, Any]] = None) -> Optional[Any]:
        return await self._request("POST", f"/rpc/{function}", payload=args or {})

    async def ping(self) -> bool:
        """Cheap liveness probe used by ``/health``."""
        if not self.enabled:
            return False
        result = await self._request(
            "GET", "/bot_settings", params={"select": "key", "limit": "1"}
        )
        return result is not None
