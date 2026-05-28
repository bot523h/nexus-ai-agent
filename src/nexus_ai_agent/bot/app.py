from __future__ import annotations

from typing import Any

from telegram.ext import Application, ApplicationBuilder

from nexus_ai_agent.config.settings import Settings

from .handlers import build_handlers


def build_application(settings: Settings, graph: Any) -> Application:
    application = ApplicationBuilder().token(settings.telegram_bot_token).build()
    for handler in build_handlers(graph, db_session_factory=_get_session_factory(), settings=settings):
        application.add_handler(handler)
    return application


def _get_session_factory():
    # Local import to avoid importing SQLAlchemy at module import time.
    from nexus_ai_agent.storage.db import get_session

    return get_session

