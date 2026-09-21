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
from nexus_ai_agent.observability.logging import get_logger
from nexus_ai_agent.presence import PresenceStore
from nexus_ai_agent.storage.ai_storage import AIStorageManager, ProviderConfig

from .handlers import (
    build_handlers,
)

logger = get_logger(__name__)


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
            r2_account_id=settings.r2_account_id,
            r2_access_key_id=settings.r2_access_key_id,
            r2_secret_access_key=settings.r2_secret_access_key,
            r2_bucket=settings.r2_bucket,
        ),
    )


def _bot_token(settings: Settings) -> str:
    """Resolve the bot token exactly like ``build_application`` does."""
    return os.environ.get("TELEGRAM_BOT_TOKEN", settings.telegram_bot_token)


def _build_job_completion_notifier(token: str) -> Any:
    """D4: notify the origin chat when a background job finishes.

    Returns the hook injected into ``InProcessJobQueue``. The queue
    guarantees fail-safety (hook exceptions are logged and swallowed), so
    this only formats and sends. Payloads without an origin ``chat_id``
    (e.g. CLI-drained jobs) stay silent.
    """
    from nexus_ai_agent.adapters.in_process_job_queue import JobCompletion
    from nexus_ai_agent.application.ports.job_queue import JobStatus

    async def _notify(completion: JobCompletion) -> None:
        raw_chat_id = completion.payload.get("chat_id")
        if raw_chat_id is None:
            return
        from telegram import Bot

        if completion.status is JobStatus.FAILED:
            text = (
                f"❌ پردازش «{completion.job_type}» ناموفق بود.\n"
                f"شناسه: {completion.job_id}\n"
                f"خطا: {completion.error or 'نامشخص'}"
            )
        else:
            text = f"✅ پردازش «{completion.job_type}» کامل شد.\nشناسه: {completion.job_id}"
        if completion.job_type == "slideshow_render":
            # Wave 2.5 (D4 extension, r7 item 4): deliver the rendered master
            # and own its cleanup; failures arrive as short mapped messages.
            from nexus_ai_agent.bot.slideshow_notify import notify_slideshow_completion

            await notify_slideshow_completion(completion, token)
            return
        bot = Bot(token=token)
        await bot.send_message(chat_id=int(str(raw_chat_id)), text=text)

    return _notify


def _init_v2_engines(settings: Settings) -> dict[str, Any]:
    """Initialize all v2.0.0+ feature engines.

    Returns a dict suitable for storing in application.bot_data.
    """
    from nexus_ai_agent.adapters.in_process_job_queue import InProcessJobQueue
    from nexus_ai_agent.features.ai_chat import GeminiEngine
    from nexus_ai_agent.features.conversation_store import ConversationStore
    from nexus_ai_agent.features.image_gen import ImageGenEngine
    from nexus_ai_agent.features.referral import ReferralEngine
    from nexus_ai_agent.features.request_queue import GeminiRequestQueue
    from nexus_ai_agent.features.speech import SpeechEngine
    from nexus_ai_agent.features.summarizer import SummarizerEngine
    from nexus_ai_agent.storage.unified_cloud import UnifiedCloudStorage

    engines: dict[str, Any] = {}

    # Application-owned background jobs. The queue is a SQLite sidecar owned
    # by this adapter; execution remains on the bot process event loop.
    # D4: finished jobs notify the origin Telegram chat (fail-safe hook).
    from nexus_ai_agent.worker import default_job_handlers, job_queue_db_path

    job_queue = InProcessJobQueue(
        job_queue_db_path(settings.db_path),
        on_job_finished=_build_job_completion_notifier(_bot_token(settings)),
    )
    for job_type, handler in default_job_handlers().items():
        job_queue.register_handler(job_type, handler)
    engines["job_queue"] = job_queue

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

    # Shared feature-engine container (feature-wiring batch). Reuses the
    # referral instance above so exactly ONE ReferralEngine exists per
    # process (P0-8: single source of truth for documented engines).
    from nexus_ai_agent.bot.feature_handlers import build_feature_engines

    engines["feature_engines"] = build_feature_engines(
        settings, referral=engines["referral_engine"]
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
    job_queue = engines["job_queue"]
    feature_engines = engines["feature_engines"]

    async def _post_init(application: Any) -> None:
        # Bind the runtime bot to the bindable engines (reminders,
        # force-join, anonymous chat) and restore pending reminders so
        # a restart does not silently drop them.
        try:
            feature_engines.reminders.bind(application.bot)
            feature_engines.force_join.bind(application.bot)
            feature_engines.anon.bind(application.bot)
            await feature_engines.reminders.restore_pending()
        except Exception:  # noqa: BLE001 — startup wiring must not kill the bot
            logger.exception("feature_engines_startup_failed")
        await job_queue.resume_pending()

    async def _post_shutdown(application: Any) -> None:
        try:
            feature_engines.reminders.close()
        except Exception:  # noqa: BLE001
            pass

    application = (
        ApplicationBuilder()
        .token(token)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )
    application.bot_data["graph"] = graph
    application.bot_data["presence"] = presence_store
    application.bot_data["storage"] = storage_manager
    application.bot_data.setdefault("heartbeat_user_ids", set())

    # Store engines in bot_data for handler access
    for key, value in engines.items():
        application.bot_data[key] = value

    # P0-2: global deny-by-default access guard, group -1 = before every
    # other handler (commands, callbacks, and free text alike).
    from nexus_ai_agent.bot.access_guard import build_access_guard

    application.add_handler(build_access_guard(settings), group=-1)

    for handler in build_handlers(
        graph,
        db_session_factory=session_factory or _get_session_factory(),
        settings=settings,
        presence=presence_store,
        storage=storage_manager,
        feature_engines=feature_engines,
    ):
        application.add_handler(handler)
    # Custom command handlers removed as they should be part of build_handlers or imported correctly
    # install_presence_heartbeat(application) # Removed as it was an unawaited mock
    return application


def _get_session_factory() -> Callable[[], Any]:
    from nexus_ai_agent.storage.db import get_session

    return get_session


class WebhookApplicationAdapter:
    """Bridge raw Telegram webhook payloads into the PTB Application (v3.8.0).

    Lives here, in ``bot/app.py``, on purpose: the frozen import-boundary
    test (``tests/architecture/test_import_boundaries.py``) only tolerates
    the ``telegram`` package in grandfathered files and this file is one of
    them.  ``api/app.py`` and ``bot/webhook.py`` must stay telegram-free;
    they call this adapter instead.
    """

    def __init__(self, application: Any) -> None:
        self._application = application

    def parse_update(self, payload: dict[str, Any]) -> Any | None:
        """Convert a raw webhook JSON payload into a PTB ``Update``.

        Returns ``None`` when the payload carries no ``update_id``
        (Telegram always sends one; anything else is malformed).  Raises
        for payloads ``Update.de_json`` cannot make sense of.
        """
        from telegram import Update

        if payload.get("update_id") is None:
            # Telegram always includes update_id; PTB's de_json raises
            # (Update.__init__ requires it) for payloads without one —
            # surface that as "no update" so the API answers 400.
            return None
        bot = getattr(self._application, "bot", None)
        # Annotated as Any on purpose: PTB types de_json() as non-optional,
        # but at runtime it returns None for falsy payloads (and may raise
        # for garbage) — exactly the cases this method must surface.
        update: Any = Update.de_json(payload, bot)
        if update is None or update.update_id is None:
            return None
        return update

    def enqueue(self, update: Any) -> None:
        """Hand a parsed ``Update`` to the application's update queue."""
        queue = getattr(self._application, "update_queue", None)
        if queue is None:
            raise RuntimeError("application has no update_queue (not initialized?)")
        queue.put_nowait(update)
