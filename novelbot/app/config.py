"""Central, validated configuration.

Everything the app needs comes from the environment. Nothing is hardcoded -
no API id, no hash, no token, no URL. Missing/invalid values fail fast with a
readable ``ConfigError`` instead of crashing deep inside a handler.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Tuple

from dotenv import load_dotenv

from .errors import ConfigError

# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent
RESOURCES_DIR = BASE_DIR / "resources"

# Render's free tier has an EPHEMERAL disk: only /tmp is guaranteed writable.
# Never write anything permanent outside /tmp on the free plan.
load_dotenv(BASE_DIR / ".env")


# --------------------------------------------------------------------------
# Typed env helpers
# --------------------------------------------------------------------------
def _str(key: str, default: str = "") -> str:
    value = os.getenv(key)
    return default if value is None or not value.strip() else value.strip()


def _int(key: str, default: int) -> int:
    raw = os.getenv(key)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError as exc:  # pragma: no cover - config guard
        raise ConfigError(f"{key} must be an integer (got {raw!r})") from exc


def _float(key: str, default: float) -> float:
    raw = os.getenv(key)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw.strip())
    except ValueError as exc:  # pragma: no cover - config guard
        raise ConfigError(f"{key} must be a number (got {raw!r})") from exc


def _bool(key: str, default: bool) -> bool:
    raw = os.getenv(key)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "y"}


def _csv(key: str) -> Tuple[str, ...]:
    raw = os.getenv(key, "")
    return tuple(part.strip() for part in raw.split(",") if part.strip())


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------
_BOT_TOKEN_RE = re.compile(r"^\d{6,}:[A-Za-z0-9_\-]{30,}$")


@dataclass(frozen=True)
class Settings:
    # --- Telegram ---
    api_id: int
    api_hash: str
    bot_token: str

    # --- Web / hosting ---
    port: int
    public_url: str
    mini_app_url: str
    return_to_bot: str

    # --- Supabase ---
    supabase_url: str
    supabase_key: str
    supabase_timeout: float
    supabase_retries: int
    supabase_enabled: bool

    # --- Limits ---
    max_file_mb: float
    max_file_chars: int
    rate_limit_per_hour: int
    max_concurrent_jobs: int
    job_timeout_seconds: int
    max_key_length: int
    max_pairs: int
    discovery_threshold: int

    # --- Behaviour ---
    allow_latin_single_token: bool
    names_version: str
    names_seed: str
    names_remote_urls: Tuple[str, ...]

    # --- Ops ---
    log_level: str
    log_json: bool
    keepalive_enabled: bool
    keepalive_interval: int
    admin_ids: Tuple[int, ...]
    data_dir: Path = field(default_factory=lambda: Path(_str("DATA_DIR", "/tmp/novelbot")))

    # ---------------------------------------------------------------
    # Derived helpers
    # ---------------------------------------------------------------
    @property
    def max_file_bytes(self) -> int:
        return int(self.max_file_mb * 1024 * 1024)

    @property
    def supabase_configured(self) -> bool:
        return bool(self.supabase_url and self.supabase_key)

    @property
    def storage_enabled(self) -> bool:
        return self.supabase_enabled and self.supabase_configured

    @property
    def webhook_base(self) -> str:
        """Best-effort public base URL, used for the Mini App menu button."""
        if self.mini_app_url:
            return self.mini_app_url
        if self.public_url:
            return self.public_url
        render_url = _str("RENDER_EXTERNAL_URL")
        return render_url or ""

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.admin_ids

    def redacted(self) -> dict:
        """Safe-to-log snapshot. Never emits the hash or the token."""

        def mask(value: str) -> str:
            if not value:
                return "<unset>"
            return value[:4] + "*" * 6 + value[-2:] if len(value) > 8 else "<set>"

        return {
            "api_id": self.api_id,
            "api_hash": mask(self.api_hash),
            "bot_token": mask(self.bot_token),
            "port": self.port,
            "webhook_base": self.webhook_base or "<unset>",
            "supabase": self.storage_enabled,
            "max_file_mb": self.max_file_mb,
            "max_pairs": self.max_pairs,
            "discovery_threshold": self.discovery_threshold,
            "rate_limit_per_hour": self.rate_limit_per_hour,
            "max_concurrent_jobs": self.max_concurrent_jobs,
            "names_version": self.names_version,
            "log_level": self.log_level,
        }


def _load() -> Settings:
    settings = Settings(
        api_id=_int("TELEGRAM_API_ID", 0),
        api_hash=_str("TELEGRAM_API_HASH"),
        bot_token=_str("BOT_TOKEN"),
        port=_int("PORT", 8000),
        public_url=_str("PUBLIC_URL").rstrip("/"),
        mini_app_url=_str("MINI_APP_URL").rstrip("/"),
        return_to_bot=_str("RETURN_TO_BOT", "https://t.me"),
        supabase_url=_str("SUPABASE_URL").rstrip("/"),
        supabase_key=_str("SUPABASE_SERVICE_KEY") or _str("SUPABASE_KEY"),
        supabase_timeout=_float("SUPABASE_TIMEOUT", 8.0),
        supabase_retries=_int("SUPABASE_MAX_RETRIES", 3),
        supabase_enabled=_bool("SUPABASE_ENABLED", True),
        max_file_mb=_float("MAX_FILE_MB", 20.0),
        max_file_chars=_int("MAX_FILE_CHARS", 4_000_000),
        rate_limit_per_hour=_int("RATE_LIMIT_PER_HOUR", 20),
        max_concurrent_jobs=_int("MAX_CONCURRENT_JOBS", 2),
        job_timeout_seconds=_int("JOB_TIMEOUT_SECONDS", 240),
        max_key_length=_int("MAX_KEY_LENGTH", 24),
        max_pairs=_int("MAX_PAIRS", 1500),
        discovery_threshold=_int("DISCOVERY_THRESHOLD", 3),
        allow_latin_single_token=_bool("ALLOW_LATIN_SINGLE_TOKEN", True),
        names_version=_str("NAMES_VERSION", "v2"),
        names_seed=_str("NAMES_SEED", "novelbot-v2"),
        names_remote_urls=_csv("NAMES_REMOTE_URLS"),
        log_level=_str("LOG_LEVEL", "INFO").upper(),
        log_json=_bool("LOG_JSON", True),
        keepalive_enabled=_bool("KEEPALIVE_ENABLED", True),
        keepalive_interval=_int("KEEPALIVE_INTERVAL", 600),
        admin_ids=tuple(int(x) for x in _csv("ADMIN_IDS") if x.lstrip("-").isdigit()),
    )

    problems: list[str] = []
    if settings.api_id <= 0:
        problems.append("TELEGRAM_API_ID is missing or not a positive integer")
    if len(settings.api_hash) < 16:
        problems.append("TELEGRAM_API_HASH is missing or too short")
    if not _BOT_TOKEN_RE.match(settings.bot_token or ""):
        problems.append("BOT_TOKEN is missing or does not look like a bot token")
    if settings.max_file_mb <= 0:
        problems.append("MAX_FILE_MB must be > 0")
    if settings.rate_limit_per_hour <= 0:
        problems.append("RATE_LIMIT_PER_HOUR must be > 0")
    if settings.max_concurrent_jobs < 1:
        problems.append("MAX_CONCURRENT_JOBS must be >= 1")
    if settings.discovery_threshold < 1:
        problems.append("DISCOVERY_THRESHOLD must be >= 1")
    if settings.supabase_enabled and not settings.supabase_configured:
        # Downgrade to a warning: the bot must still boot without a database.
        settings = Settings(**{**settings.__dict__, "supabase_enabled": False})
    if problems:
        raise ConfigError("; ".join(problems))

    return settings


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings accessor."""
    return _load()
