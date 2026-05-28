from __future__ import annotations

from typing import Any

from telegram.ext import Application, ApplicationBuilder, CommandHandler

from nexus_ai_agent.config.settings import Settings

from .handlers import (
    analyze_handler,
    build_handlers,
    companion_handler,
    persona_handler,
    story_handler,
)


def build_application(settings: Settings, graph: Any) -> Application:
    application = ApplicationBuilder().token(settings.telegram_bot_token).build()
    application.bot_data["graph"] = graph
    for handler in build_handlers(
        graph,
        db_session_factory=_get_session_factory(),
        settings=settings,
    ):
        application.add_handler(handler)
    application.add_handler(CommandHandler("story", story_handler))
    application.add_handler(CommandHandler("companion", companion_handler))
    application.add_handler(CommandHandler("analyze", analyze_handler))
    application.add_handler(CommandHandler("persona", persona_handler))
    return application


def _get_session_factory():
    # Local import to avoid importing SQLAlchemy at module import time.
    from nexus_ai_agent.storage.db import get_session

    return get_session
