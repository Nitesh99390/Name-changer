"""Inline keyboards for the Mini App + settings."""

from __future__ import annotations

from pyrogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    WebAppInfo,
)

from ..config import get_settings
from ..db.repository import DEFAULT_USER_SETTINGS


def main_menu(mini_app_url: str = "") -> InlineKeyboardMarkup:
    settings = get_settings()
    url = mini_app_url or settings.webhook_base
    rows: list[list[InlineKeyboardButton]] = []
    if url.startswith("https://"):
        rows.append([InlineKeyboardButton("🚀 Open Mini App", web_app=WebAppInfo(url=url))])
    rows.append(
        [
            InlineKeyboardButton("❓ Help", callback_data="menu:help"),
            InlineKeyboardButton("⚙️ Settings", callback_data="menu:settings"),
        ]
    )
    rows.append([InlineKeyboardButton("🔐 Privacy", callback_data="menu:privacy")])
    return InlineKeyboardMarkup(rows)


def settings_menu(current: dict) -> InlineKeyboardMarkup:
    def mark(key: str) -> str:
        return "✅" if current.get(key) else "⬜️"

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    f"{mark('latin_names')} Pinyin / Latin names",
                    callback_data="set:latin_names",
                )
            ],
            [
                InlineKeyboardButton(
                    f"{mark('notify_progress')} Live progress",
                    callback_data="set:notify_progress",
                )
            ],
            [
                InlineKeyboardButton(
                    f"{mark('keep_original')} Keep original in brackets",
                    callback_data="set:keep_original",
                )
            ],
            [InlineKeyboardButton("↩️ Back", callback_data="menu:back")],
        ]
    )


def after_result() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🔁 Convert another", callback_data="menu:another"),
                InlineKeyboardButton("📊 Stats", callback_data="menu:stats"),
            ]
        ]
    )
