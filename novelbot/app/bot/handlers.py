"""All message handlers.

Registration order matters in Pyrogram: the first matching handler wins, so the
``filters.command`` handlers are declared before the catch-all document
handler, and the document handler itself is the last thing registered.
"""

from __future__ import annotations

import html
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict

from pyrogram import Client, filters
from pyrogram.types import CallbackQuery, Message

from ..config import get_settings
from ..db import repository as repo
from ..errors import (
    BotError,
    BusyError,
    FileTooLargeError,
    InternalError,
    UnsupportedFileError,
)
from ..core.discovery import augment_mapping
from ..logging_setup import get_logger
from ..services.files import (
    assert_supported,
    cleanup,
    cleanup_dir,
    decode_bytes,
    human_size,
    iter_text_chunks,
    safe_name,
    temp_path,
)
from ..services.jobs import get_job_manager
from ..services.ratelimit import get_rate_limiter
from .keyboards import after_result, main_menu, settings_menu
from .middleware import guard, rate_limit, track_user

log = get_logger("bot.handlers")

BOT_USERNAME_HINT = "@YourBotUsername"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _runtime() -> Any:
    """Fetch the shared runtime (registry + converter) from the client."""
    from ..runtime import get_runtime

    return get_runtime()


def _fmt_duration(ms: int) -> str:
    if ms < 1000:
        return f"{ms} ms"
    if ms < 60_000:
        return f"{ms / 1000:.1f} s"
    return f"{ms / 60_000:.1f} min"


def _human_int(value: int) -> str:
    return f"{value:,}"


HELP_TEXT = """<b>📖 Novel Name Converter</b>

Send me a novel / chapter as a <b>text file</b> and I will replace the Chinese
character names with Indian names - consistently, from the first page to the
last.

<b>How to use</b>
1. Send <code>/start</code> once.
2. Send the novel as a <code>.txt</code> / <code>.md</code> file.
3. Get the converted file back in seconds.

<b>Supported</b>: {exts}
<b>Max size</b>: {size} MB

<b>Commands</b>
/convert - convert the file you reply to
/settings - pinyin names, live progress, keep original
/stats - your usage
/cancel - stop the running conversion
/privacy - what is stored
"""


def help_text() -> str:
    settings = get_settings()
    return HELP_TEXT.format(
        exts=", ".join(sorted({".txt", ".md", ".text", ".srt", ".csv"})),
        size=int(settings.max_file_mb),
    )


PRIVACY_TEXT = """<b>🔐 Privacy</b>

<b>Stored (metadata only)</b>
• your Telegram user id, username, language
• job counters: file size, characters processed, replacements, duration
• your preferences (pinyin on/off, etc.)

<b>Never stored</b>
• the novel text you upload
• the converted text
• the uploaded or generated file itself

Files are written to an ephemeral <code>/tmp</code> directory, used for the
duration of the job, and deleted immediately afterwards - they are never
committed to a database or a backup.
"""


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------
@Client.on_message(filters.command("start") & filters.private)
@guard
@track_user
async def cmd_start(client: Client, message: Message) -> None:
    runtime = _runtime()
    settings = get_settings()
    user = message.from_user
    mini_url = settings.webhook_base

    text = (
        f"<b>👋 Namaste, {html.escape(user.first_name or 'there')}!</b>\n\n"
        "I convert Chinese names in your novel into Indian names.\n\n"
        f"<b>Name list ready:</b> <code>{runtime.registry.info()['pairs']}</code> pairs\n"
        f"<b>Version:</b> <code>{runtime.registry.version}</code> "
        f"(<code>{runtime.registry.digest}</code>)\n\n"
        "Just send me your <code>.txt</code> novel file to begin."
    )
    await message.reply(text, reply_markup=main_menu(mini_url))


@Client.on_message(filters.command("help") & filters.private)
@guard
@track_user
async def cmd_help(client: Client, message: Message) -> None:
    await message.reply(help_text(), reply_markup=main_menu())


@Client.on_message(filters.command("privacy") & filters.private)
@guard
@track_user
async def cmd_privacy(client: Client, message: Message) -> None:
    await message.reply(PRIVACY_TEXT)


@Client.on_message(filters.command("version") & filters.private)
@guard
@track_user
async def cmd_version(client: Client, message: Message) -> None:
    from .. import __version__
    from ..runtime import get_runtime

    runtime = get_runtime()
    settings = get_settings()
    await message.reply(
        "<b>Build</b>\n"
        f"<code>app {__version__}</code>\n"
        f"<code>names {runtime.registry.version} / {runtime.registry.digest}</code>\n"
        f"<code>supabase {'on' if settings.storage_enabled else 'off'}</code>\n"
        f"<code>rate {settings.rate_limit_per_hour}/h · concurrency "
        f"{settings.max_concurrent_jobs}</code>"
    )


@Client.on_message(filters.command("stats") & filters.private)
@guard
@track_user
async def cmd_stats(client: Client, message: Message) -> None:
    user = message.from_user
    settings = get_settings()
    remaining = await get_rate_limiter().remaining(user.id)
    history = await repo.user_job_history(user.id, limit=5)

    lines = [
        "<b>📊 Your stats</b>",
        f"<code>quota left : {remaining}/{settings.rate_limit_per_hour} per hour</code>",
    ]
    if history:
        lines.append("")
        for job in history:
            icon = {"done": "✅", "failed": "❌", "running": "⏳"}.get(job.get("status"), "•")
            name = html.escape((job.get("file_name") or "file")[:28])
            chars = _human_int(int(job.get("char_count") or 0))
            repl = _human_int(int(job.get("replacement_count") or 0))
            lines.append(f"{icon} <code>{name}</code> · {chars} ch · {repl} names")
    else:
        lines.append("\nNo conversions yet.")

    if settings.storage_enabled:
        stats = await repo.global_stats()
        lines += [
            "",
            "<b>🌍 Global</b>",
            f"<code>users        : {_human_int(stats['users'])}</code>",
            f"<code>conversions  : {_human_int(stats['jobs_done'])}</code>",
            f"<code>chars total  : {_human_int(stats['chars'])}</code>",
            f"<code>names total  : {_human_int(stats['replacements'])}</code>",
        ]
    await message.reply("\n".join(lines))


@Client.on_message(filters.command("settings") & filters.private)
@guard
@track_user
async def cmd_settings(client: Client, message: Message) -> None:
    current = await repo.get_user_settings(message.from_user.id)
    await message.reply(
        "<b>⚙️ Your settings</b>\n\n"
        "Toggle what you want, then send a novel as usual.",
        reply_markup=settings_menu(current),
    )


@Client.on_message(filters.command("cancel") & filters.private)
@guard
@track_user
async def cmd_cancel(client: Client, message: Message) -> None:
    manager = get_job_manager()
    if manager.is_busy(message.from_user.id):
        await manager.cancel(message.from_user.id)
        await message.reply("🛑 Cancellation requested. The job will stop at the next checkpoint.")
    else:
        await message.reply("Nothing is running right now.")


@Client.on_message(filters.command("convert") & filters.private)
@guard
@track_user
async def cmd_convert(client: Client, message: Message) -> None:
    replied = message.reply_to_message
    if not replied or not replied.document:
        await message.reply(
            "Reply to a <code>.txt</code> file with <code>/convert</code>, "
            "or just send me the file directly."
        )
        return
    await _handle_document(client, replied, replied.document)


@Client.on_message(filters.command(["admin", "reload"]) & filters.private)
@guard
@track_user
async def cmd_admin(client: Client, message: Message) -> None:
    settings = get_settings()
    user = message.from_user
    if not settings.is_admin(user.id):
        await message.reply("⛔️ Admin only.")
        return

    runtime = _runtime()
    parts = (message.text or "").split(maxsplit=1)
    action = parts[1].strip().lower() if len(parts) > 1 else "info"

    if action == "reload":
        seed = f"{settings.names_seed}-{int(time.time())}"
        runtime.registry.refresh(seed)
        runtime.converter.refresh(force=True)
        await repo.set_setting("names_seed", seed)
        info = runtime.registry.info()
        await message.reply(
            f"♻️ Mapping regenerated\n<code>seed {info['seed']}</code>\n"
            f"<code>pairs {info['pairs']}</code>\n<code>{info['checksum']}</code>"
        )
        return

    info = runtime.registry.info()
    manager = get_job_manager()
    await message.reply(
        "<b>🛠 Admin</b>\n"
        f"<code>mapping  {info['version']} / {info['checksum']}</code>\n"
        f"<code>pairs    {info['pairs']} / capacity {info['capacity']}</code>\n"
        f"<code>jobs     {manager.stats()}</code>\n"
        f"<code>db       {'on' if settings.storage_enabled else 'off'}</code>\n"
        f"<code>uptime   {int(time.monotonic() - runtime.started_at)}s</code>\n\n"
        "<code>/admin reload</code> - new name mapping"
    )


# --------------------------------------------------------------------------
# callbacks
# --------------------------------------------------------------------------
@Client.on_callback_query(filters.regex(r"^menu:") & filters.private)
@guard
async def on_menu(client: Client, query: CallbackQuery) -> None:
    action = query.data.split(":", 1)[1]
    if action == "help":
        await query.message.edit_text(help_text(), reply_markup=main_menu())
    elif action == "settings":
        current = await repo.get_user_settings(query.from_user.id)
        await query.message.edit_text(
            "<b>⚙️ Your settings</b>", reply_markup=settings_menu(current)
        )
    elif action == "privacy":
        await query.message.edit_text(PRIVACY_TEXT, reply_markup=main_menu())
    elif action == "stats":
        await query.message.edit_text(
            "Use /stats for the full breakdown.", reply_markup=main_menu()
        )
    elif action in {"back", "another"}:
        await query.message.edit_text(
            "Send me a <code>.txt</code> novel file whenever you're ready.",
            reply_markup=main_menu(),
        )
    await query.answer()


@Client.on_callback_query(filters.regex(r"^set:") & filters.private)
@guard
async def on_setting(client: Client, query: CallbackQuery) -> None:
    key = query.data.split(":", 1)[1]
    current = await repo.get_user_settings(query.from_user.id)
    if key in current:
        current[key] = not bool(current[key])
        await repo.save_user_settings(query.from_user.id, current)
        await query.answer("Saved ✅")
        await query.message.edit_text(
            "<b>⚙️ Your settings</b>", reply_markup=settings_menu(current)
        )
    else:
        await query.answer("Unknown option", show_alert=True)


# --------------------------------------------------------------------------
# document pipeline
# --------------------------------------------------------------------------
@Client.on_message(filters.document & filters.private)
@guard
@track_user
async def on_document(client: Client, message: Message) -> None:
    await _handle_document(client, message, message.document)


async def _handle_document(client: Client, message: Message, document) -> None:
    settings = get_settings()
    runtime = _runtime()
    user = message.from_user
    manager = get_job_manager()

    if manager.is_busy(user.id):
        raise BusyError()

    # ---- cheap validations first: never download a file we will reject ----
    assert_supported(document)
    size = int(getattr(document, "file_size", 0) or 0)
    if size > settings.max_file_bytes:
        raise FileTooLargeError(
            f"{size} > {settings.max_file_bytes}",
            user_message=(
                f"That file is {human_size(size)} - the free tier limit is "
                f"{int(settings.max_file_mb)} MB.\nPlease split it and send the parts."
            ),
        )

    await get_rate_limiter().assert_allowed(user.id)

    original_name = safe_name(getattr(document, "file_name", "") or "novel.txt")
    stem = Path(original_name).stem[:40] or "novel"
    source_path = temp_path(message.chat.id, "source")
    output_path = temp_path(message.chat.id, "converted")

    user_settings = await repo.get_user_settings(user.id)
    keep_original = bool(user_settings.get("keep_original"))
    allow_latin = bool(user_settings.get("latin_names"))
    live_progress = bool(user_settings.get("notify_progress"))

    job_id = await repo.create_job(
        user.id, file_name=original_name, file_size=size, chat_id=message.chat.id
    )
    status = await message.reply(
        f"⏳ <b>Working on</b> <code>{html.escape(original_name)}</code> "
        f"({human_size(size)})…"
    )

    last_edit = 0.0
    result_box: Dict[str, Any] = {}

    async def _progress(text: str) -> None:
        nonlocal last_edit
        if not live_progress:
            return
        now = time.monotonic()
        if now - last_edit < 1.6:
            return
        last_edit = now
        try:
            await status.edit_text(text)
        except Exception:  # pragma: no cover - edit races are harmless
            pass

    async def _work(handle) -> None:
        await client.download_media(document, file_name=str(source_path))
        if not source_path.exists() or source_path.stat().st_size == 0:
            raise UnsupportedFileError("download produced an empty file")

        await _progress("🔤 <b>Reading</b> the novel…")
        raw = source_path.read_bytes()
        text = decode_bytes(raw)
        del raw  # free the bytes copy before conversion

        char_total = len(text)
        if char_total > settings.max_file_chars:
            raise FileTooLargeError(
                f"{char_total} chars",
                user_message=(
                    f"This novel has {_human_int(char_total)} characters; the free "
                    f"tier limit is {_human_int(settings.max_file_chars)}.\n"
                    "Please split the file."
                ),
            )

        await _progress(
            f"🔎 <b>Scanning for character names</b>…\n"
            f"<code>{_human_int(char_total)} characters</code>"
        )

        # Two-pass pipeline: discover the names this novel actually uses, merge
        # them into the curated mapping, then replace in ONE regex pass.
        mapping, stats = augment_mapping(
            runtime.registry,
            text,
            threshold=settings.discovery_threshold,
            max_names=settings.max_pairs,
        )
        converter = runtime.converter_with(
            allow_latin, mapping, str(stats.get("checksum") or runtime.registry.digest)
        )

        await _progress(
            f"⚡ <b>Converting names</b>…\n"
            f"<code>{_human_int(char_total)} characters</code>\n"
            f"<code>{len(converter.mapping)} name keys</code>"
        )

        # Line-aligned chunking keeps memory bounded on 512 MB.
        total_repl = 0
        unique = 0
        top: list = []
        with output_path.open("w", encoding="utf-8", newline="\n") as out:
            for chunk in _split_text(text, max_chars=settings.max_file_chars):
                result = converter.convert(chunk, keep_original=keep_original)
                out.write(result.text)
                total_repl += result.replacements
                unique = max(unique, result.unique_names)
                if result.top_names:
                    top = result.top_names
        del text

        result_box.update(
            replacements=total_repl,
            unique=unique,
            top=top,
            chars=char_total,
            checksum=str(stats.get("checksum") or runtime.registry.digest),
            discovered=int(stats.get("added") or 0),
            found=int(stats.get("found") or 0),
        )
        await _progress("📤 <b>Uploading</b> the converted file…")

    try:
        await manager.run(user.id, message.chat.id, _work, job_id=job_id)
    except BotError as exc:
        await repo.finish_job(
            job_id,
            status="failed",
            duration_ms=0,
            error_code=exc.code,
            mapping_version=runtime.registry.version,
            user_id=user.id,
        )
        cleanup(source_path, output_path)
        await _edit_or_send(status, exc.as_text())
        return
    except Exception as exc:  # noqa: BLE001
        log.exception("document job crashed", extra={"user_id": user.id})
        await repo.finish_job(
            job_id,
            status="failed",
            error_code=InternalError.code,
            mapping_version=runtime.registry.version,
            user_id=user.id,
        )
        cleanup(source_path, output_path)
        await _edit_or_send(status, InternalError().as_text())
        return

    try:
        out_name = f"{stem}_converted.txt"
        checksum_used = result_box.get("checksum") or runtime.registry.digest
        caption = (
            "🎉 <b>Conversion complete</b>\n\n"
            f"<code>names replaced  : {_human_int(result_box.get('replacements', 0))}</code>\n"
            f"<code>distinct names  : {result_box.get('unique', 0)}</code>\n"
            f"<code>new names found : {result_box.get('discovered', 0)}</code>\n"
            f"<code>mapping         : {runtime.registry.version}/{checksum_used}</code>"
        )
        top = result_box.get("top") or []
        if top:
            sample = " · ".join(f"{html.escape(str(o))}→{html.escape(str(n))}" for o, n, _ in top[:4])
            caption += f"\n\n<b>Top:</b> {sample}"
        if not result_box.get("replacements"):
            caption += (
                "\n\n⚠️ <b>No names were matched.</b> The file came back unchanged.\n"
                "This usually means the novel uses names outside the built-in pool. "
                "Try lowering <code>DISCOVERY_THRESHOLD</code> or add names to "
                "<code>resources/</code>."
            )

        await client.send_document(
            message.chat.id,
            str(output_path),
            file_name=out_name,
            caption=caption,
            reply_markup=after_result(),
        )
        await repo.finish_job(
            job_id,
            status="done",
            char_count=int(result_box.get("chars") or 0),
            replacement_count=int(result_box.get("replacements") or 0),
            duration_ms=0,
            mapping_version=runtime.registry.version,
            user_id=user.id,
        )
        await _safe_delete(status)
    finally:
        cleanup(source_path, output_path)


def _split_text(text: str, *, max_chars: int, chunk_size: int = 150_000):
    """Line-aligned chunker so a name is never cut in half."""
    start = 0
    length = len(text)
    while start < length:
        end = min(start + chunk_size, length)
        if end < length:
            newline = text.rfind("\n", start, end)
            if newline > start:
                end = newline + 1
        yield text[start:end]
        start = end


async def _edit_or_send(status: Message, text: str) -> None:
    try:
        await status.edit_text(text)
    except Exception:  # pragma: no cover
        try:
            await status.reply(text)
        except Exception:
            pass


async def _safe_delete(message: Message) -> None:
    try:
        await message.delete()
    except Exception:  # pragma: no cover
        pass


# --------------------------------------------------------------------------
# registration
# --------------------------------------------------------------------------
def register_handlers(client: Client) -> int:
    """Attach every decorated handler in this module to ``client``.

    ``@Client.on_message`` used at class level does NOT register anything by
    itself in Pyrogram 2.x - it only stores ``(handler, group)`` tuples on the
    function's ``handlers`` attribute and relies on the plugin loader to pick
    them up. We do not use the plugin system (it needs an importable package
    path that differs between local runs, Docker and Render), so we collect
    the handlers explicitly. Module definition order is preserved, which keeps
    the catch-all document handler last.
    """
    registered = 0
    for obj in list(globals().values()):
        entries = getattr(obj, "handlers", None)
        if not entries or not callable(obj):
            continue
        for entry in entries:
            try:
                handler, group = entry
            except (TypeError, ValueError):
                continue
            client.add_handler(handler, group)
            registered += 1
    log.info("handlers registered", extra={"count": registered})
    return registered
