from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

from telegram.ext import Application, ApplicationBuilder, CommandHandler

from nexus_ai_agent.config.settings import Settings
from nexus_ai_agent.presence import PresenceStore
from nexus_ai_agent.storage.ai_storage import AIStorageManager, ProviderConfig

from .handlers import (
    analyze_handler,
    build_handlers,
    companion_handler,
    install_presence_heartbeat,
    persona_handler,
    story_handler,
)


def _build_default_storage(settings: Settings) -> AIStorageManager:
    from pathlib import Path

    return AIStorageManager(
        cache_dir=Path(settings.cache_dir),
        config=ProviderConfig(
            github_token=settings.github_token,
            github_repo=settings.github_repo,
            mega_email=settings.mega_email,
            mega_password=settings.mega_password,
            huggingface_token=settings.huggingface_token,
            rclone_remote=settings.rclone_remote,
            gdrive_bearer_token=settings.gdrive_bearer_token,
        ),
    )


def build_application(
    settings: Settings,
    graph: Any,
    storage: AIStorageManager | None = None,
    *,
    presence: PresenceStore | None = None,
    session_factory: Callable[[], Any] | None = None,
) -> Application:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", settings.telegram_bot_token)
    if not token or token == "CHANGE_ME":
        raise ValueError("TELEGRAM_BOT_TOKEN must be provided via environment/settings")

    presence_store = presence or PresenceStore()
    storage_manager = storage or _build_default_storage(settings)

    application = ApplicationBuilder().token(token).build()
    application.bot_data["graph"] = graph
    application.bot_data["presence"] = presence_store
    application.bot_data["storage"] = storage_manager
    application.bot_data.setdefault("heartbeat_user_ids", set())

    for handler in build_handlers(
        graph,
        db_session_factory=session_factory or _get_session_factory(),
        settings=settings,
        presence=presence_store,
        storage=storage_manager,
    ):
        application.add_handler(handler)
    application.add_handler(CommandHandler("story", story_handler))
    application.add_handler(CommandHandler("companion", companion_handler))
    application.add_handler(CommandHandler("analyze", analyze_handler))
    application.add_handler(CommandHandler("persona", persona_handler))
    install_presence_heartbeat(application)
    return application


def _get_session_factory() -> Callable[[], Any]:
    from nexus_ai_agent.storage.db import get_session

    return get_session
