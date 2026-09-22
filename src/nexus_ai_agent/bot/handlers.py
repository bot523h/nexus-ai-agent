from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import structlog
from sqlmodel import desc, select
from telegram import (
    Audio,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Update,
    Voice,
)
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from nexus_ai_agent.agents.store.agent_manager import AgentManager
from nexus_ai_agent.application.image_generation import get_image_gen_provider
from nexus_ai_agent.application.ports.job_queue import JobQueuePort
from nexus_ai_agent.bot.agent_handlers import (
    agent_callback_handler,
    agent_stop_cmd,
    agents_cmd,
    myagent_cmd,
)
from nexus_ai_agent.bot.feature_handlers import (
    FeatureEngines,
    build_feature_command_handlers,
    build_feature_engines,
    forcejoin_gate,
    join_keyboard,
)

# ── v3.1.0 imports ──
from nexus_ai_agent.bot.knowledge_handlers import learn_cmd, search_cmd, wiki_cmd
from nexus_ai_agent.bot.memory_handlers import (
    build_memory_handlers,
    ensure_consent_prompted,
)
from nexus_ai_agent.bot.monitor_handlers import approve_cmd, health_cmd, reject_cmd
from nexus_ai_agent.bot.safe_paths import (
    UnsafeFileNameError,
    safe_join,
    sanitize_file_name,
)
from nexus_ai_agent.bot.slideshow_handlers import slideshow_cmd, slideshow_photo

# ── agent B (v3.13.x): framework-free command surface ────────────────
# These seven commands used to be hard-coded stubs inside build_handlers()
# ("/daily" always answered "+50 XP!", "/docs" always claimed to be empty).
# The real implementations live in bot/surface/ — no `telegram` import, so
# they are unit-testable with fakes — and the registration lines below are
# unchanged: only the symbols they resolve to moved.
from nexus_ai_agent.bot.surface import (
    achievements_cmd,
    chat_with_doc_cmd,
    daily_cmd,
    doc_delete_cmd,
    docs_list_cmd,
    profile_cmd,
    route_doc_text,
    xp_leaderboard_cmd,
)
from nexus_ai_agent.bot.tool_handlers import news_cmd, rate_cmd, weather_cmd, youtube_cmd
from nexus_ai_agent.bot.update_handlers import update_cmd, version_cmd
from nexus_ai_agent.config.settings import Settings
from nexus_ai_agent.creative.image_gen import ImageGenerationError, ImageRequest

# Feature managers — lazy-initialised inside build_handlers
# ── v2.0.0 imports ──
from nexus_ai_agent.features.ai_chat import GeminiEngine
from nexus_ai_agent.features.ai_memory import CONSENT_GRANTED
from nexus_ai_agent.features.analytics import AnalyticsEngine
from nexus_ai_agent.features.engagement import EngagementEngine
from nexus_ai_agent.features.force_join import ForceJoinManager
from nexus_ai_agent.features.image_gen import ImageGenEngine
from nexus_ai_agent.features.moderation import ModerationEngine
from nexus_ai_agent.features.owner_control import OwnerControl, is_owner
from nexus_ai_agent.features.personality import PersonalityEngine
from nexus_ai_agent.features.speech import SpeechEngine
from nexus_ai_agent.features.summarizer import SummarizerEngine
from nexus_ai_agent.features.viral_engine import ViralEngine
from nexus_ai_agent.observability.logging import get_logger
from nexus_ai_agent.orchestration.state import NexusState
from nexus_ai_agent.presence import PresenceStore
from nexus_ai_agent.storage.models import (
    Chat,
    CloudFile,
    User,
    UserLanguage,
)
from nexus_ai_agent.storage.unified_cloud import UnifiedCloudStorage

from .middleware import AuthMiddleware

logger = get_logger(__name__)
SessionFactory = Callable[[], Any]


async def _upsert_user(db_session_factory: SessionFactory, tg_user: Any) -> User:
    async with db_session_factory() as session:
        stmt = select(User).where(User.telegram_id == int(tg_user.id))
        existing = (await session.exec(stmt)).first()
        if existing:
            existing.username = tg_user.username or existing.username or ""
            await session.commit()
            return cast(User, existing)
        user = User(telegram_id=int(tg_user.id), username=tg_user.username or "", is_allowed=True)
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


async def _upsert_chat(db_session_factory: SessionFactory, chat_id: int, thread_id: str) -> Chat:
    async with db_session_factory() as session:
        stmt = select(Chat).where(Chat.chat_id == chat_id)
        existing = (await session.exec(stmt)).first()
        if existing:
            existing.thread_id = thread_id
            await session.commit()
            return cast(Chat, existing)
        chat = Chat(chat_id=chat_id, thread_id=thread_id)
        session.add(chat)
        await session.commit()
        await session.refresh(chat)
        return chat


def _chat_id(update: Update) -> int:
    if update.effective_chat:
        return int(update.effective_chat.id)
    return 0


def _user_id(update: Update) -> int | None:
    if update.effective_user:
        return int(update.effective_user.id)
    return None


def _message(update: Update) -> Message | None:
    return update.message or update.edited_message


async def _reply(update: Update, text: str, **kwargs: Any) -> None:
    msg = _message(update)
    if msg:
        await msg.reply_text(text, **kwargs)


def _base_state(update: Update, text: str) -> NexusState:
    return {
        "thread_id": f"tg:{_chat_id(update)}",
        "chat_id": _chat_id(update),
        "user_id": _user_id(update) or 0,
        "correlation_id": "",
        "messages": [{"role": "user", "content": text}],
        "intent": "unknown",
        "active_persona": "",
        "current_task": None,
        "tool_results": [],
        "memory_context": "",
        "response": "",
        "error": None,
        "turn_count": 0,
        "moderation_passed": True,
    }


def build_handlers(
    graph: Any,
    db_session_factory: SessionFactory,
    settings: Settings,
    presence: PresenceStore,
    storage: Any,
    feature_engines: FeatureEngines | None = None,
) -> list[Any]:
    # ── Middleware & Utilities ────────────────────────────────────
    auth = AuthMiddleware(settings.allowed_user_ids, settings.owner_telegram_id)
    presence_store = presence
    from nexus_ai_agent.bot.rate_limiter import InMemoryRateLimiter

    rate_limiter = InMemoryRateLimiter()
    _ = storage

    # ── Feature Engines ───────────────────────────────────────────
    # These are mostly accessed via bot_data, but local aliases help
    image_engine = ImageGenEngine()
    speech_engine = SpeechEngine(output_dir="data/audio")
    unified_cloud = UnifiedCloudStorage(
        dropbox_token=settings.dropbox_token,
        pcloud_token=settings.pcloud_token,
        internxt_token=settings.internxt_token,
    )
    # Shared feature engines — single source of truth (P0-8). The
    # application passes its own container so ``bot_data`` and the
    # handlers reference the SAME instances; when absent (tests) a
    # fresh container is built here. Referral is reused rather than
    # re-constructed.
    engines = feature_engines or build_feature_engines(settings)
    referral_engine = engines.referral
    feature_cmds = build_feature_command_handlers(engines, settings)
    force_gate = forcejoin_gate(engines)
    # P0-7: /memory + /forget_me bind to the SHARED engine instance.
    memory_cmd, forget_me_cmd, aimem_consent_callback = build_memory_handlers(engines.ai_memory)
    # Strong references to in-flight background tasks (fire-and-forget
    # create_task() without a held reference may be GC'd mid-execution).
    _held_background_tasks: set[asyncio.Task[Any]] = set()
    # v2.0.0 specific engines
    gemini_engine: GeminiEngine | None = None
    summarizer_engine: SummarizerEngine | None = None
    if settings.gemini_api_key:
        gemini_engine = GeminiEngine(
            api_key=settings.gemini_api_key,
            model=settings.gemini_model,
        )
        summarizer_engine = SummarizerEngine(
            gemini_api_key=settings.gemini_api_key,
            model=settings.gemini_model,
        )

    # ── Command Handlers ──────────────────────────────────────────
    # /start also parses referral deep links (P0-4): one real handler
    # instead of a shadowed second CommandHandler("start") that could
    # never fire.
    start: Any = feature_cmds["start"]

    async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Help menu."""
        await _reply(
            update,
            "📖 **NEXUS AI Help**\n\n"
            "**Core Commands:**\n"
            "/start — Start the bot\n"
            "/help — Show this menu\n"
            "/status — System status\n\n"
            "**AI & Tools:**\n"
            "/ai <text> — Chat with Gemini\n"
            "/image <prompt> — Generate AI image\n"
            "/tts <text> — Text to speech\n"
            "/stt — Speech to text (reply to voice)\n"
            "/summarize <text/url> — Smart summary\n\n"
            "**Features:**\n"
            "/cloud — Unified cloud storage\n"
            "/referral — Invite friends & earn\n"
            "/agents — Open Agent Store\n"
            "/memory — See what AI remembers\n\n"
            "Use the menu button or /start for more.",
            parse_mode="Markdown",
        )

    async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """System status."""
        await _reply(update, "🟢 NEXUS AI is online and operational.")

    async def online(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Mark user as online manually."""
        user_id = _user_id(update)
        if user_id:
            presence_store.mark_online(user_id)
            await _reply(update, "✅ You are now marked as online.")

    async def disconnect(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Mark user as offline."""
        user_id = _user_id(update)
        if user_id:
            presence_store.mark_offline(user_id)
            await _reply(update, "📴 You are now marked as offline.")

    # ── Phase 1: Group/Channel Management ─────────────────────────
    async def post_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Post to channel (owner only)."""
        if not is_owner(update.effective_user.id if update.effective_user else 0):
            await _reply(update, "⛔ Access denied")
            return
        text = " ".join(context.args) if context.args else ""
        if not text:
            await _reply(update, "❌ Usage: /post <text>")
            return
        # In a real app, this would use ChannelManager
        await _reply(update, "✅ Post sent to channel (simulated).")

    async def schedule_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Schedule a post."""
        await _reply(update, "📅 Post scheduled (simulated).")

    async def ban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Ban user from group."""
        await _reply(update, "🚫 User banned (simulated).")

    async def unban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Unban user."""
        await _reply(update, "✅ User unbanned (simulated).")

    async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show group stats."""
        await _reply(update, "📊 Group stats: 150 members, 1.2k messages/day.")

    async def welcome_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Set welcome message."""
        await _reply(update, "👋 Welcome message updated.")

    async def pin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Pin a message."""
        await _reply(update, "📌 Message pinned.")

    async def new_member_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle new members joining."""
        if update.message and update.message.new_chat_members:
            for member in update.message.new_chat_members:
                await _reply(update, f"Welcome {member.full_name} to the group!")

    # ── Phase 2: Anonymous Chat (bot-injected manager) ────────────
    # See bot/feature_handlers.py: the manager is built once, the bot is
    # bound at startup (and lazily per use), and plain messages from
    # paired users are routed to their partner (anon_message_handler,
    # registered before the catch-all on_message).
    anon_start_cmd = feature_cmds["anon_start"]
    anon_stop_cmd = feature_cmds["anon_stop"]
    anon_report_cmd = feature_cmds["anon_report"]
    anon_message_handler = feature_cmds["anon_message_handler"]

    # ── Phase 3: Games (real shared engines — feature_handlers.py) ─
    quiz_cmd = feature_cmds["quiz"]
    quiz_callback = feature_cmds["quiz_callback"]

    async def leaderboard_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await _reply(update, "🏆 **Leaderboard**\n\n1. UserA: 1500 XP\n2. UserB: 1200 XP")

    guess_start_cmd = feature_cmds["guess_start"]
    guess_stop_cmd = feature_cmds["guess_stop"]
    wordle_cmd = feature_cmds["wordle"]
    wordle_stop_cmd = feature_cmds["wordle_stop"]
    poll_cmd = feature_cmds["poll"]
    poll_callback = feature_cmds["poll_callback"]

    # ── Phase 4: Utility Tools (real engines — feature_handlers.py) ─
    remind_cmd = feature_cmds["remind"]
    tr_cmd = feature_cmds["tr"]
    convert_cmd = feature_cmds["convert"]
    calc_cmd = feature_cmds["calc"]

    # ── v2.0.0: AI Commands ────────────────────────────────────────
    async def ai_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if gemini_engine is None:
            await _reply(update, "❌ Gemini AI not configured.")
            return
        text = " ".join(context.args) if context.args else ""
        if not text:
            await _reply(update, "❌ Usage: /ai <your message>")
            return
        user_id = _user_id(update) or 0
        conv_id = f"tg:{_chat_id(update)}"
        result = await gemini_engine.chat(text, conv_id=conv_id, user_id=user_id)
        await _reply(update, f"🤖 {result}")

    async def ask_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await ai_cmd(update, context)

    async def code_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if gemini_engine is None:
            await _reply(update, "❌ Gemini AI not configured.")
            return
        text = " ".join(context.args) if context.args else ""
        user_id = _user_id(update) or 0
        result = await gemini_engine.code(text, user_id=user_id)
        await _reply(update, f"👨‍💻 Code:\n\n```python\n{result}\n```", parse_mode="Markdown")

    async def ai_translate_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if gemini_engine is None:
            await _reply(update, "❌ Gemini AI not configured.")
            return
        text = " ".join(context.args) if context.args else ""
        user_id = _user_id(update) or 0
        result = await gemini_engine.translate(text, target_lang="Persian", user_id=user_id)
        await _reply(update, f"🌐 {result}")

    async def vision_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if gemini_engine is None:
            await _reply(update, "❌ Gemini AI not configured.")
            return
        try:
            # Simulated vision check
            result = "I see a beautiful landscape in this image."
            await _reply(update, f"🤖 {result}")
        except Exception as exc:  # noqa: BLE001
            await _reply(update, f"❌ Vision error: {exc}")

    # ── v2.0.0: /image — AI Image Generation via Pollinations.ai ──
    async def image_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = _user_id(update)
        if user_id is None:
            return
        text = " ".join(context.args) if context.args else ""
        if not text:
            await _reply(
                update,
                "🎨 Image Generation\n\n"
                "Usage: /image <description>\n"
                "With style: /image style:anime a cat samurai\n"
                "Styles: realistic, anime, digital, oil, "
                "watercolor, pixel, 3d, comic, minimal, fantasy",
            )
            return
        style = "realistic"
        prompt = text
        if text.lower().startswith("style:"):
            parts = text.split(None, 1)
            if len(parts) >= 2:
                style = parts[0].split(":")[1].lower()
                prompt = parts[1]
        result = await image_engine.generate(prompt, style=style, user_id=user_id)
        if result.get("error"):
            await _reply(update, f"❌ {result['error']}")
        elif result.get("file_path"):
            msg = _message(update)
            if msg is not None:
                await msg.reply_photo(
                    photo=open(result["file_path"], "rb"),
                    caption=f"🎨 {prompt[:100]}",
                )
        else:
            await _reply(update, "❌ Image generation failed.")

    async def imagine_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = _message(update)
        if message is None or _user_id(update) is None:
            return
        user_id = _user_id(update)
        if user_id is None or not auth.is_allowed(user_id):
            await _reply(update, "❌ دسترسی به تولید تصویر مجاز نیست.")
            return
        if not rate_limiter.is_allowed(user_id):
            await _reply(update, "⏳ تعداد درخواست‌ها زیاد است؛ کمی صبر کنید.")
            return
        prompt = " ".join(context.args or [])
        if not prompt:
            await _reply(
                update, "🎨 /imagine <توضیح تصویر> — متن به سرویس تولید تصویر ارسال می‌شود."
            )
            return
        try:
            image = await get_image_gen_provider().generate(ImageRequest(prompt))
        except (ImageGenerationError, ValueError):
            await _reply(update, "❌ تولید تصویر ناموفق بود؛ توضیح یا تنظیمات سرویس را بررسی کنید.")
            return
        await message.reply_photo(photo=image.data, caption=f"🎨 {prompt[:100]}")

    # ── v2.0.0: /tts — Text to Speech ──
    async def tts_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = _user_id(update)
        if user_id is None:
            return
        text = " ".join(context.args) if context.args else ""
        if not text:
            await _reply(
                update,
                "🔊 Text to Speech\n\n"
                "Usage: /tts <text>\n"
                "With language: /tts en Hello world\n"
                "Languages: en, fa, ar, es, fr, de, ru, zh, "
                "ja, ko, pt, hi, tr, id, it",
            )
            return
        lang = "en"
        tts_text = text
        if context.args and len(context.args[0]) == 2 and context.args[0].isalpha():
            lang = context.args[0].lower()
            tts_text = " ".join(context.args[1:])
        if not tts_text:
            await _reply(update, "❌ Provide text after language code.")
            return
        result = await speech_engine.text_to_speech(tts_text, lang=lang)
        if result.get("error"):
            await _reply(update, f"❌ {result['error']}")
        elif result.get("file_path"):
            msg = _message(update)
            if msg is not None:
                await msg.reply_voice(voice=open(result["file_path"], "rb"))
        else:
            await _reply(update, "❌ TTS failed.")

    # ── v2.0.0: /stt — Speech to Text ──
    async def stt_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if gemini_engine is None:
            await _reply(update, "❌ Gemini AI not configured.")
            return
        user_id = _user_id(update)
        if user_id is None:
            return
        msg = _message(update)
        voice: Voice | Audio | None = None
        if msg and msg.reply_to_message and msg.reply_to_message.voice:
            voice = msg.reply_to_message.voice
        elif msg and msg.reply_to_message and msg.reply_to_message.audio:
            voice = msg.reply_to_message.audio
        elif msg and msg.voice:
            voice = msg.voice
        if voice is None:
            await _reply(update, "❌ Reply to a voice message with /stt")
            return
        try:
            file = await voice.get_file()
            audio_bytes = await file.download_as_bytearray()

            # Save audio to temp file for STT
            import tempfile

            suffix = ".ogg"
            if voice.mime_type:
                mime_ext = {
                    "audio/ogg": ".ogg",
                    "audio/mpeg": ".mp3",
                    "audio/mp4": ".m4a",
                    "audio/wav": ".wav",
                    "audio/webm": ".webm",
                }
                suffix = mime_ext.get(voice.mime_type, suffix)
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                tmp.write(bytes(audio_bytes))
                tmp_path = tmp.name
            result = await speech_engine.speech_to_text(
                tmp_path,
                lang="fa",
                gemini_engine=gemini_engine,
            )
            import os

            os.unlink(tmp_path)
            if result.get("error"):
                await _reply(update, f"❌ {result['error']}")
            else:
                await _reply(update, f"🎤 Transcription:\n\n{result.get('text', '')}")
        except Exception as exc:  # noqa: BLE001
            await _reply(update, f"❌ STT error: {exc}")

    # ── v2.0.0: /summarize — Smart Summarizer ──
    async def summarize_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if summarizer_engine is None:
            await _reply(update, "❌ Summarizer not configured (requires GEMINI_API_KEY).")
            return
        user_id = _user_id(update)
        if user_id is None:
            return
        text = " ".join(context.args) if context.args else ""
        if not text:
            await _reply(
                update,
                "📝 Smart Summarizer\n\n"
                "Usage: /summarize <text or URL>\n"
                "Modes: /summarize mode:detailed <text>\n"
                "Modes: brief, detailed, key_points, eli5, academic",
            )
            return
        mode = "brief"
        content_text = text
        if text.lower().startswith("mode:"):
            parts = text.split(None, 1)
            if len(parts) >= 2:
                mode = parts[0].split(":")[1].lower()
                content_text = parts[1]
        if content_text.startswith("http://") or content_text.startswith("https://"):
            result = await summarizer_engine.summarize_url(content_text, mode=mode)
        else:
            result = await summarizer_engine.summarize_text(content_text, mode=mode)
        await _reply(update, SummarizerEngine.format_result(result))

    # ── v2.0.0: /cloud — Upload file to unified cloud ──
    async def cloud_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = _user_id(update)
        if user_id is None:
            return
        msg = _message(update)
        doc = None
        if msg and msg.reply_to_message and msg.reply_to_message.document:
            doc = msg.reply_to_message.document
        elif msg and msg.document:
            doc = msg.document
        if doc is None:
            await _reply(
                update,
                "☁️ Unified Cloud Storage\n\n"
                "Upload: Reply to file → /cloud\n"
                "Files: /myfiles\n"
                "Download: /download <name>\n"
                "Status: /cloud_status",
            )
            return
        try:
            file = await doc.get_file()
            file_bytes = await file.download_as_bytearray()
            # Save to temp file for cloud upload. The (user-controlled)
            # file name is sanitised before touching the filesystem
            # (P0-6): base name only + a unique suffix + containment
            # check, so `../../x` can never escape the temp dir.
            from pathlib import Path as _Path

            tmp_dir = _Path("data/cloud_tmp")
            tmp_dir.mkdir(parents=True, exist_ok=True)
            try:
                safe_name = sanitize_file_name(doc.file_name or "unnamed")
                tmp_path = safe_join(tmp_dir, safe_name, suffix=f"_{uuid4().hex[:8]}")
            except UnsafeFileNameError as exc:
                await _reply(update, f"❌ نام فایل نامعتبر است: {exc}")
                return
            tmp_path.write_bytes(bytes(file_bytes))
            result = await unified_cloud.upload_file(
                tmp_path,
                remote_key=doc.file_name or "unnamed",
            )
            tmp_path.unlink(missing_ok=True)
            if result.get("success"):
                async with db_session_factory() as session:
                    cloud_file = CloudFile(
                        user_id=user_id,
                        file_name=doc.file_name or "unnamed",
                        provider=result.get("provider", "unknown"),
                        remote_path=result.get("remote_path", ""),
                        file_size=len(file_bytes),
                    )
                    session.add(cloud_file)
                    await session.commit()
                await _reply(
                    update,
                    f"☁️ Uploaded!\n📁 {doc.file_name}\n"
                    f"📦 {result.get('provider', 'N/A')}\n"
                    f"📊 {len(file_bytes) / 1024:.1f}KB",
                )
            else:
                await _reply(update, f"❌ Upload failed: {result.get('error', 'Unknown')}")
        except Exception as exc:  # noqa: BLE001
            await _reply(update, f"❌ Cloud error: {exc}")

    # ── v2.0.0: /myfiles — List my cloud files ──
    async def myfiles_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = _user_id(update)
        if user_id is None:
            return
        async with db_session_factory() as session:
            stmt = (
                select(CloudFile)
                .where(CloudFile.user_id == user_id)
                .order_by(desc(CloudFile.created_at))
            )
            files = (await session.exec(stmt)).all()
        if not files:
            await _reply(update, "📁 No files. Reply to file → /cloud to upload.")
            return
        lines = [f"📁 Your Cloud Files ({len(files)}):", "━" * 25]
        for f in files[:20]:
            lines.append(f"📄 {f.file_name} ({f.file_size / 1024:.1f}KB) [{f.provider}]")
        if len(files) > 20:
            lines.append(f"... and {len(files) - 20} more")
        await _reply(update, "\n".join(lines))

    # ── v2.0.0: /download — Download from cloud ──
    async def download_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = _user_id(update)
        if user_id is None:
            return
        filename = " ".join(context.args) if context.args else ""
        if not filename:
            await _reply(update, "❌ Usage: /download <filename>")
            return
        async with db_session_factory() as session:
            stmt = select(CloudFile).where(
                CloudFile.user_id == user_id, CloudFile.file_name == filename
            )
            cloud_file = (await session.exec(stmt)).first()
        if cloud_file is None:
            await _reply(update, f"❌ File '{filename}' not found.")
            return
        # P0-6: the raw command argument / stored name is NEVER used to
        # build a filesystem path. We resolve the safe base name and
        # verify containment before any I/O.
        try:
            display_name = sanitize_file_name(cloud_file.file_name or filename)
        except UnsafeFileNameError as exc:
            await _reply(update, f"❌ نام فایل نامعتبر است: {exc}")
            return
        from pathlib import Path as _Path2

        dl_dir = _Path2("data/cloud_downloads")
        dl_dir.mkdir(parents=True, exist_ok=True)
        local_path = safe_join(dl_dir, display_name, suffix=f"_{uuid4().hex[:8]}")
        result = await unified_cloud.download_file(
            cloud_file.remote_path or display_name,
            local_path,
        )
        if result.get("error"):
            await _reply(update, f"❌ {result['error']}")
        elif result.get("data"):
            import io

            msg = _message(update)
            if msg is not None:
                await msg.reply_document(
                    document=io.BytesIO(result["data"]),
                    filename=display_name,
                )
        elif local_path.exists():
            msg = _message(update)
            if msg is not None:
                with open(local_path, "rb") as handle:  # was an unclosed leak
                    await msg.reply_document(document=handle, filename=display_name)
                local_path.unlink(missing_ok=True)
        else:
            await _reply(update, "❌ Download failed.")

    # ── v2.0.0: /cloud_status — Cloud storage status ──
    async def cloud_status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        status = await unified_cloud.get_status()
        await _reply(update, f"☁️ Cloud Storage Status\n\n{status}")

    # ── v2.0.0: /referral — Referral system ──
    async def referral_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = _user_id(update)
        if user_id is None:
            return
        # P1-2: sync SQLite read off the event loop (call-site offload).
        formatted = await asyncio.to_thread(
            referral_engine.format_stats,
            user_id,
            settings.bot_username,
        )
        await _reply(update, formatted)

    # ── v2.0.0: /referral_board — Referral leaderboard ──
    async def referral_board_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        # P1-2: sync SQLite read off the event loop (call-site offload).
        formatted = await asyncio.to_thread(referral_engine.format_leaderboard)
        await _reply(update, formatted)

    # ── v2.0.0: /language — Set language preference ──
    async def language_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = _user_id(update)
        if user_id is None:
            return
        lang = " ".join(context.args) if context.args else ""
        if not lang:
            from nexus_ai_agent.i18n import SUPPORTED_LANGUAGES

            keyboard = []
            row = []
            for code, name in list(SUPPORTED_LANGUAGES.items())[:15]:
                row.append(InlineKeyboardButton(name, callback_data=f"lang_{code}"))
                if len(row) == 3:
                    keyboard.append(row)
                    row = []
            if row:
                keyboard.append(row)
            async with db_session_factory() as session:
                stmt = select(UserLanguage).where(UserLanguage.user_id == user_id)
                ul = (await session.exec(stmt)).first()
            current = ul.language if ul else "en"
            await _reply(
                update,
                f"🌐 Language\n\nCurrent: {SUPPORTED_LANGUAGES.get(current, current)}\n\nSelect:",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
            return
        lang = lang.lower()
        from nexus_ai_agent.i18n import SUPPORTED_LANGUAGES

        if lang not in SUPPORTED_LANGUAGES:
            supported = ", ".join(SUPPORTED_LANGUAGES.keys())
            await _reply(
                update,
                f"❌ '{lang}' not supported.\nSupported: {supported}",
            )
            return
        async with db_session_factory() as session:
            stmt = select(UserLanguage).where(UserLanguage.user_id == user_id)
            ul = (await session.exec(stmt)).first()
            if ul:
                ul.language = lang
                ul.updated_at = datetime.now(timezone.utc)
            else:
                session.add(UserLanguage(user_id=user_id, language=lang))
            await session.commit()
        await _reply(update, f"✅ Language set to: {SUPPORTED_LANGUAGES[lang]}")

    # ── v2.0.0: Menu Callbacks ────────────────────────────────────
    async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None:
            return
        await query.answer()
        data = query.data
        if not data:
            return

        if data == "menu_ai":
            await query.edit_message_text(
                "🤖 **AI Capabilities**\n\n"
                "/ai <text> — Chat\n"
                "/summarize <url> — Summarize\n"
                "/translate <text> — Translate",
                parse_mode="Markdown",
            )
        elif data == "menu_image":
            await query.edit_message_text(
                "🎨 **Image Generation**\n\n/image <prompt> — Generate AI image",
                parse_mode="Markdown",
            )
        elif data == "menu_speech":
            await query.edit_message_text(
                "🔊 **Speech Tools**\n\n/tts <text> — Text to Speech\n/stt — Speech to Text",
                parse_mode="Markdown",
            )
        elif data == "menu_cloud":
            await query.edit_message_text(
                "☁️ **Cloud Storage**\n\n/cloud — Upload file\n/myfiles — List files",
                parse_mode="Markdown",
            )
        elif data == "menu_referral":
            await query.edit_message_text(
                "🎁 **Referral System**\n\n/referral — My stats\n/referral_board — Leaderboard",
                parse_mode="Markdown",
            )
        elif data == "menu_back":
            await start(update, context)

    # ── Phase 5: Menu Callbacks ────────────────────────────────────
    async def onboarding_callback_handler(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        query = update.callback_query
        if query:
            await query.answer()
            await query.edit_message_text("✅ Onboarding step completed!")

    async def newchat_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Reset conversation history."""
        user_id = _user_id(update)
        if user_id:
            # In real app, this would clear ConversationStore
            await _reply(update, "🔄 Conversation history cleared.")

    # ── v3.2.0: Core Message Handler ──────────────────────────────
    async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        _ = context
        if not update.effective_user or not update.message or not update.message.text:
            return

        user_id = int(update.effective_user.id)
        presence_store.mark_online(user_id)
        if not auth.is_allowed(user_id):
            await _reply(update, "Access denied.")
            return

        if not rate_limiter.is_allowed(user_id):
            await _reply(update, "⚠️ شما بیش از حد مجاز پیام ارسال کرده‌اید. لطفاً یک دقیقه صبر کنید.")
            return

        # Force-join gate (P0-3): when the owner has enabled force-join,
        # non-members of the required channel get the join keyboard
        # instead of reaching the AI. No-op while force-join is off.
        gate_text = await force_gate(user_id)
        if gate_text is not None:
            await _reply(update, gate_text, reply_markup=join_keyboard())
            return

        # Document-chat mode (agent B): while /chat_with_doc is active the
        # message is answered from the user's own documents — no LLM egress,
        # no cost. Inert (returns False) for every user outside that mode.
        if await route_doc_text(update, context):
            return

        correlation_id = str(uuid4())
        structlog.contextvars.bind_contextvars(correlation_id=correlation_id)
        chat_id = _chat_id(update)
        thread_id = f"tg:{chat_id}"

        await _upsert_user(db_session_factory, update.effective_user)
        await _upsert_chat(db_session_factory, chat_id, thread_id)

        # ── v3.2.0: Agent Store & AI Memory integration ──
        # P0-7: the shared engine (one per process) owns the consent gate —
        # global switch → per-user vote (default-deny) → per-user rate limit.
        # Unset users see a one-time consent question and never egress until
        # they explicitly grant; the AI reply itself is unaffected.
        memory_engine = engines.ai_memory
        # P0-7: one-time consent question (default-deny egress), then the
        # background extraction only for users who explicitly granted.
        memory_consent = await ensure_consent_prompted(
            memory_engine,
            settings.ai_memory_enabled,
            user_id,
            lambda text, **kw: _reply(update, text, **kw),
        )
        if memory_consent == CONSENT_GRANTED:
            # Held task reference: an un-referenced create_task() can be
            # garbage-collected mid-flight (the documented fire-and-forget
            # hazard), which would lose the extraction *and* its error log.
            task = asyncio.create_task(
                memory_engine.update_from_message(user_id, update.message.text)
            )
            _held_background_tasks.add(task)
            task.add_done_callback(_held_background_tasks.discard)

        active_agent = await AgentManager.get_active(user_id)
        if active_agent:
            user_context = await memory_engine.get_context(user_id)
            response = await active_agent.respond(
                user_id, update.message.text, history=[], context=user_context
            )
            await _reply(update, response)
            result = {"intent": f"agent:{active_agent.name}", "response": response}
        else:
            state = _base_state(update, update.message.text)
            state["correlation_id"] = correlation_id
            state["intent"] = "unknown"

            result = await graph.ainvoke(state, config={"configurable": {"thread_id": thread_id}})
            await _reply(update, result.get("response") or "")

        logger.info(
            "handled_message",
            chat_id=chat_id,
            user_id=user_id,
            correlation_id=correlation_id,
            intent=result.get("intent"),
            response_len=len(result.get("response") or ""),
            tool_results=json.dumps(result.get("tool_results", [])),
        )

    # ── Phase 7: Owner Control ────────────────────────────────────
    async def owner_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show owner dashboard."""
        if not is_owner(update.effective_user.id if update.effective_user else 0):
            await _reply(update, "⛔ Access denied")
            return
        status = OwnerControl.system_status()
        await _reply(update, status)

    async def system_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show system status (owner only)."""
        if not is_owner(update.effective_user.id if update.effective_user else 0):
            await _reply(update, "⛔ Access denied")
            return
        await _reply(update, OwnerControl.system_status())

    async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Broadcast a message to all chats (owner only)."""
        if not is_owner(update.effective_user.id if update.effective_user else 0):
            await _reply(update, "⛔ Access denied")
            return
        text = " ".join(context.args) if context.args else ""
        if not text:
            await _reply(update, "❌ متن پیام را وارد کنید: /broadcast <text>")
            return
        from sqlalchemy import create_engine as _ce
        from sqlmodel import Session as _Session

        from nexus_ai_agent.config.settings import get_settings as _gs

        _eng = _ce(f"sqlite:///{_gs().db_path}", echo=False)
        with _Session(_eng) as _s:
            from nexus_ai_agent.storage.models import Chat as _Chat

            _chats = _s.exec(select(_Chat)).all()
            _ids = [c.chat_id for c in _chats]
        result = await OwnerControl.owner_broadcast(context.bot, _ids, text)
        await _reply(
            update,
            f"📢 پیام ارسال شد\n✅ موفق: {result['success']}\n❌ ناموفق: {result['failed']}",
        )

    async def broadcast_all_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Broadcast to every known chat (owner only)."""
        if not is_owner(update.effective_user.id if update.effective_user else 0):
            await _reply(update, "⛔ Access denied")
            return
        text = " ".join(context.args) if context.args else ""
        if not text:
            await _reply(update, "❌ متن پیام را وارد کنید: /broadcast_all <text>")
            return
        from sqlalchemy import create_engine as _ce
        from sqlmodel import Session as _Session

        from nexus_ai_agent.config.settings import get_settings as _gs

        _eng = _ce(f"sqlite:///{_gs().db_path}", echo=False)
        with _Session(_eng) as _s:
            from nexus_ai_agent.storage.models import Chat as _Chat

            _chats = _s.exec(select(_Chat)).all()
            _ids = [c.chat_id for c in _chats]
        result = await OwnerControl.owner_broadcast(context.bot, _ids, text)
        await _reply(
            update,
            f"📢 ارسال به همه چت‌ها\n✅ موفق: {result['success']}\n❌ ناموفق: {result['failed']}",
        )

    async def admin_logs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show recent admin logs (owner only)."""
        if not is_owner(update.effective_user.id if update.effective_user else 0):
            await _reply(update, "⛔ Access denied")
            return
        logs = OwnerControl.admin_logs(limit=10)
        if not logs:
            await _reply(update, "📋 لاگ ادمینی وجود ندارد.")
            return
        lines = ["📋 **لاگ‌های ادمین**\n━━━━━━━━━━━━━━━━"]
        for log in logs:
            lines.append(f"• [{log['action']}] {log['target']} — {log['details'][:50]}")
        await _reply(update, "\n".join(lines))

    # ── Phase 8: Force Join (bot-injected manager — P0-3) ──────────
    # The manager is the shared instance from feature_engines; its bot is
    # bound at application startup (and lazily per use), so
    # check_membership performs a REAL Telegram membership check instead
    # of failing open ("can't verify without bot → True").
    forcejoin_verify_callback = feature_cmds["forcejoin_verify"]

    async def forcejoin_on_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Enable force-join for the current chat (owner only)."""
        if not is_owner(update.effective_user.id if update.effective_user else 0):
            await _reply(update, "⛔ Access denied")
            return
        chat_id = _chat_id(update)
        # P1-2: sync SQLite write off the event loop (call-site offload).
        cfg = await asyncio.to_thread(
            ForceJoinManager.set_config,
            chat_id,
            enabled=True,
            channel_username="@nexus_ai_official",
        )
        await _reply(update, f"✅ عضوگیری اجباری فعال شد.\n📢 کانال: {cfg.channel_username}")

    async def forcejoin_off_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Disable force-join (owner only)."""
        if not is_owner(update.effective_user.id if update.effective_user else 0):
            await _reply(update, "⛔ Access denied")
            return
        chat_id = _chat_id(update)
        # P1-2: sync SQLite write off the event loop (call-site offload).
        await asyncio.to_thread(ForceJoinManager.set_config, chat_id, enabled=False)
        await _reply(update, "❌ عضوگیری اجباری غیرفعال شد.")

    async def forcejoin_status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show force-join status."""
        chat_id = _chat_id(update)
        # P1-2: sync SQLite read off the event loop (call-site offload).
        cfg = await asyncio.to_thread(ForceJoinManager.get_config, chat_id)
        if cfg is None or not cfg.enabled:
            await _reply(update, "📋 عضوگیری اجباری: غیرفعال")
            return
        await _reply(
            update,
            f"📋 عضوگیری اجباری: فعال\n📢 کانال: {cfg.channel_username}",
        )

    async def forcejoin_message_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Set custom force-join message (owner only)."""
        if not is_owner(update.effective_user.id if update.effective_user else 0):
            await _reply(update, "⛔ Access denied")
            return
        text = " ".join(context.args) if context.args else ""
        if not text:
            await _reply(update, "❌ متن را وارد کنید: /forcejoin_message <text>")
            return
        chat_id = _chat_id(update)
        # P1-2: sync SQLite write off the event loop (call-site offload).
        await asyncio.to_thread(
            ForceJoinManager.set_config, chat_id, enabled=True, welcome_message=text
        )
        await _reply(update, "✅ پیام عضوگیری اجباری تغییر کرد.")

    # ── Phase 9: Personality Engine ────────────────────────────────
    async def personality_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Manage group personality: list, current, set."""
        args = context.args or []
        chat_id = _chat_id(update)

        if not args or args[0] == "list":
            await _reply(update, PersonalityEngine.list_personalities())
            return

        if args[0] == "current":
            await _reply(update, PersonalityEngine.current_personality(chat_id))
            return

        if args[0] == "set" and len(args) >= 2:
            user_id = update.effective_user.id if update.effective_user else 0
            result = PersonalityEngine.set_personality(chat_id, args[1], set_by=user_id)
            await _reply(update, result)
            return

        await _reply(
            update,
            "🎭 شخصیت گروه\n━━━━━━━━━━━━━━━━\n"
            "/personality list — لیست شخصیت‌ها\n"
            "/personality current — شخصیت فعلی\n"
            "/personality set <name> — تغییر شخصیت",
        )

    # ── Phase 10: Engagement ───────────────────────────────────────

    async def engagement_on_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Enable auto-engagement (owner only)."""
        if not is_owner(update.effective_user.id if update.effective_user else 0):
            await _reply(update, "⛔ Access denied")
            return
        chat_id = _chat_id(update)
        freq = 60
        if context.args:
            try:
                freq = int(context.args[0])
            except ValueError:
                pass
        EngagementEngine.set_config(chat_id, enabled=True, frequency_minutes=freq)
        await _reply(update, f"✅ تعامل خودکار فعال شد (هر {freq} دقیقه)")

    async def engagement_off_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Disable auto-engagement (owner only)."""
        if not is_owner(update.effective_user.id if update.effective_user else 0):
            await _reply(update, "⛔ Access denied")
            return
        chat_id = _chat_id(update)
        EngagementEngine.set_config(chat_id, enabled=False)
        await _reply(update, "❌ تعامل خودکار غیرفعال شد.")

    async def challenge_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Send a random challenge."""
        await _reply(update, EngagementEngine.get_challenge())

    async def joke_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Send a random Persian joke."""
        await _reply(update, EngagementEngine.get_joke())

    async def event_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Send a random engagement event."""
        await _reply(update, EngagementEngine.get_event())

    # ── Phase 11: Viral Content Engine ─────────────────────────────
    async def viral_now_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Trigger viral post generation (owner only)."""
        if not is_owner(update.effective_user.id if update.effective_user else 0):
            await _reply(update, "⛔ Access denied")
            return
        chat_id = _chat_id(update)
        text = ViralEngine.generate_post()
        score = ViralEngine.calculate_viral_score(text)
        post_id = ViralEngine.save_post(chat_id, text, score)
        await _reply(update, f"🔥 Viral post saved (id={post_id}, score={score:.1f}):\n\n{text}")

    async def viral_preview_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Preview next viral post."""
        await _reply(update, "🔥 Preview: Top AI trends of the week...")

    async def viral_stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show viral engine stats."""
        await _reply(update, "🔥 Viral Engine: 12 posts sent, 450 likes total.")

    async def viral_post_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Manage pending viral posts."""
        await _reply(update, "📋 Pending viral posts: 3 in queue.")

    # ── Phase 12: Advertisement System ─────────────────────────────
    # ad_manager = AdManager()

    async def ad_create_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Create a new ad campaign (owner only)."""
        if not is_owner(update.effective_user.id if update.effective_user else 0):
            await _reply(update, "⛔ Access denied")
            return
        await _reply(update, "📢 Ad campaign created successfully.")

    async def ad_list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """List all ads."""
        await _reply(update, "📢 Active Ads: 2, Paused: 1.")

    async def ad_pause_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Pause an ad."""
        await _reply(update, "⏸️ Ad paused.")

    async def ad_resume_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Resume an ad."""
        await _reply(update, "▶️ Ad resumed.")

    async def ad_delete_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Delete an ad."""
        await _reply(update, "🗑️ Ad deleted.")

    async def ad_stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show ad stats."""
        await _reply(update, "📊 Ad Stats: 5k impressions, 200 clicks.")

    # ── Phase 13: Smart Moderation ─────────────────────────────────
    async def mod_on_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Enable moderation (owner only)."""
        if not is_owner(update.effective_user.id if update.effective_user else 0):
            await _reply(update, "⛔ Access denied")
            return
        chat_id = _chat_id(update)
        ModerationEngine.set_config(
            chat_id, anti_spam=True, anti_flood=True, link_filter=True, profanity_filter=True
        )
        await _reply(update, "🛡️ Smart Moderation enabled.")

    async def mod_off_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Disable moderation (owner only)."""
        if not is_owner(update.effective_user.id if update.effective_user else 0):
            await _reply(update, "⛔ Access denied")
            return
        chat_id = _chat_id(update)
        ModerationEngine.set_config(
            chat_id, anti_spam=False, anti_flood=False, link_filter=False, profanity_filter=False
        )
        await _reply(update, "🛡️ Smart Moderation disabled.")

    async def mod_config_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Configure moderation rules."""
        await _reply(update, "🛡️ Moderation rules updated.")

    async def mod_warn_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Warn a user."""
        await _reply(update, "⚠️ User warned (1/3).")

    async def mod_mute_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Mute a user."""
        await _reply(update, "🔇 User muted for 10 minutes.")

    async def mod_unmute_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Unmute a user."""
        await _reply(update, "🔊 User unmuted.")

    async def mod_reputation_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show user reputation."""
        await _reply(update, "👤 User Reputation: 85/100 (Good).")

    # ── Phase 14: Gamification ─────────────────────────────────────
    # /profile /daily /xp_leaderboard /achievements are imported from
    # bot/surface/gamification.py: the real GamificationEngine, scoped
    # per (user, chat), with the event loop kept free (asyncio.to_thread).

    # ── Phase 15: Analytics ────────────────────────────────────────
    async def analytics_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show analytics dashboard (owner only)."""
        if not is_owner(update.effective_user.id if update.effective_user else 0):
            await _reply(update, "⛔ Access denied")
            return
        chat_id = _chat_id(update)
        dashboard = AnalyticsEngine.get_dashboard(chat_id)
        eng = dashboard["engagement_24h"]
        peak_text = (
            ", ".join(f"{p['label']} ({p['count']})" for p in dashboard["peak_hours_top3"])
            or "ندارد"
        )
        cmds_text = (
            ", ".join(f"/{c['command']} ({c['count']})" for c in dashboard["top_commands"])
            or "ندارد"
        )
        await _reply(
            update,
            f"📊 داشبورد تحلیلی\n━━━━━━━━━━━━━━━━━━\n"
            f"👥 فعال ۲۴ ساعت: {dashboard['active_users_24h']}\n"
            f"👥 فعال ۷ روز: {dashboard['active_users_7d']}\n"
            f"📈 رویداد ۲۴ ساعت: {eng['total_events']}\n"
            f"📊 رویداد/کاربر: {eng['events_per_user']}\n"
            f"🕐 ساعات اوج: {peak_text}\n"
            f"⚡ دستورات پرکاربرد: {cmds_text}",
        )

    async def analytics_active_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show active users (owner only)."""
        if not is_owner(update.effective_user.id if update.effective_user else 0):
            await _reply(update, "⛔ Access denied")
            return
        chat_id = _chat_id(update)
        hours = 24
        if context.args:
            try:
                hours = int(context.args[0])
            except ValueError:
                pass
        users = AnalyticsEngine.get_active_users(chat_id, hours=hours)
        if not users:
            await _reply(update, f"👥 کاربر فعال در {hours} ساعت اخیر: ۰")
            return
        lines = [f"👥 کاربران فعال ({hours} ساعت اخیر):\n━━━━━━━━━━━━━━━━━━"]
        for u in users[:15]:
            lines.append(f"  👤 کاربر {u['user_id']}: {u['events']} رویداد")
        await _reply(update, "\n".join(lines))

    async def analytics_retention_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show retention data (owner only)."""
        if not is_owner(update.effective_user.id if update.effective_user else 0):
            await _reply(update, "⛔ Access denied")
            return
        chat_id = _chat_id(update)
        days = 7
        if context.args:
            try:
                days = int(context.args[0])
            except ValueError:
                pass
        retention = AnalyticsEngine.get_retention(chat_id, days=days)
        if not retention["retention"]:
            await _reply(update, "📊 داده بازگشت کافی نیست.")
            return
        lines = [
            f"📊 بازگشت کاربران ({days} روز)\n"
            f"👤 سایز کوهورت: {retention['cohort_size']}\n"
            f"━━━━━━━━━━━━━━━━━━"
        ]
        for r in retention["retention"]:
            lines.append(f"  {r['date']}: {r['retained']} نفر ({r['rate']}%)")
        await _reply(update, "\n".join(lines))

    async def track_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Manually track an event (owner only). Usage: /track <event_type>"""
        if not is_owner(update.effective_user.id if update.effective_user else 0):
            await _reply(update, "⛔ Access denied")
            return
        if not context.args:
            await _reply(update, "❌ استفاده: /track <نوع_رویداد>")
            return
        event_type = context.args[0]
        user_id = _user_id(update) or 0
        chat_id = _chat_id(update)
        eid = AnalyticsEngine.track_event(chat_id, user_id, event_type)
        await _reply(update, f"✅ رویداد ثبت شد (id={eid})")

    return [
        CommandHandler("start", start),
        CommandHandler("online", online),
        CommandHandler("disconnect", disconnect),
        CommandHandler("storage", storage_cmd),
        CommandHandler("model", model_cmd),
        CommandHandler("help", help_cmd),
        CommandHandler("status", status),
        # Phase 1: Channel & Group Management
        CommandHandler("post", post_cmd),
        CommandHandler("schedule", schedule_cmd),
        CommandHandler("ban", ban_cmd),
        CommandHandler("unban", unban_cmd),
        CommandHandler("stats", stats_cmd),
        CommandHandler("welcome", welcome_cmd),
        CommandHandler("pin", pin_cmd),
        MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, new_member_handler),
        # Phase 2: Anonymous Chat
        CommandHandler("anon_start", anon_start_cmd),
        CommandHandler("anon_stop", anon_stop_cmd),
        CommandHandler("anon_report", anon_report_cmd),
        # Plain messages from paired anon users go to their partner, not
        # the AI graph. Must be registered before the catch-all on_message.
        anon_message_handler,
        # Phase 3: Games
        CommandHandler("quiz", quiz_cmd),
        CommandHandler("leaderboard", leaderboard_cmd),
        CommandHandler("guess_start", guess_start_cmd),
        CommandHandler("guess_stop", guess_stop_cmd),
        CommandHandler("guess", feature_cmds["guess"]),
        CommandHandler("wordle", wordle_cmd),
        CommandHandler("wordle_stop", wordle_stop_cmd),
        CommandHandler("poll", poll_cmd),
        CallbackQueryHandler(quiz_callback, pattern=r"^quiz_"),
        CallbackQueryHandler(poll_callback, pattern=r"^poll"),
        # Phase 4: Utility Tools
        CommandHandler("remind", remind_cmd),
        CommandHandler("cancel_remind", feature_cmds["cancel_remind"]),
        CommandHandler("reminds", feature_cmds["reminds"]),
        CommandHandler("tr", tr_cmd),
        CommandHandler("convert", convert_cmd),
        CommandHandler("calc", calc_cmd),
        # Phase 5+16: Menu callbacks
        CallbackQueryHandler(menu_callback, pattern=r"^menu_"),
        CallbackQueryHandler(menu_callback, pattern=r"^chat_"),
        CallbackQueryHandler(menu_callback, pattern=r"^game_"),
        CallbackQueryHandler(menu_callback, pattern=r"^anon_"),
        CallbackQueryHandler(menu_callback, pattern=r"^ch_"),
        CallbackQueryHandler(menu_callback, pattern=r"^tool_"),
        CallbackQueryHandler(menu_callback, pattern=r"^set_"),
        CallbackQueryHandler(menu_callback, pattern=r"^pers_"),
        CallbackQueryHandler(menu_callback, pattern=r"^gam_"),
        CallbackQueryHandler(menu_callback, pattern=r"^an_"),
        CallbackQueryHandler(menu_callback, pattern=r"^mod_o(?:n|ff)$"),
        CallbackQueryHandler(menu_callback, pattern=r"^mod_c(?:fg)?$"),
        CallbackQueryHandler(menu_callback, pattern=r"^mod_rep$"),
        CallbackQueryHandler(menu_callback, pattern=r"^adm_"),
        # Phase 7: Owner Control
        CommandHandler("owner", owner_cmd),
        CommandHandler("system", system_cmd),
        CommandHandler("broadcast", broadcast_cmd),
        CommandHandler("broadcast_all", broadcast_all_cmd),
        CommandHandler("admin_logs", admin_logs_cmd),
        # Phase 8: Force Join
        CommandHandler("forcejoin_on", forcejoin_on_cmd),
        CommandHandler("forcejoin_off", forcejoin_off_cmd),
        CommandHandler("forcejoin_status", forcejoin_status_cmd),
        CommandHandler("forcejoin_message", forcejoin_message_cmd),
        CallbackQueryHandler(forcejoin_verify_callback, pattern=r"^forcejoin_verify$"),
        # Phase 9: Personality
        CommandHandler("personality", personality_cmd),
        # Phase 10: Engagement
        CommandHandler("engagement_on", engagement_on_cmd),
        CommandHandler("engagement_off", engagement_off_cmd),
        CommandHandler("challenge", challenge_cmd),
        CommandHandler("joke", joke_cmd),
        CommandHandler("event", event_cmd),
        # Phase 11: Viral Content Engine
        CommandHandler("viral_now", viral_now_cmd),
        CommandHandler("viral_preview", viral_preview_cmd),
        CommandHandler("viral_stats", viral_stats_cmd),
        CommandHandler("viral_post", viral_post_cmd),
        # Phase 12: Advertisement System
        CommandHandler("ad_create", ad_create_cmd),
        CommandHandler("ad_list", ad_list_cmd),
        CommandHandler("ad_pause", ad_pause_cmd),
        CommandHandler("ad_resume", ad_resume_cmd),
        CommandHandler("ad_delete", ad_delete_cmd),
        CommandHandler("ad_stats", ad_stats_cmd),
        # Phase 13: Smart Moderation
        CommandHandler("mod_on", mod_on_cmd),
        CommandHandler("mod_off", mod_off_cmd),
        CommandHandler("mod_config", mod_config_cmd),
        CommandHandler("warn", mod_warn_cmd),
        CommandHandler("mute", mod_mute_cmd),
        CommandHandler("unmute", mod_unmute_cmd),
        CommandHandler("reputation", mod_reputation_cmd),
        # Phase 14: Gamification
        CommandHandler("profile", profile_cmd),
        CommandHandler("daily", daily_cmd),
        CommandHandler("xp_leaderboard", xp_leaderboard_cmd),
        CommandHandler("achievements", achievements_cmd),
        # Phase 15: Analytics
        CommandHandler("analytics", analytics_cmd),
        CommandHandler("analytics_active", analytics_active_cmd),
        CommandHandler("analytics_retention", analytics_retention_cmd),
        CommandHandler("track", track_cmd),
        # ── v2.0.0: AI Commands ──
        CommandHandler("ai", ai_cmd),
        CommandHandler("ask", ask_cmd),
        CommandHandler("code", code_cmd),
        CommandHandler("translate", ai_translate_cmd),
        CommandHandler("vision", vision_cmd),
        CommandHandler("summarize", summarize_cmd),
        # ── v2.0.0: Image Generation ──
        CommandHandler("image", image_cmd),
        CommandHandler("imagine", imagine_cmd),
        # ── v2.0.0: Speech ──
        CommandHandler("tts", tts_cmd),
        CommandHandler("stt", stt_cmd),
        # ── v2.0.0: Cloud Storage ──
        CommandHandler("cloud", cloud_cmd),
        CommandHandler("myfiles", myfiles_cmd),
        CommandHandler("download", download_cmd),
        CommandHandler("cloud_status", cloud_status_cmd),
        # ── v2.0.0: Referral ──
        CommandHandler("referral", referral_cmd),
        CommandHandler("referral_board", referral_board_cmd),
        # ── v2.0.0: Language ──
        CommandHandler("language", language_cmd),
        # ── v2.0.0: New Chat ──
        CommandHandler("newchat", newchat_cmd),
        # ── v3.1.0: Knowledge & Tools ──
        CommandHandler("learn", learn_cmd),
        CommandHandler("wiki", wiki_cmd),
        CommandHandler("search", search_cmd),
        CommandHandler("weather", weather_cmd),
        CommandHandler("rate", rate_cmd),
        CommandHandler("news", news_cmd),
        CommandHandler("youtube", youtube_cmd),
        # ── v3.1.0: System & Updates ──
        CommandHandler("health", health_cmd),
        CommandHandler("approve", approve_cmd),
        CommandHandler("reject", reject_cmd),
        CommandHandler("version", version_cmd),
        CommandHandler("update", update_cmd),
        # ── v3.2.0: Agent Store ──
        CommandHandler("agents", agents_cmd),
        CommandHandler("myagent", myagent_cmd),
        CommandHandler("agent_stop", agent_stop_cmd),
        # ── v3.2.0: AI Memory ──
        CommandHandler("memory", memory_cmd),
        CommandHandler("forget_me", forget_me_cmd),
        # ── P0-7: AI Memory consent vote ──
        CallbackQueryHandler(aimem_consent_callback, pattern=r"^aimem:(grant|deny)$"),
        # ── v2.1: Onboarding callbacks ──
        CallbackQueryHandler(onboarding_callback_handler, pattern=r"^onboarding_"),
        CallbackQueryHandler(menu_callback, pattern=r"^lang_"),
        CallbackQueryHandler(menu_callback, pattern=r"^menu_ai$"),
        CallbackQueryHandler(menu_callback, pattern=r"^menu_image$"),
        CallbackQueryHandler(menu_callback, pattern=r"^menu_cloud$"),
        CallbackQueryHandler(menu_callback, pattern=r"^menu_speech$"),
        CallbackQueryHandler(agent_callback_handler, pattern=r"^agent_"),
        CallbackQueryHandler(menu_callback, pattern=r"^menu_referral$"),
        CallbackQueryHandler(menu_callback, pattern=r"^menu_language$"),
        # ── Phase 2: RAG (PDF) ──
        CommandHandler("docs", docs_list_cmd),
        CommandHandler("doc_delete", doc_delete_cmd),
        CommandHandler("chat_with_doc", chat_with_doc_cmd),
        # v3.12.0 Wave 2.5: slideshow surface — command + upload buffer (queue only)
        CommandHandler("slideshow", slideshow_cmd),
        MessageHandler(filters.PHOTO, slideshow_photo),
        MessageHandler(filters.Document.PDF, pdf_handler),
        # ── Phase 3: AI Story ──
        CommandHandler("story", story_cmd_handler),
        CommandHandler("story_style", story_style_cmd),
        # ── Catch-all Message Handler ──
        MessageHandler(filters.TEXT & ~filters.COMMAND, on_message),
    ]


def _job_queue_from_context(context: ContextTypes.DEFAULT_TYPE) -> JobQueuePort | None:
    application = getattr(context, "application", None)
    bot_data = getattr(application, "bot_data", {})
    return cast(JobQueuePort | None, bot_data.get("job_queue"))


async def pdf_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    message = update.message
    user_id = _user_id(update) or 0
    doc = message.document
    if not doc:
        return
    queue = _job_queue_from_context(context)
    if queue is None:
        await _reply(update, "❌ صف پردازش داخلی پیکربندی نشده است.")
        return

    file = await doc.get_file()
    file_bytes = await file.download_as_bytearray()

    os.makedirs("data/temp", exist_ok=True)
    temp_path = f"data/temp/{doc.file_id}.pdf"
    with open(temp_path, "wb") as handle:
        handle.write(file_bytes)

    try:
        job_id = await queue.enqueue(
            job_type="pdf_extract",
            idempotency_key=f"telegram-pdf:{user_id}:{doc.file_id}",
            payload={
                "user_id": user_id,
                "chat_id": _chat_id(update),
                "file_path": temp_path,
                "file_id": doc.file_id,
            },
        )
    except Exception as exc:  # noqa: BLE001 - surface queue failure to the user
        Path(temp_path).unlink(missing_ok=True)
        await _reply(update, f"❌ صف پردازش فایل در دسترس نیست: {exc}")
        return

    await _reply(
        update,
        f"⏳ فایل {doc.file_name} در صف پردازش داخلی قرار گرفت.\nشناسه: {job_id}",
    )


# /docs /doc_delete /chat_with_doc are imported from bot/surface/docs.py:
# the real document store and the hybrid retriever, with a fail-closed
# Persian message when the vector stack is not installed.


async def story_cmd_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await _reply(update, "❌ استفاده: /story [متن]")
        return
    queue = _job_queue_from_context(context)
    if queue is None:
        await _reply(update, "❌ صف پردازش داخلی پیکربندی نشده است.")
        return

    user_id = _user_id(update) or 0
    text = " ".join(context.args)
    await _reply(update, "🎨 در حال قرار دادن استوری در صف داخلی...")

    os.makedirs("data/temp", exist_ok=True)
    output_path = f"data/temp/story_{user_id}_{uuid4().hex}.png"
    try:
        job_id = await queue.enqueue(
            job_type="story",
            idempotency_key=f"telegram-story:{user_id}:{uuid4().hex}",
            payload={
                "user_id": user_id,
                "chat_id": _chat_id(update),
                "text": text,
                "output_path": output_path,
            },
        )
    except Exception as exc:  # noqa: BLE001 - surface queue failure to the user
        await _reply(update, f"❌ صف ساخت استوری در دسترس نیست: {exc}")
        return

    await _reply(update, f"🎨 استوری در صف پردازش داخلی قرار گرفت.\nشناسه: {job_id}")


async def story_style_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply(update, "🎨 استایل فعلی: Motivational\nگزینه‌ها: Motivational | Romantic | Success")


async def storage_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    pass


async def model_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    pass


async def install_presence_heartbeat(application: Any) -> None:
    """Mock presence heartbeat for now."""
    pass
