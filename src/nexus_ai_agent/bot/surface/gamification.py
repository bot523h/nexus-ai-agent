"""Telegram surface for gamification: /daily, /xp_leaderboard, /achievements, /profile.

Replaces the fixed strings (``"+50 XP!"``, ``"UserX: 5000 XP"``) with the
real :class:`~nexus_ai_agent.features.gamification.GamificationEngine`.
XP is scoped per ``(user, chat)`` exactly as the engine stores it.
"""

from __future__ import annotations

from typing import Any

from nexus_ai_agent.features.gamification import GamificationEngine

from ._ptb import chat_id, reply, user_id

__all__ = ["achievements_cmd", "daily_cmd", "format_daily", "profile_cmd", "xp_leaderboard_cmd"]


def format_daily(result: dict[str, Any]) -> str:
    """Render :meth:`GamificationEngine.claim_daily` output for Telegram."""
    if not result.get("claimed"):
        hours = int(result.get("remaining_hours", 0))
        minutes = int(result.get("remaining_minutes", 0)) % 60
        wait = f"{hours} ساعت و {minutes} دقیقه" if hours else f"{minutes} دقیقه"
        return f"⏳ جایزه روزانه را قبلاً گرفته‌اید. {wait} دیگر دوباره سر بزنید."
    lines = [
        f"🎁 جایزه روزانه: +{result['total_reward']} XP",
        f"  پایه: {result['base_reward']} · پاداش استریک: {result['streak_bonus']}",
        f"🔥 استریک: {result['streak']} روز",
    ]
    if result.get("streak_broken"):
        lines.append("💔 استریک قبلی شکست؛ از نو شروع شد.")
    if result.get("leveled_up"):
        lines.append(f"⬆️ سطح جدید: {result['new_level']} {result.get('title', '')}".rstrip())
    lines.append(f"⭐ مجموع XP: {result.get('xp', '?')}")
    return "\n".join(lines)


async def daily_cmd(update: Any, context: Any) -> None:
    """``/daily`` — claim the daily XP reward (once per 24 h, streak-aware)."""
    uid, cid = user_id(update), chat_id(update)
    if uid is None or cid is None:
        return
    await reply(update, format_daily(GamificationEngine.claim_daily(uid, cid)))


async def xp_leaderboard_cmd(update: Any, context: Any) -> None:
    """``/xp_leaderboard`` — top XP holders of this chat."""
    cid = chat_id(update)
    if cid is None:
        return
    board = GamificationEngine.get_leaderboard(cid)
    if not board:
        await reply(update, "🏆 هنوز کسی در این گفتگو XP نگرفته است. با /daily شروع کنید!")
        return
    lines = ["🏆 جدول XP", "━━━━━━━━━━━━━━━━"]
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    for rank, row in enumerate(board, 1):
        badge = medals.get(rank, f"{rank}.")
        lines.append(
            f"  {badge} کاربر {row['user_id']}: {row['xp']} XP — سطح {row['level']} {row['title']}"
        )
    await reply(update, "\n".join(lines))


async def achievements_cmd(update: Any, context: Any) -> None:
    """``/achievements`` — unlocked achievements of the caller in this chat."""
    uid, cid = user_id(update), chat_id(update)
    if uid is None or cid is None:
        return
    unlocked = GamificationEngine.get_achievements(uid, cid)
    body = GamificationEngine.format_achievements(unlocked)
    await reply(update, f"🏅 دستاوردها ({len(unlocked)})\n━━━━━━━━━━━━━━━━\n{body}")


async def profile_cmd(update: Any, context: Any) -> None:
    """``/profile`` — level, XP, streak and progress to the next level."""
    uid, cid = user_id(update), chat_id(update)
    if uid is None or cid is None:
        return
    p = GamificationEngine.get_profile(uid, cid)
    nxt = f"{p['xp_to_next']} XP تا سطح بعد" if p["xp_to_next"] else "حداکثر سطح 🏆"
    await reply(
        update,
        f"👤 پروفایل\n━━━━━━━━━━━━━━━━\n"
        f"⭐ سطح {p['level']} {p['title']}\n"
        f"✨ XP: {p['xp']} ({nxt})\n"
        f"🔥 استریک: {p['streak']} روز\n"
        f"🏅 دستاوردها: {p['achievement_count']}",
    )
