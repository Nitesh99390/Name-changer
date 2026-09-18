"""Typed, user-safe error hierarchy.

Every error raised inside a handler must be one of these (or a subclass of
``BotError``) so that the central handler can decide what to show the user and
what to log. Anything unexpected is caught by the global guard as
``InternalError`` and reported with a short, non-leaking message.
"""

from __future__ import annotations


class BotError(Exception):
    """Base class for all expected/handled failures.

    ``user_message`` is safe to show in Telegram. ``code`` is a stable,
    machine-friendly identifier that is also stored in Supabase.
    """

    code = "bot_error"
    user_message = "Something went wrong while processing your request."

    def __init__(self, detail: str = "", *, user_message: str | None = None) -> None:
        self.detail = detail or self.__class__.__doc__ or self.code
        if user_message:
            self.user_message = user_message
        super().__init__(self.detail)

    def as_text(self) -> str:
        return f"{self.user_message}\n\n`{self.code}`"


class ConfigError(BotError):
    """Configuration/environment problem."""

    code = "config_error"
    user_message = "The bot is not configured correctly. Please contact the admin."


class NameDataError(BotError):
    """Name resources are missing or empty."""

    code = "name_data_error"
    user_message = "Name lists are unavailable right now. Please try again shortly."


class UnsupportedFileError(BotError):
    """File type is not accepted."""

    code = "unsupported_file"
    user_message = "Only text files (.txt, .md, .text) are accepted."


class FileTooLargeError(BotError):
    """File exceeds the configured ceiling."""

    code = "file_too_large"
    user_message = "That file is too large for the free tier. Please send a smaller one."


class EmptyDocumentError(BotError):
    """Document contains no readable text."""

    code = "empty_document"
    user_message = "That file appears to be empty after decoding."


class EncodingError_(BotError):
    """Could not decode the file with any known encoding."""

    code = "encoding_error"
    user_message = (
        "Could not read that file's text encoding. "
        "Please re-save it as UTF-8 and send it again."
    )


class RateLimitedError(BotError):
    """User is over their hourly quota."""

    code = "rate_limited"

    def __init__(self, retry_after: int, limit: int) -> None:
        self.retry_after = retry_after
        self.limit = limit
        super().__init__(f"rate limit {limit}/h, retry in {retry_after}s")
        self.user_message = (
            f"You've hit the limit of {limit} conversions per hour.\n"
            f"Please try again in about {max(1, retry_after // 60)} minute(s)."
        )


class BusyError(BotError):
    """Another job of this user is still running."""

    code = "busy"
    user_message = "You already have a conversion running. Please wait for it to finish."


class JobTimeoutError(BotError):
    """Conversion exceeded the wall-clock budget."""

    code = "job_timeout"
    user_message = "That file took too long to process. Try splitting it into smaller parts."


class StorageError(BotError):
    """Filesystem / download / upload failure."""

    code = "storage_error"
    user_message = "Could not read or write the file on the server. Please try again."


class InternalError(BotError):
    """Unexpected exception."""

    code = "internal_error"
    user_message = "Unexpected server error. It has been logged - please try again."
