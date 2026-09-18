"""HTTP layer: Telegram Mini App + health/metrics endpoints.

Why a web service and not a worker?
-----------------------------------
Render's **free plan has no background workers** - the only runnable service
type is a Web Service, and it must bind ``0.0.0.0:$PORT`` and answer HTTP or
Render kills/restarts it. So the Pyrogram bot and this aiohttp app run inside
the *same* event loop and the *same* process: the web server keeps the service
"healthy" for Render, and the bot does the actual work.
"""

from .app import create_app
from .security import TelegramUser, validate_init_data

__all__ = ["create_app", "validate_init_data", "TelegramUser"]
