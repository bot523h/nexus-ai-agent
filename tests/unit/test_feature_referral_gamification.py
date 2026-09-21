"""B3 (referral deep link) and B6 (``/daily`` → GamificationEngine)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlmodel import Session, select
from surface_fakes import FakeBot, make_context, make_update

from nexus_ai_agent.bot.surface import gamification as gsurface
from nexus_ai_agent.bot.surface import referral as rsurface
from nexus_ai_agent.features.gamification import (
    XP_DAILY_BONUS,
    XP_STREAK_BONUS,
    GamificationEngine,
)
from nexus_ai_agent.storage.models import UserXP

# ── B3: referral ─────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _fresh_referral_engine() -> None:
    rsurface.reset_referral_engine()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (["ref_NEXUS-1-ABC"], "ref_NEXUS-1-ABC"),
        (["ref_"], None),
        (["hello"], None),
        ([], None),
        (["ref_" + "x" * 80], None),
    ],
)
def test_parse_start_param(raw: list[str], expected: str | None) -> None:
    assert rsurface.parse_start_param(raw) == expected


@pytest.mark.asyncio
async def test_start_without_code_is_a_noop(feature_db) -> None:
    assert await rsurface.apply_start_referral(make_update(user_id=5), make_context([])) is None


@pytest.mark.asyncio
async def test_referral_deep_link_records_and_notifies(feature_db) -> None:
    engine = rsurface.get_referral_engine()
    code = engine.get_or_create_code(1000)
    bot = FakeBot()

    line = await rsurface.apply_start_referral(
        make_update(user_id=2000), make_context([f"ref_{code}"], bot=bot)
    )
    assert line is not None and "+50 XP" in line
    assert bot.sent[0]["chat_id"] == 1000
    assert "دعوت‌های موفق: 1" in bot.sent[0]["text"]
    assert engine.get_referral_stats(1000)["total"] == 1
    # both sides got XP (referrer in their own DM scope)
    assert GamificationEngine.get_profile(2000, 2000)["xp"] == 50
    assert GamificationEngine.get_profile(1000, 1000)["xp"] == 50

    # second /start with the same code: already referred → silent
    again = await rsurface.apply_start_referral(
        make_update(user_id=2000), make_context([f"ref_{code}"], bot=bot)
    )
    assert again is None
    assert len(bot.sent) == 1


@pytest.mark.asyncio
async def test_referral_error_messages(feature_db) -> None:
    engine = rsurface.get_referral_engine()
    code = engine.get_or_create_code(1)
    self_ref = await rsurface.apply_start_referral(
        make_update(user_id=1), make_context([f"ref_{code}"], bot=FakeBot())
    )
    assert self_ref is not None and "خودتان" in self_ref
    unknown = await rsurface.apply_start_referral(
        make_update(user_id=3), make_context(["ref_NOPE"], bot=FakeBot())
    )
    assert unknown is not None and "نامعتبر" in unknown


@pytest.mark.asyncio
async def test_referral_survives_notification_failure(feature_db) -> None:
    engine = rsurface.get_referral_engine()
    code = engine.get_or_create_code(10)
    bot = FakeBot()
    bot.fail_for.add(10)
    line = await rsurface.apply_start_referral(
        make_update(user_id=20), make_context([f"ref_{code}"], bot=bot)
    )
    assert line is not None and line.startswith("🎁")
    assert engine.get_referral_stats(10)["total"] == 1


# ── B6: /daily ───────────────────────────────────────────────────────


def _set_last_daily(db_path: str, user_id: int, chat_id: int, when: datetime) -> None:
    engine = create_engine(f"sqlite:///{db_path}")
    with Session(engine) as session:
        row = session.exec(
            select(UserXP).where(UserXP.user_id == user_id, UserXP.chat_id == chat_id)
        ).one()
        row.last_daily = when.replace(tzinfo=None)  # SQLite stores naive UTC
        session.add(row)
        session.commit()
    engine.dispose()


def test_claim_daily_first_claim_then_cooldown(feature_db) -> None:
    first = GamificationEngine.claim_daily(1, 1)
    assert first["claimed"] is True
    assert first["streak"] == 1
    assert first["streak_bonus"] == XP_STREAK_BONUS
    assert first["total_reward"] == XP_DAILY_BONUS + XP_STREAK_BONUS
    assert first["xp"] == first["total_reward"]

    second = GamificationEngine.claim_daily(1, 1)
    assert second["claimed"] is False
    assert second["reason"] == "already_claimed"
    assert second["remaining_hours"] == 23
    assert second["remaining_minutes"] >= 23 * 60


def test_claim_daily_streak_continues_and_breaks(feature_db) -> None:
    GamificationEngine.claim_daily(2, 2)
    _set_last_daily(feature_db.db_path, 2, 2, datetime.now(timezone.utc) - timedelta(hours=30))
    cont = GamificationEngine.claim_daily(2, 2)
    assert cont["claimed"] and cont["streak"] == 2 and not cont["streak_broken"]

    _set_last_daily(feature_db.db_path, 2, 2, datetime.now(timezone.utc) - timedelta(days=5))
    broken = GamificationEngine.claim_daily(2, 2)
    assert broken["claimed"] and broken["streak"] == 1 and broken["streak_broken"]
    assert GamificationEngine.get_profile(2, 2)["xp"] == (
        cont["total_reward"] + broken["total_reward"] + (XP_DAILY_BONUS + XP_STREAK_BONUS)
    )


def test_format_daily() -> None:
    assert "⏳" in gsurface.format_daily(
        {"claimed": False, "remaining_hours": 5, "remaining_minutes": 5 * 60 + 7}
    )
    assert "5 ساعت و 7 دقیقه" in gsurface.format_daily(
        {"claimed": False, "remaining_hours": 5, "remaining_minutes": 5 * 60 + 7}
    )
    text = gsurface.format_daily(
        {
            "claimed": True,
            "base_reward": 25,
            "streak_bonus": 10,
            "total_reward": 35,
            "streak": 2,
            "streak_broken": False,
            "leveled_up": True,
            "new_level": 1,
            "title": "🌱 تازه‌کار",
            "xp": 35,
        }
    )
    assert "+35 XP" in text and "سطح جدید: 1" in text and "استریک: 2" in text


@pytest.mark.asyncio
async def test_daily_cmd_uses_engine(feature_db) -> None:
    update = make_update(user_id=9, chat_id=-9)
    await gsurface.daily_cmd(update, make_context())
    assert update.message.last.startswith("🎁 جایزه روزانه: +")
    update = make_update(user_id=9, chat_id=-9)
    await gsurface.daily_cmd(update, make_context())
    assert update.message.last.startswith("⏳")
    assert GamificationEngine.get_profile(9, -9)["xp"] == XP_DAILY_BONUS + XP_STREAK_BONUS


@pytest.mark.asyncio
async def test_profile_leaderboard_and_achievements_cmds(feature_db) -> None:
    GamificationEngine.add_xp(1, -1, 120)
    GamificationEngine.add_xp(2, -1, 30)

    update = make_update(user_id=1, chat_id=-1)
    await gsurface.xp_leaderboard_cmd(update, make_context())
    text = update.message.last
    assert "🥇 کاربر 1: 120 XP" in text and "🥈 کاربر 2: 30 XP" in text

    update = make_update(user_id=1, chat_id=-1)
    await gsurface.profile_cmd(update, make_context())
    assert "XP: 120" in update.message.last

    update = make_update(user_id=1, chat_id=-1)
    await gsurface.achievements_cmd(update, make_context())
    assert update.message.last.startswith("🏅 دستاوردها")

    update = make_update(user_id=1, chat_id=-2)
    await gsurface.xp_leaderboard_cmd(update, make_context())
    assert "هنوز کسی" in update.message.last
