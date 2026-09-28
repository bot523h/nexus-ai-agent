"""NEXUS AI Telegram Bot — Application builder.

W1 True Runtime Closure + W3 Memory Trust — Production/World-Class.

All engines (Gemini, Image, Speech, Referral, Cloud, Queue) are initialized here
and passed to handlers via bot_data. Every long-lived resource is registered
with a canonical Runtime container whose shutdown hook runs ALL cleanup steps
in LIFO order, fail-safe, shielded, joining inner tasks even on CancelledError.

There is exactly ONE startup authority (post_init) and ONE shutdown authority
(post_shutdown); webhook mode must not duplicate lifecycle steps and must
explicitly call post_init/post_shutdown because PTB initialize()/shutdown()
do NOT call them (only run_polling/run_webhook do).
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from telegram.ext import Application, ApplicationBuilder

from nexus_ai_agent.application.ports.job_queue import JobStatus
from nexus_ai_agent.config.settings import Settings
from nexus_ai_agent.core.runtime import Runtime, shutdown_module_sync_engines
from nexus_ai_agent.observability.logging import get_logger
from nexus_ai_agent.presence import PresenceStore
from nexus_ai_agent.storage.ai_storage import AIStorageManager, ProviderConfig

from .handlers import build_handlers

logger = get_logger(__name__)


def _build_default_storage(settings: Settings) -> AIStorageManager:
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
    return os.environ.get("TELEGRAM_BOT_TOKEN", settings.telegram_bot_token)


def _failure_class_line(status: Any, lang: str) -> str:
    if status is JobStatus.FAILED_RETRYABLE:
        head = (
            "⚠️ شکست موقت (قابل تکرار)"
            if lang.startswith("fa")
            else "⚠️ Temporary failure (may be retried)"
        )
    else:
        head = "❌ شکست قطعی" if lang.startswith("fa") else "❌ Terminal failure"
    return head + "\n"


async def _notify_creative_completion(completion: Any, token: str) -> None:
    from telegram import Bot

    from nexus_ai_agent.creative.render_jobs import ERROR_CODES, cleanup_workspace
    from nexus_ai_agent.i18n import i18n
    from nexus_ai_agent.jobs.failure_semantics import parse_typed_failure_error

    payload = completion.payload or {}
    result = completion.result or {}
    lang = i18n.detect_language(str(payload.get("lang") or "en"))
    chat_id = int(payload.get("chat_id") or 0)
    if not chat_id:
        return
    bot = Bot(token=token)
    try:
        failed = completion.status is not JobStatus.COMPLETED or result.get("success") is False
        command = str(payload.get("command", "edit"))
        operation = str(payload.get("operation", ""))
        if failed:
            code = str(
                result.get("error_code")
                or parse_typed_failure_error(completion.error)
                or "internal"
            )
            key = f"creative.failed.{code}" if code in ERROR_CODES else "creative.failed.internal"
            class_line = _failure_class_line(completion.status, lang)
            text = class_line + i18n.t(
                key,
                lang=lang,
                detail=str(result.get("error_detail") or "—"),
            )
            await bot.send_message(chat_id=chat_id, text=text)
            return
        caption = i18n.t("creative.completed", lang=lang, command=command, operation=operation)
        artifact = result.get("artifact_path")
        if artifact and Path(str(artifact)).exists():
            path = Path(str(artifact))
            if result.get("artifact_kind") == "video":
                duration_us = result.get("duration_us")
                with path.open("rb") as handle:
                    await bot.send_video(
                        chat_id=chat_id,
                        video=handle,
                        caption=caption,
                        duration=int(duration_us / 1_000_000) if duration_us else None,
                    )
            else:
                with path.open("rb") as handle:
                    await bot.send_document(chat_id=chat_id, document=handle, caption=caption)
        else:
            await bot.send_message(chat_id=chat_id, text=caption)
    finally:
        cleanup_workspace({**payload, **result})
        try:
            await bot.shutdown()
        except Exception:
            pass


def _build_job_completion_notifier(token: str) -> Any:
    from nexus_ai_agent.adapters.in_process_job_queue import JobCompletion
    from nexus_ai_agent.application.ports.job_queue import JobStatus

    async def _notify(completion: JobCompletion) -> None:
        raw_chat_id = completion.payload.get("chat_id")
        if raw_chat_id is None:
            return
        from telegram import Bot

        if completion.status is JobStatus.FAILED_RETRYABLE:
            text = (
                f"⚠️ پردازش «{completion.job_type}» ناموفق بود (قابل تکرار).\n"
                f"شناسه: {completion.job_id}\n"
                f"خطا: {completion.error or 'نامشخص'}"
            )
        elif completion.status is JobStatus.FAILED_TERMINAL:
            text = (
                f"❌ پردازش «{completion.job_type}» با شکست قطعی پایان یافت.\n"
                f"شناسه: {completion.job_id}\n"
                f"خطا: {completion.error or 'نامشخص'}"
            )
        elif completion.status is JobStatus.COMPLETED:
            text = f"✅ پردازش «{completion.job_type}» کامل شد.\nشناسه: {completion.job_id}"
        else:
            logger.info(
                "job %s notified in non-terminal state %s — staying silent",
                completion.job_id,
                completion.status,
            )
            return
        if completion.job_type == "slideshow_render":
            from nexus_ai_agent.bot.slideshow_notify import notify_slideshow_completion

            await notify_slideshow_completion(completion, token)
            return
        if completion.job_type == "creative_render":
            await _notify_creative_completion(completion, token)
            return
        bot = Bot(token=token)
        try:
            await bot.send_message(chat_id=int(str(raw_chat_id)), text=text)
        finally:
            try:
                await bot.shutdown()
            except Exception:
                pass

    return _notify


async def _safe_aclose(obj: Any) -> None:
    closer = getattr(obj, "aclose", None) or getattr(obj, "close", None)
    if closer is None:
        return
    try:
        result = closer()
        if hasattr(result, "__await__"):
            await result
    except Exception:
        logger.warning("runtime_resource_close_failed", exc_info=True)


def _init_v2_engines(
    settings: Settings, runtime: Runtime, long_term_memory: Any | None = None
) -> dict[str, Any]:
    """Initialize all v2.0.0+ feature engines and register cleanup.

    Every engine that creates resources is registered with runtime so shutdown
    disposes them deterministically, fail-safe. Own-what-you-create (W1 Law 5).
    Engines dict is assigned early to runtime.engines to avoid partial leak.
    Cleanup lambdas capture engine directly via closure, not via dict lookup.
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
    # Assign early — if later construction fails, shutdown still sees earlier engines
    runtime.engines = engines

    from nexus_ai_agent.worker import default_job_handlers, job_queue_db_path

    job_queue = InProcessJobQueue(
        job_queue_db_path(settings.db_path),
        on_job_finished=_build_job_completion_notifier(_bot_token(settings)),
    )
    for job_type, handler in default_job_handlers().items():
        job_queue.register_handler(job_type, handler)
    engines["job_queue"] = job_queue
    runtime.add_cleanup(lambda rt, q=job_queue: q.shutdown())

    conv_store = ConversationStore(db_path=settings.db_path)
    engines["conversation_store"] = conv_store
    runtime.add_cleanup(lambda rt, cs=conv_store: asyncio.to_thread(cs.close))

    request_queue = GeminiRequestQueue(
        max_rpm=settings.gemini_max_rpm,
        max_daily=settings.gemini_max_daily,
    )
    engines["request_queue"] = request_queue
    runtime.add_cleanup(lambda rt, rq=request_queue: rq.close())

    # Gemini AI Engine + Provider — ONE engine, ONE provider (W1 Law 4,5,8)
    from nexus_ai_agent.llm.gemini_provider import GeminiProvider

    gemini_engine: GeminiEngine | None = None
    gemini_provider: GeminiProvider | None = None
    if settings.gemini_api_key:
        gemini_engine = GeminiEngine(
            api_key=settings.gemini_api_key,
            model=settings.gemini_model,
            max_rpm=settings.gemini_max_rpm,
            max_daily=settings.gemini_max_daily,
            conversation_store=conv_store,
            request_queue=request_queue,
        )
        gemini_provider = GeminiProvider(engine=gemini_engine)
    engines["gemini_engine"] = gemini_engine
    engines["gemini_provider"] = gemini_provider

    # Image Generation (no long-lived resources)
    image_engine = ImageGenEngine()
    engines["image_engine"] = image_engine

    # Speech (TTS/STT)
    speech_engine = SpeechEngine(output_dir="data/audio")
    engines["speech_engine"] = speech_engine

    # Summarizer — owns httpx.AsyncClient
    summarizer_engine: SummarizerEngine | None = None
    if settings.gemini_api_key:
        summarizer_engine = SummarizerEngine(
            gemini_api_key=settings.gemini_api_key,
            model=settings.gemini_model,
        )
    engines["summarizer_engine"] = summarizer_engine
    if summarizer_engine is not None:
        runtime.add_cleanup(lambda rt, s=summarizer_engine: _safe_aclose(s))

    # Referral — owns sync SQLite engine
    referral_engine = ReferralEngine(db_path=settings.db_path)
    engines["referral_engine"] = referral_engine
    runtime.add_cleanup(lambda rt, r=referral_engine: asyncio.to_thread(r.close))

    # Unified Cloud Storage (no long-lived)
    unified_cloud = UnifiedCloudStorage(
        dropbox_token=settings.dropbox_token,
        pcloud_token=settings.pcloud_token,
        internxt_token=settings.internxt_token,
    )
    engines["unified_cloud"] = unified_cloud

    # Shared feature-engine container — reuses referral and runtime provider
    from nexus_ai_agent.bot.feature_handlers import build_feature_engines

    feature_engines = build_feature_engines(
        settings, referral=referral_engine, gemini_provider=gemini_provider
    )
    engines["feature_engines"] = feature_engines

    # ReminderSystem has its own sync engine and scheduled tasks
    # Use aclose for proper task joining
    runtime.add_cleanup(lambda rt, fe=feature_engines: fe.reminders.aclose())

    # Feature sync engines (force_join, anonymous_chat) — lru_cache-style caches
    runtime.add_cleanup(lambda rt: asyncio.to_thread(shutdown_module_sync_engines))

    # LongTermMemory (legacy) — if provided, ensure its sqlite conn is closed
    if long_term_memory is not None:
        engines["long_term_memory"] = long_term_memory
        runtime.add_cleanup(lambda rt, lt=long_term_memory: _safe_aclose(lt))

    return engines


def build_application(
    settings: Settings,
    graph: Any,
    storage: AIStorageManager | None = None,
    *,
    presence: PresenceStore | None = None,
    session_factory: Callable[[], Any] | None = None,
    long_term_memory: Any | None = None,
) -> Application:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", settings.telegram_bot_token)
    if not token or token == "CHANGE_ME":
        raise ValueError("TELEGRAM_BOT_TOKEN must be provided via environment/settings")

    presence_store = presence or PresenceStore()
    storage_manager = storage or _build_default_storage(settings)

    # W1 canonical runtime: single authority for construction + shutdown
    runtime = Runtime(settings=settings)

    # Initialize all v2.0.0+ engines; every long-lived resource registers
    # its cleanup with the runtime before the application is built.
    engines = _init_v2_engines(settings, runtime, long_term_memory=long_term_memory)
    # runtime.engines already assigned inside _init_v2_engines, but keep reference
    runtime.engines = engines
    job_queue = engines["job_queue"]
    feature_engines = engines["feature_engines"]

    async def _post_init(application: Any) -> None:
        """ONE canonical startup hook (W1 Law 4)."""
        try:
            feature_engines.reminders.bind(application.bot)
            feature_engines.force_join.bind(application.bot)
            feature_engines.anon.bind(application.bot)
            await feature_engines.reminders.restore_pending()
        except Exception:
            logger.exception("feature_engines_startup_failed")
        try:
            await job_queue.resume_pending()
        except Exception:
            logger.exception("job_queue_resume_pending_failed")

    async def _post_shutdown(application: Any) -> None:
        """ONE canonical shutdown hook (W1 Law 4)."""
        # W1 Fix: close ChannelManager tasks before runtime global dispose
        try:
            cm = application.bot_data.get("channel_manager")
            if cm is not None:
                await _safe_aclose(cm)
        except Exception:
            logger.exception("channel_manager_shutdown_failed")
        try:
            await runtime.shutdown()
        except Exception:
            logger.exception("runtime_shutdown_failed")

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
    application.bot_data["runtime"] = runtime
    application.bot_data.setdefault("heartbeat_user_ids", set())

    for key, value in engines.items():
        application.bot_data[key] = value

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

    from telegram.ext import CommandHandler as _CommandHandler

    from nexus_ai_agent.bot.creative_surface import build_creative_handlers

    for _name, _fn in build_creative_handlers(job_queue).items():
        application.add_handler(_CommandHandler(_name, _fn))
    return application


def _get_session_factory() -> Callable[[], Any]:
    from nexus_ai_agent.storage.db import get_session

    return get_session


class WebhookApplicationAdapter:
    """Bridge raw Telegram webhook payloads into the PTB Application (v3.8.0)."""

    def __init__(self, application: Any) -> None:
        self._application = application

    def parse_update(self, payload: dict[str, Any]) -> Any | None:
        from telegram import Update

        if payload.get("update_id") is None:
            return None
        bot = getattr(self._application, "bot", None)
        update: Any = Update.de_json(payload, bot)
        if update is None or update.update_id is None:
            return None
        return update

    def enqueue(self, update: Any) -> None:
        queue = getattr(self._application, "update_queue", None)
        if queue is None:
            raise RuntimeError("application has no update_queue (not initialized?)")
        queue.put_nowait(update)
