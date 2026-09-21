"""Telegram surface for anonymous chat and the force-join gate.

Both feature managers need the Telegram ``bot`` object to do anything real
(relay a message, call ``get_chat_member``) and were constructed **without**
one in the old handler module — so anonymous partners could never talk and
the "verify membership" button accepted everybody (audit P0-3 / P0-10).
Here the bot is bound lazily from the handler context on every call.

Plain-text relay for paired users is :func:`route_anon_text`; the gate for
the catch-all message handler is :func:`forcejoin_gate`.  Both return a
bool so the caller knows whether to stop processing.
"""

from __future__ import annotations

from typing import Any

from nexus_ai_agent.config.settings import get_settings
from nexus_ai_agent.features.anonymous_chat import AnonymousChatManager
from nexus_ai_agent.features.force_join import ForceJoinManager
from nexus_ai_agent.observability.logging import get_logger

from ._ptb import bot_of, chat_id, message_of, message_text, reply, user_id

__all__ = [
    "anon_report_cmd",
    "anon_start_cmd",
    "anon_stop_cmd",
    "forcejoin_gate",
    "forcejoin_verify_callback",
    "get_anon_manager",
    "get_forcejoin_manager",
    "reset_community",
    "route_anon_text",
]

logger = get_logger(__name__)

#: Relayed anonymous messages are truncated to this length.
MAX_ANON_TEXT = 4000

_anon = AnonymousChatManager()
_forcejoin = ForceJoinManager()


def get_anon_manager() -> AnonymousChatManager:
    return _anon


def get_forcejoin_manager() -> ForceJoinManager:
    return _forcejoin


def reset_community() -> None:
    """Fresh managers (tests)."""
    global _anon, _forcejoin
    _anon, _forcejoin = AnonymousChatManager(), ForceJoinManager()


def _bind(context: Any) -> None:
    bot = bot_of(context)
    _anon.bind_bot(bot)
    _forcejoin.bind_bot(bot)


# ── Anonymous chat ───────────────────────────────────────────────────


async def anon_start_cmd(update: Any, context: Any) -> None:
    """``/anon_start`` — join the queue; pairs immediately when someone is waiting."""
    uid = user_id(update)
    if uid is None:
        return
    _bind(context)
    if chat_id(update) != uid:
        await reply(update, "🔒 چت ناشناس فقط در گفتگوی خصوصی با ربات کار می‌کند.")
        return
    await reply(update, await _anon.join_queue(uid))


async def anon_stop_cmd(update: Any, context: Any) -> None:
    uid = user_id(update)
    if uid is None:
        return
    _bind(context)
    await reply(update, await _anon.leave_chat(uid))


async def anon_report_cmd(update: Any, context: Any) -> None:
    uid = user_id(update)
    if uid is None:
        return
    _bind(context)
    await reply(update, await _anon.report_user(uid, get_settings().owner_telegram_id))


async def route_anon_text(update: Any, context: Any) -> bool:
    """Relay a plain text message to the anonymous partner, if the sender is paired.

    Returns ``True`` when the message was consumed by the relay.
    """
    uid = user_id(update)
    text = message_text(update)
    if uid is None or not text or text.startswith("/") or chat_id(update) != uid:
        return False
    if _anon.partner_of(uid) is None:
        return False
    _bind(context)
    delivered = await _anon.send_anon_message(uid, text[:MAX_ANON_TEXT])
    if not delivered:
        await reply(
            update, "⚠️ ارسال پیام ناشناس ناموفق بود؛ شاید طرف مقابل ربات را مسدود کرده است."
        )
    return True


# ── Force join ───────────────────────────────────────────────────────


async def forcejoin_verify_callback(update: Any, context: Any) -> None:
    """``forcejoin_verify`` button — re-check membership with the real bot (fail closed)."""
    query = getattr(update, "callback_query", None)
    if query is None:
        return
    await query.answer()
    uid = user_id(update)
    if uid is None:
        return
    _bind(context)
    _forcejoin.invalidate_cache(uid)
    channel = ForceJoinManager.required_channel(chat_id(update))
    if channel is None:
        await query.edit_message_text("ℹ️ عضویت اجباری برای این گفتگو فعال نیست.")
        return
    if await _forcejoin.check_membership(uid, channel):
        await query.edit_message_text("✅ عضویت شما تأیید شد! می‌توانید از ربات استفاده کنید.")
    else:
        await query.edit_message_text(
            f"❌ هنوز عضو {channel} نیستید. ابتدا عضو شوید و دوباره «تأیید عضویت» را بزنید."
        )


async def forcejoin_gate(update: Any, context: Any, command: str = "") -> bool:
    """Block non-members when force-join is enabled for the chat.

    Returns ``True`` when the update was blocked (a join prompt was sent).
    Public commands (``start``, ``help``, ``forcejoin_status``) always pass.
    """
    uid = user_id(update)
    if uid is None or message_of(update) is None:
        return False
    _bind(context)
    cid = chat_id(update)
    try:
        blocked = await _forcejoin.should_block(uid, command, chat_id=cid, bot=bot_of(context))
    except Exception:  # noqa: BLE001 - a broken gate must not take the bot down
        logger.exception("forcejoin_gate_failed", user_id=uid, chat_id=cid)
        return False
    if not blocked:
        return False
    channel = ForceJoinManager.required_channel(cid) or ""
    cfg = ForceJoinManager.get_config(cid) if cid is not None else None
    prompt = "⛔ لطفاً ابتدا در کانال عضو شوید."
    if cfg is not None and cfg.welcome_message:
        prompt = cfg.welcome_message
    await reply(
        update,
        f"{prompt}\n📢 {channel}",
        reply_markup=ForceJoinManager.get_join_keyboard(channel),
    )
    return True
