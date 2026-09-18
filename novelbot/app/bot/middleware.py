"""Cross-cutting decorators: global error guard + user bookkeeping.

The original bot let any exception bubble into Pyrogram's loop, which on Render
silently killed the worker. ``guard`` makes that impossible: every handler is
wrapped, every failure is logged with a job id, and the user always gets a
human-readable reply instead of dead silence.
"""

from __future__ import annotations

import functools
import time
import uuid
from typing import Any, Awaitable, Callable

from pyrogram import Client
from pyrogram.errors import RPCError
from pyrogram.types import Message

from ..db import repository as repo
from ..errors import BotError, InternalError
from ..logging_setup import get_logger

log = get_logger("bot.middleware")

Handler = Callable[[Client, Message], Awaitable[Any]]


def guard(func: Handler) -> Handler:
    """Wrap a message handler so it can never crash the process."""

    @functools.wraps(func)
    async def wrapper(client: Client, message: Message, *args: Any, **kwargs: Any):
        trace_id = uuid.uuid4().hex[:10]
        user = getattr(message, "from_user", None)
        started = time.monotonic()
        extra = {
            "trace": trace_id,
            "handler": func.__name__,
            "user_id": getattr(user, "id", None),
            "chat_id": getattr(message, "chat", None) and message.chat.id,
        }
        try:
            return await func(client, message, *args, **kwargs)
        except BotError as exc:
            log.warning("handled error", extra={**extra, "code": exc.code, "detail": exc.detail})
            await _safe_reply(message, exc.as_text(), trace_id)
        except RPCError as exc:
            # Telegram-side failures (blocked bot, deleted message, flood wait).
            log.warning("telegram rpc error", extra={**extra, "err": type(exc).__name__})
            await _safe_reply(
                message,
                "Telegram rejected that request. Please try again in a moment.\n\n"
                f"`{type(exc).__name__}`",
                trace_id,
            )
        except asyncio_cancelled():
            raise
        except Exception as exc:  # noqa: BLE001 - this is the point of the guard
            log.exception("unhandled error", extra={**extra, "err": type(exc).__name__})
            await _safe_reply(message, InternalError().as_text(), trace_id)
        finally:
            log.info(
                "handler finished",
                extra={**extra, "ms": int((time.monotonic() - started) * 1000)},
            )

    return wrapper


def asyncio_cancelled() -> type[BaseException]:
    """Late import so this module stays importable in odd environments."""
    import asyncio

    return asyncio.CancelledError


async def _safe_reply(message: Message, text: str, trace_id: str) -> None:
    """Reply even if the original message was deleted mid-flight."""
    try:
        await message.reply(f"{text}\n\n<code>ref: {trace_id}</code>")
    except Exception:  # pragma: no cover - nothing else we can do
        log.warning("could not deliver error reply", extra={"trace": trace_id})


def track_user(func: Handler) -> Handler:
    """Upsert the caller into Supabase before the handler body runs."""

    @functools.wraps(func)
    async def wrapper(client: Client, message: Message, *args: Any, **kwargs: Any):
        user = getattr(message, "from_user", None)
        if user is not None:
            try:
                await repo.touch_user(
                    user.id,
                    username=getattr(user, "username", None),
                    first_name=getattr(user, "first_name", None),
                    last_name=getattr(user, "last_name", None),
                    language_code=getattr(user, "language_code", None),
                )
            except Exception:  # pragma: no cover - never block a user on analytics
                log.warning("touch_user failed", extra={"user_id": getattr(user, "id", None)})
        return await func(client, message, *args, **kwargs)

    return wrapper


def rate_limit(func: Handler) -> Handler:
    """Apply the shared limiter before doing any work."""
    from ..services.ratelimit import get_rate_limiter

    @functools.wraps(func)
    async def wrapper(client: Client, message: Message, *args: Any, **kwargs: Any):
        user = getattr(message, "from_user", None)
        if user is not None:
            await get_rate_limiter().assert_allowed(user.id)
        return await func(client, message, *args, **kwargs)

    return wrapper
