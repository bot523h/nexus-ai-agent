"""Telegram surface for gamification.

Commands: ``/daily``, ``/profile``, ``/achievements``, ``/xp_leaderboard``.

Replaces the fixed strings that ``bot/handlers.py`` still ships for three of
these commands::

    async def daily_cmd(...)          -> "🎁 Daily reward claimed: +50 XP!"
    async def xp_leaderboard_cmd(...) -> "🏆 **XP Leaderboard**\\n\\n1. UserX: 5000 XP"
    async def achievements_cmd(...)   -> "🏅 **Achievements**\\n\\n- First Message…"

with the real :class:`~nexus_ai_agent.features.gamification.GamificationEngine`,
scoped per ``(user, chat)`` exactly as the engine stores it.

Two engineering notes
---------------------
* **The event loop is never blocked.** Every engine call is synchronous SQLite
  CRUD, so it is off-loaded with :func:`asyncio.to_thread` — the same
  convention ``bot/feature_handlers.py`` adopted in the P0 batch.
* **Formatting is separated from I/O.** The ``format_*`` helpers are pure
  functions over the engine's payloads, which is what makes the rendering
  testable without a bot.
"""

from __future__ import annotations

import asyncio
from typing import Any

from nexus_ai_agent.features.gamification import GamificationEngine

from ._ptb import chat_id, reply, user_id

__all__ = [
    "achievements_cmd",
    "daily_cmd",
    "format_achievements",
    "format_daily",
    "format_leaderboard",
    "format_profile",
    "profile_cmd",
    "xp_leaderboard_cmd",
]

_MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}
_SEPARATOR = "━━━━━━━━━━━━━━━━"


# ── pure rendering ─────────────────────────────────────────────────────────


def format_daily(result: dict[str, Any]) -> str:
    """Render :meth:`GamificationEngine.claim_daily` output for Telegram."""
    if not result.get("claimed"):
        hours = int(result.get("remaining_hours") or 0)
        minutes = int(result.get("remaining_minutes") or 0) % 60
        wait = f"{hours} ساعت و {minutes} دقیقه" if hours else f"{minutes} دقیقه"
        streak = int(result.get("streak") or 0)
        tail = f" 🔥 استریک فعلی: {streak} روز." if streak else ""
        return f"⏳ جایزهٔ روزانه را امروز گرفته‌اید. {wait} دیگر دوباره سر بزنید.{tail}"

    lines = [
        f"🎁 جایزهٔ روزانه: +{result['total_reward']} XP",
        f"  پایه: {result['base_reward']} · پاداش استریک: {result['streak_bonus']}",
        f"🔥 استریک: {result['streak']} روز",
    ]
    if result.get("streak_broken"):
        lines.append("💔 استریک قبلی شکست؛ از نو شروع شد.")
    if result.get("leveled_up"):
        lines.append(f"⬆️ سطح جدید: {result['new_level']} {result.get('title', '')}".rstrip())
    lines.append(f"⭐ مجموع XP: {result.get('xp', '؟')}")
    return "\n".join(lines)


def format_leaderboard(rows: list[dict[str, Any]]) -> str:
    """Render :meth:`GamificationEngine.get_leaderboard` output."""
    if not rows:
        return "🏆 هنوز کسی در این گفتگو XP نگرفته است. با /daily شروع کنید!"

    lines = ["🏆 جدول XP این گفتگو", _SEPARATOR]
    for rank, row in enumerate(rows, start=1):
        badge = _MEDALS.get(rank, f"{rank}.")
        lines.append(
            f"  {badge} کاربر {row['user_id']}: {row['xp']} XP — سطح {row['level']} {row['title']}"
        )
    return "\n".join(lines)


def format_achievements(unlocked: list[str], rendered: str) -> str:
    """Render the achievements block (count + the engine's readable list)."""
    body = rendered or "هنوز دستاوردی نداری!"
    return f"🏅 دستاوردها ({len(unlocked)})\n{_SEPARATOR}\n{body}"


def format_profile(profile: dict[str, Any]) -> str:
    """Render :meth:`GamificationEngine.get_profile` output."""
    remaining = profile.get("xp_to_next")
    progress = f"{remaining} XP تا سطح بعد" if remaining else "حداکثر سطح 🏆"
    return (
        "👤 پروفایل\n"
        f"{_SEPARATOR}\n"
        f"⭐ سطح {profile['level']} {profile['title']}\n"
        f"✨ XP: {profile['xp']} ({progress})\n"
        f"🔥 استریک: {profile['streak']} روز\n"
        f"🏅 دستاوردها: {profile['achievement_count']}"
    )


# ── commands ──────────────────────────────────────────────────────────────


async def daily_cmd(update: Any, context: Any) -> None:
    """``/daily`` — claim the daily XP reward (once per 24 h, streak-aware)."""
    uid, cid = user_id(update), chat_id(update)
    if uid is None or cid is None:
        return
    result = await asyncio.to_thread(GamificationEngine.claim_daily, uid, cid)
    await reply(update, format_daily(result))


async def profile_cmd(update: Any, context: Any) -> None:
    """``/profile`` — level, XP, streak and progress to the next level."""
    uid, cid = user_id(update), chat_id(update)
    if uid is None or cid is None:
        return
    profile = await asyncio.to_thread(GamificationEngine.get_profile, uid, cid)
    await reply(update, format_profile(profile))


async def achievements_cmd(update: Any, context: Any) -> None:
    """``/achievements`` — unlocked achievements of the caller in this chat."""
    uid, cid = user_id(update), chat_id(update)
    if uid is None or cid is None:
        return
    unlocked, rendered = await asyncio.to_thread(_achievements_sync, uid, cid)
    await reply(update, format_achievements(unlocked, rendered))


async def xp_leaderboard_cmd(update: Any, context: Any) -> None:
    """``/xp_leaderboard`` — top XP holders of this chat."""
    cid = chat_id(update)
    if cid is None:
        return
    rows = await asyncio.to_thread(GamificationEngine.get_leaderboard, cid)
    await reply(update, format_leaderboard(list(rows)))


def _achievements_sync(uid: int, cid: int) -> tuple[list[str], str]:
    """Both achievement calls in one thread hop (they hit the same database)."""
    unlocked = GamificationEngine.get_achievements(uid, cid)
    return unlocked, GamificationEngine.format_achievements(unlocked)
