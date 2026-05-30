"""NEXUS AI Telegram Bot — Application builder.

v2.1: All engines (Gemini, Image, Speech, Referral, Cloud, Queue)
are initialized here and passed to handlers via bot_data.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

from telegram.ext import Application, ApplicationBuilder

from nexus_ai_agent.config.settings import Settings
from nexus_ai_agent.presence import PresenceStore
from nexus_ai_agent.storage.ai_storage import AIStorageManager, ProviderConfig

from .handlers import (
    build_handlers,
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


def _init_v2_engines(settings: Settings) -> dict[str, Any]:
    """Initialize all v2.0.0+ feature engines.

    Returns a dict suitable for storing in application.bot_data.
    """
    from nexus_ai_agent.features.ai_chat import GeminiEngine
    from nexus_ai_agent.features.conversation_store import ConversationStore
    from nexus_ai_agent.features.image_gen import ImageGenEngine
    from nexus_ai_agent.features.referral import ReferralEngine
    from nexus_ai_agent.features.request_queue import GeminiRequestQueue
    from nexus_ai_agent.features.speech import SpeechEngine
    from nexus_ai_agent.features.summarizer import SummarizerEngine
    from nexus_ai_agent.storage.unified_cloud import UnifiedCloudStorage

    engines: dict[str, Any] = {}

    # Persistent conversation store
    conv_store = ConversationStore(db_path=settings.db_path)
    engines["conversation_store"] = conv_store

    # Request queue for fair Gemini API access
    request_queue = GeminiRequestQueue(
        max_rpm=settings.gemini_max_rpm,
        max_daily=settings.gemini_max_daily,
    )
    engines["request_queue"] = request_queue

    # Gemini AI Engine
    gemini_engine: GeminiEngine | None = None
    if settings.gemini_api_key:
        gemini_engine = GeminiEngine(
            api_key=settings.gemini_api_key,
            model=settings.gemini_model,
            max_rpm=settings.gemini_max_rpm,
            max_daily=settings.gemini_max_daily,
            conversation_store=conv_store,
            request_queue=request_queue,
        )
    engines["gemini_engine"] = gemini_engine

    # Image Generation
    engines["image_engine"] = ImageGenEngine()

    # Speech (TTS/STT)
    engines["speech_engine"] = SpeechEngine(output_dir="data/audio")

    # Summarizer
    summarizer_engine: SummarizerEngine | None = None
    if settings.gemini_api_key:
        summarizer_engine = SummarizerEngine(
            gemini_api_key=settings.gemini_api_key,
            model=settings.gemini_model,
        )
    engines["summarizer_engine"] = summarizer_engine

    # Referral
    engines["referral_engine"] = ReferralEngine(db_path=settings.db_path)

    # Unified Cloud Storage
    engines["unified_cloud"] = UnifiedCloudStorage(
        dropbox_token=settings.dropbox_token,
        pcloud_token=settings.pcloud_token,
        internxt_token=settings.internxt_token,
    )

    return engines


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

    # Initialize all v2.0.0+ engines
    engines = _init_v2_engines(settings)

    application = ApplicationBuilder().token(token).build()
    application.bot_data["graph"] = graph
    application.bot_data["presence"] = presence_store
    application.bot_data["storage"] = storage_manager
    application.bot_data.setdefault("heartbeat_user_ids", set())

    # Store engines in bot_data for handler access
    for key, value in engines.items():
        application.bot_data[key] = value

    for handler in build_handlers(
        graph,
        db_session_factory=session_factory or _get_session_factory(),
        settings=settings,
        presence=presence_store,
        storage=storage_manager,
    ):
        application.add_handler(handler)
    # Custom command handlers removed as they should be part of build_handlers or imported correctly
    # install_presence_heartbeat(application) # Removed as it was an unawaited mock
    return application


def _get_session_factory() -> Callable[[], Any]:
    from nexus_ai_agent.storage.db import get_session

    return get_session
