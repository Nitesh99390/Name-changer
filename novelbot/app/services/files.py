"""File handling: extension guards, encoding detection, bounded-memory reads.

Render's free tier gives an **ephemeral** filesystem and a 512 MB / 0.1 CPU
container, so:

* everything goes under ``/tmp`` (never the repo dir);
* text is streamed in line-aligned chunks, so a 4 MB novel never needs 4 MB of
  Python strings plus a full second copy in memory at once;
* temporary files are always removed in a ``finally`` block.
"""

from __future__ import annotations

import os
import re
import unicodedata
from pathlib import Path
from typing import Iterator, List, Sequence

from ..config import get_settings
from ..errors import EncodingError_, EmptyDocumentError, UnsupportedFileError
from ..logging_setup import get_logger

log = get_logger("services.files")

# Text formats we accept. Anything else is rejected before download.
ALLOWED_EXTENSIONS = frozenset(
    {".txt", ".text", ".md", ".markdown", ".log", ".srt", ".vtt", ".csv", ".json", ".xml", ".html", ".htm", ".rtf"}
)
ALLOWED_MIME_PREFIXES = ("text/", "application/json", "application/xml", "application/octet-stream")

# Tried in order. GB18030 covers Simplified Chinese; Big5 covers Traditional.
ENCODINGS: Sequence[str] = ("utf-8-sig", "utf-8", "gb18030", "big5", "utf-16", "cp1252", "latin-1")

_UNSAFE = re.compile(r"[^A-Za-z0-9._\u0600-\u06FF\u0900-\u097F\u4E00-\u9FFF-]+")
CHUNK_TARGET = 150_000  # characters per processing slice


def safe_name(name: str, *, fallback: str = "novel.txt") -> str:
    """Sanitise an untrusted filename (no path traversal, bounded length)."""
    base = os.path.basename(name or "").strip() or fallback
    base = unicodedata.normalize("NFKC", base)
    base = _UNSAFE.sub("_", base).strip("._") or fallback
    return base[:96]


def temp_path(chat_id: int, kind: str, suffix: str = ".txt") -> Path:
    """Unique, per-chat path under the (ephemeral) data dir."""
    settings = get_settings()
    directory = Path(settings.data_dir) / str(chat_id)
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{kind}_{os.getpid()}{suffix}"


def is_supported_document(document) -> bool:
    """Validate a Pyrogram ``Document`` before spending bandwidth on it."""
    suffix = Path(getattr(document, "file_name", "") or "").suffix.lower()
    mime = (getattr(document, "mime_type", "") or "").lower()
    if suffix in ALLOWED_EXTENSIONS:
        return True
    if not suffix and mime.startswith(ALLOWED_MIME_PREFIXES):
        return True
    # Telegram sometimes reports octet-stream for .txt; trust a text-ish mime.
    return bool(mime.startswith(ALLOWED_MIME_PREFIXES) and suffix in {"", ".dat"})


def assert_supported(document) -> None:
    if not is_supported_document(document):
        allowed = ", ".join(sorted(ALLOWED_EXTENSIONS))
        raise UnsupportedFileError(
            f"unsupported type {getattr(document, 'mime_type', '?')}",
            user_message=f"Only plain-text novels are supported.\nAllowed: {allowed}",
        )


def decode_bytes(raw: bytes) -> str:
    """Decode with a ladder of encodings, then verify the file was not empty."""
    if not raw:
        raise EmptyDocumentError("zero bytes")
    for encoding in ENCODINGS:
        try:
            text = raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
        if text.strip():
            if encoding not in {"utf-8", "utf-8-sig"}:
                log.info("decoded with fallback encoding", extra={"encoding": encoding})
            return text
    raise EncodingError_("no encoding produced readable text")


def iter_text_chunks(path: Path, *, max_chars: int, chunk_size: int = CHUNK_TARGET) -> Iterator[str]:
    """Stream the file in *line-aligned* chunks.

    Line alignment matters: a name must never be split across two chunks, so we
    only cut at ``\\n`` boundaries and carry the remainder forward.
    """
    settings = get_settings()
    max_bytes = settings.max_file_bytes
    size = path.stat().st_size
    if size > max_bytes:
        from ..errors import FileTooLargeError

        raise FileTooLargeError(f"{size} > {max_bytes}")

    buffer: List[str] = []
    buffered_chars = 0
    total = 0

    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            buffer.append(line)
            buffered_chars += len(line)
            total += len(line)
            if buffered_chars >= chunk_size:
                if total > max_chars:
                    from ..errors import FileTooLargeError

                    raise FileTooLargeError(
                        f"{total} chars > {max_chars}",
                        user_message=(
                            "This novel is too large for the free tier "
                            f"({max_chars:,} characters max). Please split it."
                        ),
                    )
                yield "".join(buffer)
                buffer.clear()
                buffered_chars = 0

    if buffer:
        yield "".join(buffer)


def cleanup(*paths: Path | str | None) -> None:
    """Delete temp artefacts. Never raises - cleanup must not break a job."""
    for path in paths:
        if not path:
            continue
        try:
            Path(path).unlink(missing_ok=True)
        except Exception:  # pragma: no cover
            log.warning("cleanup failed", extra={"path": str(path)})


def cleanup_dir(directory: Path | str | None) -> None:
    if not directory:
        return
    try:
        target = Path(directory)
        if not target.exists():
            return
        for child in target.rglob("*"):
            if child.is_file():
                child.unlink(missing_ok=True)
        target.rmdir()
    except Exception:  # pragma: no cover
        log.warning("cleanup_dir failed", extra={"path": str(directory)})


def human_size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024 or unit == "GB":
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} GB"
