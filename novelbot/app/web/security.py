"""Telegram WebApp ``initData`` verification.

The Mini App cannot be trusted on its own: anyone can open the page. Every
API call that touches a user's data must carry Telegram's signed ``initData``,
and we verify it exactly the way Telegram specifies:

    secret_key = HMAC_SHA256(key=b"WebAppData", msg=bot_token)
    signature  = HMAC_SHA256(key=secret_key,   msg=data_check_string)

where ``data_check_string`` is the remaining fields sorted alphabetically and
joined with ``\\n``. Comparison is constant-time, and stale payloads are
rejected. No user id is ever taken from a plain query parameter.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional
from urllib.parse import parse_qsl

from ..logging_setup import get_logger

log = get_logger("web.security")

MAX_AUTH_AGE = 24 * 3600  # Telegram recommends rejecting day-old payloads


@dataclass(frozen=True)
class TelegramUser:
    id: int
    first_name: str = ""
    last_name: str = ""
    username: str = ""
    language_code: str = ""
    is_premium: bool = False

    @classmethod
    def from_payload(cls, payload: Dict[str, Any]) -> "TelegramUser":
        return cls(
            id=int(payload.get("id") or 0),
            first_name=str(payload.get("first_name") or "")[:64],
            last_name=str(payload.get("last_name") or "")[:64],
            username=str(payload.get("username") or "")[:64],
            language_code=str(payload.get("language_code") or "")[:8],
            is_premium=bool(payload.get("is_premium")),
        )


def _secret_key(bot_token: str) -> bytes:
    return hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()


def validate_init_data(
    init_data: str,
    bot_token: str,
    *,
    max_age: int = MAX_AUTH_AGE,
    now: Optional[float] = None,
) -> Optional[TelegramUser]:
    """Return the verified :class:`TelegramUser`, or ``None`` if invalid."""
    if not init_data or not bot_token:
        return None
    try:
        pairs = dict(parse_qsl(init_data, strict_parsing=True, keep_blank_values=True))
    except ValueError:
        log.warning("initData could not be parsed")
        return None

    received = pairs.pop("hash", "")
    if not received:
        return None

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    expected = hmac.new(
        _secret_key(bot_token), data_check_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(expected, received):
        log.warning("initData signature mismatch")
        return None

    try:
        auth_date = int(pairs.get("auth_date") or 0)
    except ValueError:
        return None
    if max_age and (now or time.time()) - auth_date > max_age:
        log.warning("initData expired", extra={"age_s": int(time.time()) - auth_date})
        return None

    try:
        user_payload = json.loads(pairs.get("user") or "{}")
    except ValueError:
        return None
    user = TelegramUser.from_payload(user_payload)
    if user.id <= 0:
        return None
    return user
