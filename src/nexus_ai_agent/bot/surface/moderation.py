"""Telegram surface for smart moderation.

Commands: ``/mod_config``, ``/warn``, ``/mute``, ``/unmute``, ``/reputation`` and
the message ingress :func:`moderate_message`.

Replaces the canned strings ``bot/handlers.py`` shipped for these commands::

    async def mod_config_cmd(...)     -> "🛡️ Moderation rules updated."
    async def mod_warn_cmd(...)       -> "⚠️ User warned (1/3)."
    async def mod_mute_cmd(...)       -> "🔇 User muted for 10 minutes."
    async def mod_unmute_cmd(...)     -> "🔊 User unmuted."
    async def mod_reputation_cmd(...) -> "👤 User Reputation: 85/100 (Good)."

with the real :class:`~nexus_ai_agent.features.moderation.ModerationEngine`.
None of the old stubs checked who was calling, and ``/mod_on`` stored a
configuration that nothing ever consulted (audit finding A-03, 2026-10-15).

Design
------
* **Framework-free** (see :mod:`._ptb`): no ``telegram`` import, so the whole
  surface is unit-testable with ``SimpleNamespace`` fakes. Telegram-side
  enforcement (``restrict_chat_member``) goes through the duck-typed bot with a
  plain-dict permissions payload — python-telegram-bot JSON-encodes dict
  parameters verbatim, which is exactly what the Bot API expects.
* **Owner-gated.** The bot is deny-by-default; moderation actions are
  restricted to the owner exactly like ``/mod_on`` / ``/mod_off`` and
  ``/ban``. A non-owner gets the same denial string as every other owner
  command.
* **The event loop is never blocked.** Every engine call is synchronous SQLite
  CRUD, so it is off-loaded with :func:`asyncio.to_thread`.
* **Enforcement is reported, never pretended.** A reply says "muted on
  Telegram" only when ``restrict_chat_member`` succeeded; otherwise it says the
  mute is recorded but the bot lacks admin rights.
* **Formatting is separated from I/O**: the ``format_*`` helpers are pure.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from nexus_ai_agent.features.moderation import ModerationEngine
from nexus_ai_agent.features.owner_control import is_owner

from ._ptb import (
    args,
    bot_of,
    chat_id,
    message_content,
    message_of,
    reply,
    user_id,
)
from .channel_management import parse_target_id

__all__ = [
    "evaluate_message",
    "format_config",
    "format_muted",
    "format_reputation",
    "format_unmuted",
    "format_warned",
    "mod_config_cmd",
    "moderate_message",
    "mute_cmd",
    "parse_config_args",
    "reputation_cmd",
    "unmute_cmd",
    "warn_cmd",
]

logger = logging.getLogger(__name__)

_DENIED = "⛔ Access denied"
_SEPARATOR = "━━━━━━━━━━━━━━━━"
_FLAGS = ("anti_spam", "anti_flood", "link_filter", "profanity_filter")
_USAGE_CONFIG = (
    "🛡️ استفاده:\n"
    "/mod_config — نمایش تنظیمات\n"
    "/mod_config <anti_spam|anti_flood|link_filter|profanity_filter> <on|off>\n"
    "/mod_config max_warnings <1-10>\n"
    "/mod_config mute_minutes <1-10080>"
)
#: Bot-API ``ChatPermissions`` payload that removes every send right.
_NO_PERMISSIONS: dict[str, bool] = {
    "can_send_messages": False,
    "can_send_audios": False,
    "can_send_documents": False,
    "can_send_photos": False,
    "can_send_videos": False,
    "can_send_video_notes": False,
    "can_send_voice_notes": False,
    "can_send_polls": False,
    "can_send_other_messages": False,
    "can_add_web_page_previews": False,
}
#: Restores the group defaults (Telegram re-applies the chat's own permissions).
_DEFAULT_PERMISSIONS: dict[str, bool] = {key: True for key in _NO_PERMISSIONS}
_REASON_LABELS = {
    "spam": "اسپم",
    "flood": "ارسال پیاپی",
    "links": "لینک",
    "profanity": "الفاظ نامناسب",
    "muted": "کاربر بی‌صدا شده",
}


# ── pure rendering / parsing ────────────────────────────────────────────────


def format_config(cfg: Any | None, chat: int) -> str:
    """Render a chat's moderation configuration (or its absence)."""
    if cfg is None:
        return f"🛡️ مدیریت هوشمند برای گفتگوی {chat} فعال نیست. با /mod_on فعال کنید."
    on = "✅"
    off = "❌"
    return (
        f"🛡️ تنظیمات مدیریت گفتگوی {chat}\n{_SEPARATOR}\n"
        f"anti_spam: {on if cfg.anti_spam else off}\n"
        f"anti_flood: {on if cfg.anti_flood else off}\n"
        f"link_filter: {on if cfg.link_filter else off}\n"
        f"profanity_filter: {on if cfg.profanity_filter else off}\n"
        f"max_warnings: {cfg.max_warnings}\n"
        f"mute_minutes: {cfg.mute_duration_minutes}"
    )


def parse_config_args(command_args: list[str]) -> dict[str, Any] | str:
    """Translate ``/mod_config`` arguments into ``set_config`` keyword arguments.

    Returns the usage text on any malformed input; an empty argument list
    means "show the current configuration" and yields an empty dict.
    """
    if not command_args:
        return {}
    if len(command_args) != 2:
        return _USAGE_CONFIG
    key, raw = command_args[0].lower(), command_args[1].lower()
    if key in _FLAGS:
        if raw in {"on", "true", "1", "yes"}:
            return {key: True}
        if raw in {"off", "false", "0", "no"}:
            return {key: False}
        return _USAGE_CONFIG
    if key in {"max_warnings", "mute_minutes"}:
        try:
            value = int(raw)
        except ValueError:
            return _USAGE_CONFIG
        if key == "max_warnings":
            if not 1 <= value <= 10:
                return _USAGE_CONFIG
            return {"max_warnings": value}
        if not 1 <= value <= 10080:
            return _USAGE_CONFIG
        return {"mute_duration_minutes": value}
    return _USAGE_CONFIG


def format_warned(target: int, warnings: int, max_warnings: int, *, muted: bool) -> str:
    """Render the outcome of ``/warn`` (and of an automatic warning)."""
    head = f"⚠️ کاربر {target} اخطار گرفت ({warnings}/{max_warnings})."
    if muted:
        return head + "\n🔇 سقف اخطارها پر شد؛ کاربر بی‌صدا شد."
    return head


def format_muted(target: int, minutes: int, *, enforced: bool) -> str:
    """Render ``/mute``; ``enforced`` is whether Telegram applied the restriction."""
    head = f"🔇 کاربر {target} به مدت {minutes} دقیقه بی‌صدا شد."
    if enforced:
        return head
    return (
        head
        + "\n⚠️ محدودیت در تلگرام اعمال نشد (ربات ادمین نیست)؛"
        + " پیام‌های او توسط ربات نادیده گرفته می‌شود."
    )


def format_unmuted(target: int, *, enforced: bool) -> str:
    """Render ``/unmute``."""
    head = f"🔊 بی‌صدایی کاربر {target} برداشته شد."
    if enforced:
        return head
    return head + "\n⚠️ محدودیت تلگرام تغییر نکرد (ربات ادمین نیست)."


def format_reputation(target: int, rep: Any | None) -> str:
    """Render ``/reputation`` from the engine's :class:`UserReputation` row."""
    if rep is None:
        return f"👤 کاربر {target}: هنوز سابقه‌ای در این گفتگو ندارد (اعتبار 0، اخطار 0)."
    status = "🔇 بی‌صدا" if rep.is_muted else "✅ فعال"
    return (
        f"👤 کاربر {target}\n{_SEPARATOR}\n"
        f"اعتبار: {rep.reputation}\n"
        f"اخطارها: {rep.warnings}\n"
        f"وضعیت: {status}"
    )


def format_violation(reasons: list[str], warnings: int, max_warnings: int, action: str) -> str:
    """Render the automatic reply of the ingress for a violating message."""
    labels = "، ".join(_REASON_LABELS.get(reason, reason) for reason in reasons) or "تخلف"
    if action == "mute":
        return f"🔇 پیام به‌دلیل {labels} حذف و کاربر بی‌صدا شد ({warnings}/{max_warnings})."
    return f"⚠️ اخطار به‌دلیل {labels} ({warnings}/{max_warnings})."


# ── Telegram enforcement (duck-typed bot) ───────────────────────────────────


async def _restrict(bot: Any, chat: int, target: int, *, minutes: int | None) -> bool:
    """Apply (``minutes`` given) or lift (``None``) a Telegram-side mute. Best effort."""
    restrict = getattr(bot, "restrict_chat_member", None)
    if restrict is None:
        return False
    try:
        if minutes is None:
            await restrict(chat_id=chat, user_id=target, permissions=_DEFAULT_PERMISSIONS)
        else:
            until = datetime.now(timezone.utc) + timedelta(minutes=minutes)
            await restrict(
                chat_id=chat, user_id=target, permissions=_NO_PERMISSIONS, until_date=until
            )
    except Exception as exc:  # noqa: BLE001 — a failed API call is reported, not raised
        logger.info("moderation: restrict_chat_member failed", extra={"error": str(exc)})
        return False
    return True


async def _announce(context: Any, chat: int, text: str) -> None:
    """Post a moderation notice to the chat (the offending message is already gone)."""
    send = getattr(bot_of(context), "send_message", None)
    if send is None:
        return
    try:
        await send(chat_id=chat, text=text)
    except Exception as exc:  # noqa: BLE001
        logger.info("moderation: notice failed", extra={"error": str(exc)})


async def _delete_message(update: Any) -> bool:
    message = message_of(update)
    delete = getattr(message, "delete", None)
    if delete is None:
        return False
    try:
        await delete()
    except Exception as exc:  # noqa: BLE001
        logger.info("moderation: delete failed", extra={"error": str(exc)})
        return False
    return True


# ── commands ────────────────────────────────────────────────────────────────


async def _owner_and_chat(update: Any) -> tuple[int, int] | None:
    chat, uid = chat_id(update), user_id(update)
    if chat is None or uid is None:
        return None
    if not is_owner(uid):
        await reply(update, _DENIED)
        return None
    return chat, uid


async def mod_config_cmd(update: Any, context: Any) -> None:
    """``/mod_config`` — show or change the chat's moderation rules (owner only)."""
    ids = await _owner_and_chat(update)
    if ids is None:
        return
    chat, _ = ids
    parsed = parse_config_args(args(context))
    if isinstance(parsed, str):
        await reply(update, parsed)
        return
    cfg: Any
    if parsed:
        cfg = await asyncio.to_thread(ModerationEngine.set_config, chat, **parsed)
    else:
        cfg = await asyncio.to_thread(ModerationEngine.get_config, chat)
    await reply(update, format_config(cfg, chat))


async def warn_cmd(update: Any, context: Any) -> None:
    """``/warn [user_id]`` — record a warning; auto-mute at the configured ceiling."""
    ids = await _owner_and_chat(update)
    if ids is None:
        return
    chat, _ = ids
    target = parse_target_id(args(context), update, command="/warn")
    if isinstance(target, str):
        await reply(update, target)
        return
    cfg = await asyncio.to_thread(ModerationEngine.get_config, chat)
    max_warnings = cfg.max_warnings if cfg is not None else 3
    mute_minutes = cfg.mute_duration_minutes if cfg is not None else 30
    warnings = await asyncio.to_thread(ModerationEngine.add_warning, target, chat, "manual")
    muted = warnings >= max_warnings
    if muted:
        await asyncio.to_thread(ModerationEngine.mute_user, target, chat, mute_minutes)
        await _restrict(bot_of(context), chat, target, minutes=mute_minutes)
    await reply(update, format_warned(target, warnings, max_warnings, muted=muted))


async def mute_cmd(update: Any, context: Any) -> None:
    """``/mute [user_id] [minutes]`` — mute in the engine and (if admin) on Telegram."""
    ids = await _owner_and_chat(update)
    if ids is None:
        return
    chat, _ = ids
    command_args = args(context)
    minutes_arg = command_args[1:2]
    target = parse_target_id(command_args[:1], update, command="/mute")
    if isinstance(target, str):
        await reply(update, target)
        return
    cfg = await asyncio.to_thread(ModerationEngine.get_config, chat)
    minutes = cfg.mute_duration_minutes if cfg is not None else 30
    if minutes_arg:
        try:
            minutes = int(minutes_arg[0])
        except ValueError:
            await reply(update, "استفاده: /mute <user_id> [دقیقه]")
            return
        if not 1 <= minutes <= 10080:
            await reply(update, "مدت باید بین ۱ تا ۱۰۰۸۰ دقیقه باشد.")
            return
    await asyncio.to_thread(ModerationEngine.mute_user, target, chat, minutes)
    enforced = await _restrict(bot_of(context), chat, target, minutes=minutes)
    await reply(update, format_muted(target, minutes, enforced=enforced))


async def unmute_cmd(update: Any, context: Any) -> None:
    """``/unmute [user_id]`` — lift the mute in the engine and (if admin) on Telegram."""
    ids = await _owner_and_chat(update)
    if ids is None:
        return
    chat, _ = ids
    target = parse_target_id(args(context), update, command="/unmute")
    if isinstance(target, str):
        await reply(update, target)
        return
    await asyncio.to_thread(ModerationEngine.unmute_user, target, chat)
    enforced = await _restrict(bot_of(context), chat, target, minutes=None)
    await reply(update, format_unmuted(target, enforced=enforced))


async def reputation_cmd(update: Any, context: Any) -> None:
    """``/reputation [user_id]`` — the caller's (or the target's) standing in this chat."""
    chat, uid = chat_id(update), user_id(update)
    if chat is None or uid is None:
        return
    command_args = args(context)
    if command_args:
        if not is_owner(uid):
            await reply(update, _DENIED)
            return
        target = parse_target_id(command_args, update, command="/reputation")
        if isinstance(target, str):
            await reply(update, target)
            return
    else:
        target = uid
    rep = await asyncio.to_thread(ModerationEngine.get_reputation, target, chat)
    await reply(update, format_reputation(target, rep))


# ── ingress ─────────────────────────────────────────────────────────────────


async def evaluate_message(update: Any, context: Any) -> dict[str, Any]:
    """Run the engine over an incoming message and apply the verdict.

    Returns the engine's verdict dict (``allowed``/``reasons``/``action``) so
    the registering layer can stop propagation for disallowed messages. The
    owner is never moderated; private chats and messages without text are
    allowed without touching the database.

    The text is ``message.text`` **or the media caption**: Telegram puts the
    text of a photo in ``caption``, so a captioned photo with a spam link
    reported an empty text and was waved through (review finding,
    2026-10-17). Bare media — no text, no caption — stays on the existing
    early-return path.
    """
    chat, uid = chat_id(update), user_id(update)
    text = message_content(update)
    allow: dict[str, Any] = {"allowed": True, "reasons": [], "action": "allow"}
    if chat is None or uid is None or not text or chat > 0 or is_owner(uid):
        return allow
    cfg = await asyncio.to_thread(ModerationEngine.get_config, chat)
    if cfg is None:
        return allow
    verdict = await asyncio.to_thread(ModerationEngine.check_message, uid, chat, text)
    if verdict.get("allowed") is True:
        return verdict
    action = str(verdict.get("action"))
    await _delete_message(update)
    if action == "mute":
        await _restrict(bot_of(context), chat, uid, minutes=cfg.mute_duration_minutes)
    if action in {"warn", "mute"}:
        rep = await asyncio.to_thread(ModerationEngine.get_reputation, uid, chat)
        warnings = rep.warnings if rep is not None else 0
        notice = format_violation(verdict.get("reasons", []), warnings, cfg.max_warnings, action)
        await _announce(context, chat, notice)
    return verdict


async def moderate_message(update: Any, context: Any) -> bool:
    """``MessageHandler`` target: ``True`` when the message may continue downstream."""
    verdict = await evaluate_message(update, context)
    return verdict.get("allowed") is True
