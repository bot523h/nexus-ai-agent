"""Behavioural tests for the gamification surface (``bot/surface/gamification.py``).

The point of these tests is the difference between a *stub* and a *wiring*:
``bot/handlers.py`` answers ``/daily`` with the fixed string
``"🎁 Daily reward claimed: +50 XP!"`` no matter what the engine says. These
tests run the real ``GamificationEngine`` against a temp SQLite database and
assert the numbers in the reply come from it.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlmodel import Session, SQLModel, select
from surface_fakes import make_context, make_update

from nexus_ai_agent.bot.surface import gamification as surface
from nexus_ai_agent.config.settings import get_settings
from nexus_ai_agent.features.gamification import GamificationEngine
from nexus_ai_agent.storage import models as _models  # noqa: F401  (registers tables)
from nexus_ai_agent.storage.models import UserXP

#: The exact strings handlers.py used to answer with — they must never come back.
STUB_STRINGS = (
    "Daily reward claimed: +50 XP!",
    "**XP Leaderboard**",
    "1. UserX: 5000 XP",
    "**Achievements**",
    "- First Message",
)


@pytest.fixture()
def db(settings_override: Any) -> Any:
    settings = get_settings()
    engine = create_engine(f"sqlite:///{settings.db_path}")
    SQLModel.metadata.create_all(engine)
    engine.dispose()
    return settings


# ── /daily ────────────────────────────────────────────────────────────────


async def test_daily_reports_the_real_reward(db: Any) -> None:
    update, context = make_update(), make_context()

    await surface.daily_cmd(update, context)

    text = update.last_reply
    assert "🎁" in text
    assert "+30 XP" in text  # 25 base + 5 streak bonus, computed by the engine
    assert "استریک: 1 روز" in text
    assert _row_xp(1, 10) == 30  # persisted, not just printed
    assert not any(stub in text for stub in STUB_STRINGS)


async def test_daily_twice_is_refused_with_a_countdown(db: Any) -> None:
    await surface.daily_cmd(make_update(), make_context())

    update = make_update()
    await surface.daily_cmd(update, make_context())

    assert "⏳" in update.last_reply
    assert "دیگر دوباره سر بزنید" in update.last_reply
    assert _row_xp(1, 10) == 30  # the second claim added nothing


async def test_daily_is_scoped_per_chat(db: Any) -> None:
    """XP is (user, chat) scoped: another chat is another profile."""
    await surface.daily_cmd(make_update(chat_id=10), make_context())
    await surface.daily_cmd(make_update(chat_id=11), make_context())

    assert _row_xp(1, 10) == 30
    assert _row_xp(1, 11) == 30


# ── /profile, /achievements, /xp_leaderboard ──────────────────────────────


async def test_profile_renders_level_and_progress(db: Any) -> None:
    GamificationEngine.add_xp(5, 50, 120)

    update = make_update(user_id=5, chat_id=50)
    await surface.profile_cmd(update, make_context())

    text = update.last_reply
    assert "👤 پروفایل" in text
    assert "XP: 120" in text
    assert "سطح 1" in text  # 120 XP is below the 150 XP threshold for level 2
    assert "30 XP تا سطح بعد" in text
    assert not any(stub in text for stub in STUB_STRINGS)


async def test_achievements_lists_what_the_user_unlocked(db: Any) -> None:
    GamificationEngine.add_xp(7, 70, 10)  # unlock_* needs an existing row
    assert GamificationEngine.unlock_achievement(7, 70, "first_message") is True

    update = make_update(user_id=7, chat_id=70)
    await surface.achievements_cmd(update, make_context())

    text = update.last_reply
    assert "🏅 دستاوردها (1)" in text
    assert "💬 اولین قدم" in text  # the id is rendered as its human label
    assert "first_message" not in text
    assert not any(stub in text for stub in STUB_STRINGS)


async def test_achievements_of_a_new_user_is_the_empty_state(db: Any) -> None:
    update = make_update(user_id=8, chat_id=80)

    await surface.achievements_cmd(update, make_context())

    assert "🏅 دستاوردها (0)" in update.last_reply


async def test_leaderboard_lists_real_xp_holders(db: Any) -> None:
    GamificationEngine.add_xp(1, 10, 300)
    GamificationEngine.add_xp(2, 10, 120)

    update = make_update(user_id=1, chat_id=10)
    await surface.xp_leaderboard_cmd(update, make_context())

    text = update.last_reply
    assert "🥇" in text and "🥈" in text
    assert "کاربر 1: 300 XP" in text
    assert "کاربر 2: 120 XP" in text
    assert not any(stub in text for stub in STUB_STRINGS)


async def test_leaderboard_empty_state_invites_the_first_claim(db: Any) -> None:
    update = make_update(chat_id=99)

    await surface.xp_leaderboard_cmd(update, make_context())

    assert "/daily" in update.last_reply


# ── robustness ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("command", ["daily_cmd", "profile_cmd", "achievements_cmd"])
async def test_commands_ignore_updates_without_a_user(command: str) -> None:
    """A malformed update degrades to silence — it must never raise."""
    update = make_update(user_id=None)

    await getattr(surface, command)(update, make_context())

    assert update.replies == []


async def test_commands_ignore_updates_without_a_chat() -> None:
    update = make_update(chat_id=None)

    await surface.xp_leaderboard_cmd(update, make_context())

    assert update.replies == []


async def test_blocking_engine_calls_are_off_the_event_loop(
    db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The engine is synchronous SQLite: it must be called through a thread."""
    seen: list[str] = []
    real_to_thread = asyncio.to_thread

    async def _spy(func: Any, /, *params: Any) -> Any:
        seen.append(getattr(func, "__name__", "?"))
        return await real_to_thread(func, *params)

    monkeypatch.setattr("asyncio.to_thread", _spy)

    await surface.daily_cmd(make_update(), make_context())

    assert seen == ["claim_daily"]


# ── pure renderers ────────────────────────────────────────────────────────


def test_format_daily_renders_a_claimed_reward() -> None:
    text = surface.format_daily(
        {
            "claimed": True,
            "base_reward": 25,
            "streak_bonus": 5,
            "total_reward": 30,
            "streak": 4,
            "streak_broken": True,
            "leveled_up": True,
            "new_level": 2,
            "title": "⚡ فعال",
            "xp": 180,
        }
    )
    assert "+30 XP" in text
    assert "💔 استریک قبلی شکست" in text
    assert "⬆️ سطح جدید: 2 ⚡ فعال" in text
    assert "⭐ مجموع XP: 180" in text


def test_format_daily_renders_the_wait_state() -> None:
    text = surface.format_daily(
        {
            "claimed": False,
            "reason": "already_claimed",
            "remaining_hours": 5,
            "remaining_minutes": 311,
            "streak": 3,
        }
    )
    assert "⏳" in text
    assert "5 ساعت و 11 دقیقه" in text
    assert "استریک فعلی: 3 روز" in text


def test_format_daily_tolerates_a_sparse_payload() -> None:
    """A payload from an older engine must still render, not raise."""
    assert "⏳" in surface.format_daily({"claimed": False})
    assert "🎁" in surface.format_daily(
        {"claimed": True, "total_reward": 25, "base_reward": 25, "streak_bonus": 0, "streak": 1}
    )


def test_format_leaderboard_medals_only_the_podium() -> None:
    rows = [
        {"user_id": 1, "xp": 100, "level": 2, "title": "A"},
        {"user_id": 2, "xp": 90, "level": 2, "title": "B"},
        {"user_id": 3, "xp": 80, "level": 1, "title": "C"},
        {"user_id": 4, "xp": 70, "level": 1, "title": "D"},
    ]
    lines = surface.format_leaderboard(rows).splitlines()
    assert lines[-1].startswith("  4.")
    assert "🥇" in lines[2] and "🥈" in lines[3] and "🥉" in lines[4]


def test_format_profile_handles_the_max_level() -> None:
    text = surface.format_profile(
        {
            "level": 15,
            "title": "🔱 خدا",
            "xp": 99999,
            "xp_to_next": 0,
            "streak": 9,
            "achievement_count": 4,
            "achievements": [],
        }
    )
    assert "حداکثر سطح 🏆" in text


def _row_xp(user_id: int, chat_id: int) -> int:
    settings = get_settings()
    engine = create_engine(f"sqlite:///{settings.db_path}")
    with Session(engine) as session:
        row = session.exec(
            select(UserXP).where(UserXP.user_id == user_id, UserXP.chat_id == chat_id)
        ).first()
        return row.xp if row else -1
