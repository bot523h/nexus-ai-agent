"""Telegram surface for the referral loop (``/start ref_<CODE>``).

The audit (P0-4) found two ``CommandHandler("start")`` registrations — PTB
only ever runs the first one — and that
:meth:`~nexus_ai_agent.features.referral.ReferralEngine.process_referral`
was never called anywhere, so no referral was ever recorded.  This module
provides the missing step as a hook the *single* ``/start`` handler calls:

    extra = await apply_start_referral(update, context)
    if extra:
        await reply(update, extra)

It records the referral, credits XP to both sides through the gamification
engine and notifies the referrer (fail-safe).  Nothing here builds keyboards
or imports ``telegram``.
"""

from __future__ import annotations

from typing import Any

from nexus_ai_agent.config.settings import get_settings
from nexus_ai_agent.features.gamification import GamificationEngine
from nexus_ai_agent.features.referral import ReferralEngine
from nexus_ai_agent.observability.logging import get_logger

from ._ptb import args, bot_of, chat_id, user_id

__all__ = [
    "apply_start_referral",
    "get_referral_engine",
    "parse_start_param",
    "reset_referral_engine",
]

logger = get_logger(__name__)

REFERRAL_PREFIX = "ref_"

_engine: ReferralEngine | None = None


def get_referral_engine() -> ReferralEngine:
    """Process-wide engine bound to the configured SQLite path (created lazily)."""
    global _engine
    if _engine is None:
        _engine = ReferralEngine(db_path=get_settings().db_path)
    return _engine


def reset_referral_engine() -> None:
    """Drop the cached engine (tests / settings change)."""
    global _engine
    _engine = None


def parse_start_param(raw: list[str]) -> str | None:
    """Return the ``ref_<CODE>`` deep-link parameter from ``/start`` args, if any."""
    if not raw:
        return None
    candidate = raw[0].strip()
    if not candidate.startswith(REFERRAL_PREFIX) or len(candidate) <= len(REFERRAL_PREFIX):
        return None
    if len(candidate) > 64:
        return None
    return candidate


def _referee_message(result: dict[str, Any]) -> str | None:
    if result.get("success"):
        return f"🎁 با کد دعوت وارد شدید! +{result['xp_bonus']} XP برای شما و دعوت‌کننده. 🎉"
    error = result.get("error")
    if error == "self_referral":
        return "😅 نمی‌توانید خودتان را دعوت کنید."
    if error == "code_not_found":
        return "❌ کد دعوت نامعتبر است."
    return None  # already_referred / invalid_code: stay silent, /start continues normally


def _referrer_message(result: dict[str, Any]) -> str:
    lines = [
        f"🎉 یک نفر با لینک دعوت شما وارد شد! +{result['xp_bonus']} XP",
        f"👥 دعوت‌های موفق: {result['total_referrals']}",
    ]
    current = result.get("current_reward")
    if current and current.get("count") == result["total_referrals"]:
        lines.append(f"🏆 سطح جدید: {current['title']} — {current['reward']}")
    nxt = result.get("next_reward")
    if nxt:
        remaining = nxt["count"] - result["total_referrals"]
        lines.append(f"⏭️ تا {nxt['title']}: {remaining} دعوت دیگر")
    return "\n".join(lines)


async def apply_start_referral(update: Any, context: Any) -> str | None:
    """Process a ``/start ref_<CODE>`` deep link.

    Returns an extra line for the referee (or ``None`` when ``/start`` carried
    no usable code).  Never raises: DB or delivery failures are logged so the
    welcome message still goes out.
    """
    uid = user_id(update)
    param = parse_start_param(args(context))
    if uid is None or param is None:
        return None
    try:
        result = get_referral_engine().process_referral(uid, param)
    except Exception:  # noqa: BLE001 - /start must survive a referral failure
        logger.exception("referral_process_failed", user_id=uid)
        return None
    if not result.get("success"):
        return _referee_message(result)

    referrer_id = int(result["referrer_id"])
    xp = int(result.get("xp_bonus", 0))
    cid = chat_id(update) or uid
    try:
        GamificationEngine.add_xp(uid, cid, xp)
        # A private chat's id equals the user's id, so the referrer's XP lands
        # in their own DM scope even though we only know their user id here.
        GamificationEngine.add_xp(referrer_id, referrer_id, xp)
    except Exception:  # noqa: BLE001
        logger.exception("referral_xp_failed", user_id=uid, referrer_id=referrer_id)

    bot = bot_of(context)
    if bot is not None:
        try:
            await bot.send_message(chat_id=referrer_id, text=_referrer_message(result))
        except Exception:  # noqa: BLE001 - referrer may have blocked the bot
            logger.warning("referral_notify_failed", referrer_id=referrer_id)
    logger.info("referral_recorded", referee_id=uid, referrer_id=referrer_id)
    return _referee_message(result)
