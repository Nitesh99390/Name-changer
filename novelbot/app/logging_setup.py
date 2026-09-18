"""Structured logging with secret redaction.

Render captures stdout/stderr, so we log JSON lines by default (``LOG_JSON=1``)
which the Render log viewer and any log drain can parse. A redaction filter
guarantees the bot token / api hash never reach the log stream, even if a
traceback contains them.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import datetime, timezone
from typing import Any, Dict

# --------------------------------------------------------------------------
# Redaction
# --------------------------------------------------------------------------
_TOKEN_PATTERNS = (
    re.compile(r"\b\d{6,}:[A-Za-z0-9_\-]{30,}\b"),          # bot token
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b"),  # JWT
    re.compile(r"(?i)(api_hash|service_key|apikey|authorization)\s*[=:]\s*\S+"),
)


def _redact(text: str) -> str:
    for pattern in _TOKEN_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text


class RedactingFilter(logging.Filter):
    """Scrub secrets from the rendered message and its args."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        if isinstance(record.msg, str):
            record.msg = _redact(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    k: (_redact(v) if isinstance(v, str) else v)
                    for k, v in record.args.items()
                }
            else:
                record.args = tuple(
                    _redact(a) if isinstance(a, str) else a for a in record.args
                )
        return True


# --------------------------------------------------------------------------
# Formatters
# --------------------------------------------------------------------------
_RESERVED = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "taskName", "message",
}


class JsonFormatter(logging.Formatter):
    """One JSON object per line - machine readable, Render friendly."""

    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in _RESERVED or key.startswith("_"):
                continue
            try:
                json.dumps(value)
                payload[key] = value
            except (TypeError, ValueError):
                payload[key] = repr(value)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return _redact(json.dumps(payload, ensure_ascii=False))


class HumanFormatter(logging.Formatter):
    """Readable single-line format for local development."""

    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)-7s %(name)-28s %(message)s", "%H:%M:%S")

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extras = {
            k: v
            for k, v in record.__dict__.items()
            if k not in _RESERVED and not k.startswith("_")
        }
        if extras:
            base += "  " + " ".join(f"{k}={v}" for k, v in extras.items())
        return _redact(base)


# --------------------------------------------------------------------------
# Setup
# --------------------------------------------------------------------------
_CONFIGURED = False


def setup_logging(level: str = "INFO", json_output: bool = True) -> None:
    """Idempotent root-logger configuration."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if json_output else HumanFormatter())
    handler.addFilter(RedactingFilter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Third-party noise control.
    logging.getLogger("pyrogram").setLevel(logging.WARNING)
    logging.getLogger("pyrogram.session").setLevel(logging.ERROR)
    logging.getLogger("pyrogram.connection").setLevel(logging.ERROR)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
