"""Pyrogram client factory.

Hardening notes
---------------
* The session lives in ``/tmp`` (Render's free disk is ephemeral, and a
  ``*.session`` file inside the repo would leak on a public repo).
* ``in_memory=False`` is deliberate: a persistent file avoids re-logging in on
  every deploy, which would otherwise trigger Telegram flood-waits.
* ``sleep_threshold`` lets Pyrogram auto-wait on ``FloodWait`` instead of
  crashing the handler.
* No API id / hash / token is hardcoded anywhere - everything comes from env.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Sequence, Tuple

from pyrogram import Client, enums
from pyrogram.types import BotCommand

from ..config import get_settings
from ..logging_setup import get_logger

log = get_logger("bot.client")

COMMANDS: Sequence[Tuple[str, str]] = (
    ("start", "Start the bot / open the Mini App"),
    ("help", "How to use + supported formats"),
    ("convert", "Convert the novel you reply to"),
    ("settings", "Your personal conversion options"),
    ("stats", "Your usage + global stats"),
    ("cancel", "Cancel the running conversion"),
    ("privacy", "What is stored and what is never stored"),
    ("version", "Build info"),
)


def build_client() -> Client:
    settings = get_settings()
    session_dir = Path(settings.data_dir) / "sessions"
    session_dir.mkdir(parents=True, exist_ok=True)
    session_path = session_dir / "novelbot"

    client = Client(
        name=str(session_path),
        api_id=settings.api_id,
        api_hash=settings.api_hash,
        bot_token=settings.bot_token,
        workers=2,                     # free tier: keep the loop light
        sleep_threshold=30,            # auto-sleep through FloodWait
        max_concurrent_transmissions=2,
        parse_mode=enums.ParseMode.HTML,
        app_version="novel-name-converter 2.0.0",
    )
    log.info("pyrogram client built", extra={"session": str(session_path)})
    return client


async def register_commands(client: Client) -> None:
    """Publish the command list to the Telegram UI."""
    try:
        await client.set_bot_commands(
            [BotCommand(command, description) for command, description in COMMANDS]
        )
        log.info("bot commands registered", extra={"count": len(COMMANDS)})
    except Exception as exc:  # pragma: no cover - non fatal
        log.warning("could not register commands", extra={"err": type(exc).__name__})


async def set_menu_button(client: Client, url: str) -> None:
    """Attach the Mini App to the chat menu button (needs a public HTTPS URL)."""
    if not url or not url.startswith("https://"):
        log.warning("menu button skipped - no https public url", extra={"url": url or "<unset>"})
        return
    try:
        from pyrogram.types import MenuButtonWebApp, WebAppInfo

        await client.set_chat_menu_button(
            menu_button=MenuButtonWebApp(
                text="Name Converter",
                web_app=WebAppInfo(url=url),
            )
        )
        log.info("mini app menu button set", extra={"url": url})
    except Exception as exc:  # pragma: no cover - non fatal
        log.warning("could not set menu button", extra={"err": type(exc).__name__})


async def set_bot_description(client: Client, text: str) -> None:
    try:
        await client.set_bot_description(description=text[:512])
    except Exception as exc:  # pragma: no cover
        log.warning("could not set description", extra={"err": type(exc).__name__})
