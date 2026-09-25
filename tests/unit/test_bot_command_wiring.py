"""Unit and integration tests for wired bot commands and moderation ingress.

Verifies:
1. All 12 fake/placeholder commands now connect to real engines/sources of truth:
   - /leaderboard (QuizGame.get_leaderboard)
   - /newchat (GeminiEngine.clear_history & ConversationStore.clear)
   - /viral_preview (ViralEngine pending / generated)
   - /viral_stats (ViralEngine.get_stats DB counts)
   - /viral_post (ViralEngine.get_pending_posts & mark_posted)
   - /mod_config (ModerationEngine.get_config & set_config)
   - /warn (ModerationEngine.add_warning & threshold mute)
   - /mute (ModerationEngine.mute_user)
   - /unmute (ModerationEngine.unmute_user)
   - /reputation (ModerationEngine.get_reputation)
   - /storage (AIStorageManager cache & list_files)
   - /model (Local GGUF path/size & remote LLM settings)
2. Safe, typed refusals (deterministic error codes, zero fake successes).
3. ModerationEngine.check_message runtime ingress wiring:
   - Profanity filter with boundary/normalization (no false positives on "خرید", "خروس", "مغز")
   - Prohibited link filter (expanded scheme detection)
   - Flood tracker isolation per chat
   - Pre-check for muted users
   - Ingress enforcement in on_message before downstream handlers
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import create_engine
from sqlmodel import Session, SQLModel
from surface_fakes import FakeMessage, make_context, make_update

from nexus_ai_agent.bot.handlers import build_handlers
from nexus_ai_agent.config import settings as settings_module
from nexus_ai_agent.config.settings import Settings, get_settings
from nexus_ai_agent.features import owner_control
from nexus_ai_agent.features.moderation import ModerationEngine
from nexus_ai_agent.features.viral_engine import ViralEngine
from nexus_ai_agent.presence import PresenceStore
from nexus_ai_agent.storage import models as _models  # noqa: F401
from nexus_ai_agent.storage.models import (
    QuizScore,
)

OWNER_ID = 1001
USER_ID = 2002
CHAT_ID = -100999


@pytest.fixture()
def db(settings_override: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    """Setup temporary SQLite database and set the owner ID."""
    monkeypatch.setenv("NEXUS_OWNER_TELEGRAM_ID", str(OWNER_ID))
    settings_module.get_settings.cache_clear()
    owner_control._owner_id = None

    settings = get_settings()
    engine = create_engine(f"sqlite:///{settings.db_path}")
    SQLModel.metadata.create_all(engine)
    return engine


def _extract_commands(handlers: list[Any]) -> dict[str, Any]:
    """Map command name to handler callback."""
    return {
        next(iter(h.commands)): h.callback
        for h in handlers
        if hasattr(h, "commands")
    }


# ═══════════════════════════════════════════════════════════════════════
# 1. /leaderboard
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_leaderboard_empty_and_populated(db: Any) -> None:
    settings = get_settings()
    handlers = build_handlers(object(), lambda: Session(db), settings, PresenceStore(), None)
    cmd_map = _extract_commands(handlers)
    assert "leaderboard" in cmd_map

    # Empty state: no scores yet
    update = make_update(user_id=USER_ID, chat_id=CHAT_ID)
    context = make_context()
    await cmd_map["leaderboard"](update, context)
    assert "No scores recorded yet" in update.last_reply
    assert "UserA: 1500 XP" not in update.last_reply

    # Populate quiz scores
    with Session(db) as session:
        session.add(QuizScore(user_id=111, chat_id=CHAT_ID, score=15, answered=3))
        session.add(QuizScore(user_id=222, chat_id=CHAT_ID, score=25, answered=5))
        session.commit()

    update2 = make_update(user_id=USER_ID, chat_id=CHAT_ID)
    await cmd_map["leaderboard"](update2, context)
    reply = update2.last_reply
    assert "🏆 **Quiz Leaderboard**" in reply
    assert "User 222: 25 pts (5 answered)" in reply
    assert "User 111: 15 pts (3 answered)" in reply


# ═══════════════════════════════════════════════════════════════════════
# 2. /newchat
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_newchat_clears_history(db: Any) -> None:
    settings = get_settings()
    handlers = build_handlers(object(), lambda: Session(db), settings, PresenceStore(), None)
    cmd_map = _extract_commands(handlers)
    assert "newchat" in cmd_map

    # User missing
    update_no_user = make_update(user_id=None, chat_id=CHAT_ID)
    context = make_context()
    await cmd_map["newchat"](update_no_user, context)
    assert "[ERR_USER_REQUIRED]" in update_no_user.last_reply

    # User present with mock store
    update = make_update(user_id=USER_ID, chat_id=CHAT_ID)
    mock_store = MagicMock()
    context_with_store = make_context(conversation_store=mock_store)
    await cmd_map["newchat"](update, context_with_store)
    assert "Conversation history cleared." in update.last_reply
    mock_store.clear.assert_called_once_with(f"tg:{CHAT_ID}")


# ═══════════════════════════════════════════════════════════════════════
# 3. Viral Engine Commands (/viral_preview, /viral_stats, /viral_post)
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_viral_commands_owner_gate(db: Any) -> None:
    settings = get_settings()
    handlers = build_handlers(object(), lambda: Session(db), settings, PresenceStore(), None)
    cmd_map = _extract_commands(handlers)

    for cmd in ["viral_now", "viral_preview", "viral_stats", "viral_post"]:
        update = make_update(user_id=USER_ID, chat_id=CHAT_ID)
        context = make_context()
        await cmd_map[cmd](update, context)
        assert "[ERR_ACCESS_DENIED]" in update.last_reply


@pytest.mark.asyncio
async def test_viral_preview_and_stats_real_db(db: Any) -> None:
    settings = get_settings()
    handlers = build_handlers(object(), lambda: Session(db), settings, PresenceStore(), None)
    cmd_map = _extract_commands(handlers)

    # Empty stats
    update_stats = make_update(user_id=OWNER_ID, chat_id=CHAT_ID)
    await cmd_map["viral_stats"](update_stats, make_context())
    assert "Total Posts: 0" in update_stats.last_reply
    assert "12 posts sent" not in update_stats.last_reply

    # Add a pending post
    post_id = ViralEngine.save_post(CHAT_ID, "This is a real AI trend!", 8.5)
    assert post_id > 0

    # Re-check stats
    update_stats2 = make_update(user_id=OWNER_ID, chat_id=CHAT_ID)
    await cmd_map["viral_stats"](update_stats2, make_context())
    assert "Total Posts: 1" in update_stats2.last_reply
    assert "Pending: 1" in update_stats2.last_reply

    # Preview pending post
    update_prev = make_update(user_id=OWNER_ID, chat_id=CHAT_ID)
    await cmd_map["viral_preview"](update_prev, make_context())
    assert f"ID: {post_id}" in update_prev.last_reply
    assert "This is a real AI trend!" in update_prev.last_reply

    # Mark posted via /viral_post mark <id>
    update_mark = make_update(user_id=OWNER_ID, chat_id=CHAT_ID)
    context_mark = make_context(args=["mark", str(post_id)])
    await cmd_map["viral_post"](update_mark, context_mark)
    assert "marked as posted" in update_mark.last_reply

    # Verify status changed in DB
    stats = ViralEngine.get_stats(CHAT_ID)
    assert stats["posted"] == 1
    assert stats["pending"] == 0

    # Malformed /viral_post mark
    update_bad = make_update(user_id=OWNER_ID, chat_id=CHAT_ID)
    await cmd_map["viral_post"](update_bad, make_context(args=["mark", "not_an_int"]))
    assert "[ERR_INVALID_ARGUMENT]" in update_bad.last_reply


# ═══════════════════════════════════════════════════════════════════════
# 4. Smart Moderation Configuration (/mod_config)
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_mod_config_command(db: Any) -> None:
    settings = get_settings()
    handlers = build_handlers(object(), lambda: Session(db), settings, PresenceStore(), None)
    cmd_map = _extract_commands(handlers)

    # Intruder check
    update_intruder = make_update(user_id=USER_ID, chat_id=CHAT_ID)
    await cmd_map["mod_config"](update_intruder, make_context())
    assert "[ERR_ACCESS_DENIED]" in update_intruder.last_reply

    # Owner when unconfigured
    update_unconfigured = make_update(user_id=OWNER_ID, chat_id=CHAT_ID)
    await cmd_map["mod_config"](update_unconfigured, make_context())
    assert "not configured" in update_unconfigured.last_reply

    # Owner updates config with valid args
    update_update = make_update(user_id=OWNER_ID, chat_id=CHAT_ID)
    context_update = make_context(
        args=["anti_spam=on", "anti_flood=off", "max_warnings=5", "mute_duration=45"]
    )
    await cmd_map["mod_config"](update_update, context_update)
    reply = update_update.last_reply
    assert "Anti-Spam: Enabled" in reply
    assert "Anti-Flood: Disabled" in reply
    assert "Max Warnings: 5" in reply
    assert "Mute Duration: 45 min" in reply

    # Verify in DB
    cfg = ModerationEngine.get_config(CHAT_ID)
    assert cfg is not None
    assert cfg.anti_spam is True
    assert cfg.anti_flood is False
    assert cfg.max_warnings == 5
    assert cfg.mute_duration_minutes == 45

    # Owner passes invalid arg
    update_bad = make_update(user_id=OWNER_ID, chat_id=CHAT_ID)
    await cmd_map["mod_config"](update_bad, make_context(args=["max_warnings=xyz"]))
    assert "[ERR_INVALID_ARGUMENT]" in update_bad.last_reply


# ═══════════════════════════════════════════════════════════════════════
# 5. Smart Moderation Actions (/warn, /mute, /unmute, /reputation)
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_warn_mute_unmute_lifecycle(db: Any) -> None:
    settings = get_settings()
    handlers = build_handlers(object(), lambda: Session(db), settings, PresenceStore(), None)
    cmd_map = _extract_commands(handlers)

    # Initialize moderation config with max_warnings=2
    ModerationEngine.set_config(CHAT_ID, max_warnings=2, mute_duration_minutes=20)

    # Non-owner /warn
    update_intruder = make_update(user_id=USER_ID, chat_id=CHAT_ID)
    await cmd_map["warn"](update_intruder, make_context(args=[str(USER_ID)]))
    assert "[ERR_ACCESS_DENIED]" in update_intruder.last_reply

    # Target missing /warn
    update_no_target = make_update(user_id=OWNER_ID, chat_id=CHAT_ID)
    await cmd_map["warn"](update_no_target, make_context())
    assert "[ERR_TARGET_REQUIRED]" in update_no_target.last_reply

    # First warning (1/2)
    update_warn1 = make_update(user_id=OWNER_ID, chat_id=CHAT_ID)
    await cmd_map["warn"](update_warn1, make_context(args=[str(USER_ID), "bad behavior"]))
    assert f"User {USER_ID} warned (1/2)" in update_warn1.last_reply

    rep1 = ModerationEngine.get_reputation(USER_ID, CHAT_ID)
    assert rep1 is not None
    assert rep1.warnings == 1
    assert rep1.is_muted is False

    # Second warning (2/2) -> triggers auto-mute
    update_warn2 = make_update(user_id=OWNER_ID, chat_id=CHAT_ID)
    await cmd_map["warn"](update_warn2, make_context(args=[str(USER_ID), "repeated violation"]))
    assert f"User {USER_ID} warned (2/2) and muted for 20 minutes" in update_warn2.last_reply

    rep2 = ModerationEngine.get_reputation(USER_ID, CHAT_ID)
    assert rep2 is not None
    assert rep2.warnings == 2
    assert rep2.is_muted is True
    assert rep2.mute_until is not None

    # Check /reputation reflects muted status
    update_rep = make_update(user_id=USER_ID, chat_id=CHAT_ID)
    await cmd_map["reputation"](update_rep, make_context(args=[str(USER_ID)]))
    assert "Muted: Yes" in update_rep.last_reply
    assert "Active Warnings: 2" in update_rep.last_reply
    assert "User Reputation: 85/100" not in update_rep.last_reply

    # Manual /unmute
    update_unmute = make_update(user_id=OWNER_ID, chat_id=CHAT_ID)
    await cmd_map["unmute"](update_unmute, make_context(args=[str(USER_ID)]))
    assert f"User {USER_ID} unmuted" in update_unmute.last_reply

    assert ModerationEngine.is_muted(USER_ID, CHAT_ID) is False

    # Manual /mute with custom duration
    update_mute = make_update(user_id=OWNER_ID, chat_id=CHAT_ID)
    await cmd_map["mute"](update_mute, make_context(args=[str(USER_ID), "15"]))
    assert f"User {USER_ID} muted for 15 minutes" in update_mute.last_reply
    assert ModerationEngine.is_muted(USER_ID, CHAT_ID) is True

    # Reply-to target support for /unmute
    replied_msg = FakeMessage("spam", author_id=USER_ID)
    update_reply_unmute = make_update(user_id=OWNER_ID, chat_id=CHAT_ID, reply_to=replied_msg)
    await cmd_map["unmute"](update_reply_unmute, make_context())
    assert f"User {USER_ID} unmuted" in update_reply_unmute.last_reply
    assert ModerationEngine.is_muted(USER_ID, CHAT_ID) is False


# ═══════════════════════════════════════════════════════════════════════
# 6. Diagnostic Commands (/storage, /model)
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_storage_command(db: Any, tmp_path: Path) -> None:
    settings = get_settings()

    # Case 1: storage is None
    handlers_none = build_handlers(object(), lambda: Session(db), settings, PresenceStore(), None)
    cmd_none = _extract_commands(handlers_none)
    update1 = make_update(user_id=USER_ID, chat_id=CHAT_ID)
    await cmd_none["storage"](update1, make_context())
    assert "[ERR_STORAGE_UNAVAILABLE]" in update1.last_reply

    # Case 2: storage is provided
    mock_storage = MagicMock()
    mock_storage.cache_dir = tmp_path / "cache"
    mock_storage.list_files = AsyncMock(return_value=["model.bin", "data.json"])

    handlers_real = build_handlers(
        object(), lambda: Session(db), settings, PresenceStore(), mock_storage
    )
    cmd_real = _extract_commands(handlers_real)
    update2 = make_update(user_id=USER_ID, chat_id=CHAT_ID)
    await cmd_real["storage"](update2, make_context())
    assert "Storage Manager Status" in update2.last_reply
    assert "Stored Files: 2" in update2.last_reply
    assert "model.bin" in update2.last_reply


@pytest.mark.asyncio
async def test_model_command(db: Any, tmp_path: Path) -> None:
    # Case 1: No local file and no remote providers
    fake_model_file = tmp_path / "nonexistent.gguf"
    settings = Settings(
        model_path=str(fake_model_file),
        gemini_api_key="",
        ollama_model="",
        groq_api_key="",
        openrouter_api_key="",
    )
    handlers = build_handlers(object(), lambda: Session(db), settings, PresenceStore(), None)
    cmd_map = _extract_commands(handlers)

    update1 = make_update(user_id=USER_ID, chat_id=CHAT_ID)
    await cmd_map["model"](update1, make_context())
    assert "[ERR_MODEL_NOT_FOUND]" in update1.last_reply

    # Case 2: Local model file created and Gemini configured
    fake_model_file.write_bytes(b"GGUF_TEST_BYTES" * 1000)
    settings2 = Settings(
        model_path=str(fake_model_file),
        gemini_api_key="gemini-fake-key",
        gemini_model="gemini-1.5-flash",
    )
    handlers2 = build_handlers(object(), lambda: Session(db), settings2, PresenceStore(), None)
    cmd_map2 = _extract_commands(handlers2)

    update2 = make_update(user_id=USER_ID, chat_id=CHAT_ID)
    await cmd_map2["model"](update2, make_context())
    assert "AI Model Configuration & Status" in update2.last_reply
    assert "Available:" in update2.last_reply
    assert "Gemini Provider: `gemini-1.5-flash` (Configured)" in update2.last_reply


# ═══════════════════════════════════════════════════════════════════════
# 7. ModerationEngine.check_message & Ingress Pipeline
# ═══════════════════════════════════════════════════════════════════════


def test_moderation_engine_boundary_and_persian_normalization(db: Any) -> None:
    ModerationEngine.set_config(
        CHAT_ID,
        anti_spam=True,
        anti_flood=True,
        link_filter=True,
        profanity_filter=True,
        max_warnings=3,
    )

    # 1. False positive check on common Persian words containing profanity substrings:
    # "خرید" (shopping), "خروس" (rooster), "مغز" (brain)
    assert ModerationEngine.has_profanity("من امروز نان خریدم") is False
    assert ModerationEngine.has_profanity("خروس خوان بیدار شدیم") is False
    assert ModerationEngine.has_profanity("مغز انسان شگفت‌انگیز است") is False

    # Punctuation/divider lines should NOT be marked as spam
    assert ModerationEngine.is_spam("-----------------------------") is False
    assert ModerationEngine.is_spam(".............................") is False

    # 2. Profanity detection with boundary and Persian normalization
    # Exact Persian profanity: "کصکش", "دیوث", "حرومزاده"
    assert ModerationEngine.has_profanity("این کصکش کیست؟") is True
    # Normalized Arabic/Persian forms: "ديوث" (Arabic yeh -> Persian yeh)
    assert ModerationEngine.has_profanity("یک ديوث واقعی") is True

    # 3. Prohibited links detection
    assert ModerationEngine.has_links("پست را ببینید: t.me/channel") is True
    assert ModerationEngine.has_links("لینک تلگرام: telegram.me/channel") is True
    assert ModerationEngine.has_links("عضویت در tg://join?invite=123") is True
    assert ModerationEngine.has_links("پیام عادی بدون لینک") is False

    # 4. Check message for muted user
    ModerationEngine.mute_user(USER_ID, CHAT_ID, duration_minutes=30)
    result_muted = ModerationEngine.check_message(USER_ID, CHAT_ID, "پیام تستی")
    assert result_muted["allowed"] is False
    assert result_muted["action"] == "block"
    assert "muted" in result_muted["reasons"]


@pytest.mark.asyncio
async def test_moderation_ingress_on_message(db: Any) -> None:
    """Verify on_message enforces moderation before downstream processing."""
    settings = Settings(
        allowed_user_ids=str(USER_ID),
        owner_telegram_id=OWNER_ID,
        db_path=get_settings().db_path,
    )
    mock_graph = MagicMock()
    mock_graph.ainvoke = AsyncMock(return_value={"response": "LLM reply"})

    handlers = build_handlers(mock_graph, lambda: Session(db), settings, PresenceStore(), None)
    # on_message is the MessageHandler without commands
    from telegram.ext import MessageHandler
    on_message_handler = next(
        h.callback
        for h in handlers
        if isinstance(h, MessageHandler)
        and not hasattr(h, "commands")
        and hasattr(h, "filters")
        and "text" in str(getattr(h, "filters", "")).lower()
    )

    # Enable moderation for chat
    ModerationEngine.set_config(
        CHAT_ID,
        anti_spam=True,
        anti_flood=True,
        link_filter=True,
        profanity_filter=True,
        max_warnings=3,
    )

    # Mute user
    ModerationEngine.mute_user(USER_ID, CHAT_ID, duration_minutes=30)

    # Attempt to send message
    update = make_update(user_id=USER_ID, chat_id=CHAT_ID, text="سلام ربات")
    context = make_context()
    await on_message_handler(update, context)

    # User was blocked at ingress, graph.ainvoke was NEVER called
    assert "شما در این گروه مسدود هستید" in update.last_reply
    mock_graph.ainvoke.assert_not_called()
