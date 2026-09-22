"""Telegram surface for channel and group management (``features/channel_manager.py``).

Commands: ``/post``, ``/schedule``, ``/pin``, ``/ban``, ``/unban``, ``/stats``,
``/welcome``.

What this replaces
------------------
Seven commands in ``bot/handlers.py`` answered with a literal that admitted it
was fake::

    async def post_cmd(...)      -> "✅ Post sent to channel (simulated)."
    async def ban_cmd(...)       -> "🚫 User banned (simulated)."
    async def stats_cmd(...)     -> "📊 Group stats: 150 members, 1.2k messages/day."
    async def welcome_cmd(...)   -> "👋 Welcome message updated."

The ``(simulated)`` replies were the worst kind: the operator believed the post
had gone out, and believed a user had been banned. ``/ban`` was also reachable
by **any** user — it checked nothing, because it did nothing. ``ChannelManager``
itself had no importer except the comment ``# In a real app, this would use
ChannelManager``.

Decisions a reader should be able to find
------------------------------------------
* **One manager per application, bound to the live bot.** :func:`manager_for`
  memoises the instance in ``application.bot_data`` (P0-8: engines used to be
  constructed twice with ``bot_data`` ignored). Fakes can be injected there in
  tests, which is what keeps this layer unit-testable.
* **Owner-gated moderation.** ``/post /schedule /pin /ban /unban /welcome`` are
  owner-only. The repo has no "is admin of this chat" helper, and inventing a
  looser privilege in a wiring PR is out of scope — see ``docs/DECISION_LOG.md``
  D-0009. ``/stats`` stays open: it reads a member count, and the access guard
  (group ``-1``) is what gates the bot overall.
* **No metric we cannot read.** ``/stats`` reports the member count from
  ``get_chat_member_count`` and says plainly that messages-per-day is not
  measured, because no table records per-message counts for a chat
  (``AnalyticsEngine`` counts events it is fed, not Telegram messages).
* **Fail honestly on the API boundary.** ``ChannelManager`` swallows some
  Telegram errors into ``False``; here every engine call is additionally wrapped
  so a ``TelegramError`` (insufficient rights, bad chat id) becomes a Persian
  explanation instead of an unhandled exception in the dispatcher. ``telegram``
  is deliberately not imported — the boundary test freezes it for this package.
* **The hardcoded official channel is not used.** ``ChannelManager.channel_id``
  is a literal id for ``@nexus_ai_official``; a group-admin command must act on
  *the chat the command came from*, so every command here is scoped to
  ``chat_id(update)``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from nexus_ai_agent.features.owner_control import is_owner

if TYPE_CHECKING:  # pragma: no cover - typing only
    # ``features.channel_manager`` imports ``telegram.error`` at module level, so
    # the import stays behind TYPE_CHECKING and is resolved lazily in
    # :func:`manager_for`. The surface package must remain importable without
    # PTB installed (asserted in tests/unit/test_surface_onboarding.py).
    from nexus_ai_agent.features.channel_manager import ChannelManager

from ._ptb import (
    args,
    bot_data,
    bot_of,
    chat_id,
    reply,
    reply_to_message_id,
    reply_to_user_id,
    user_id,
)

__all__ = [
    "ban_cmd",
    "format_ban",
    "format_members",
    "format_posted",
    "format_scheduled",
    "format_welcome_get",
    "format_welcome_set",
    "manager_for",
    "pin_cmd",
    "post_cmd",
    "schedule_cmd",
    "unban_cmd",
    "welcome_cmd",
]

#: Key under which the single :class:`ChannelManager` lives in ``bot_data``.
MANAGER_KEY = "channel_manager"
_SEPARATOR = "━━━━━━━━━━━━━━━━"
#: The exact strings ``bot/handlers.py`` used to answer with; held by
#: ``tests/unit/test_surface_registration.py`` so they cannot come back.
STUB_STRINGS = (
    "✅ Post sent to channel (simulated).",
    "📅 Post scheduled (simulated).",
    "🚫 User banned (simulated).",
    "✅ User unbanned (simulated).",
    "📊 Group stats: 150 members, 1.2k messages/day.",
    "👋 Welcome message updated.",
    "📌 Message pinned.",
)

_DENIED = "⛔ این فرمان فقط برای مالک ربات است."
_NO_BOT = "⚠️ اتصال ربات آماده نیست؛ کمی بعد دوباره امتحان کنید."
_USAGE_POST = "❌ استفاده: /post [--pin] <متن>"
_USAGE_SCHEDULE = (
    "❌ استفاده: /schedule <YYYY-MM-DD HH:MM> <متن>\n"
    "زمان به UTC است؛ مثال: /schedule 2026-10-01 18:30 📣 اعلام نسخه"
)
_USAGE_TARGET = "❌ استفاده: {} <شناسهٔ کاربر> — یا فرمان را روی پیام آن کاربر پاسخ دهید."
_NO_TARGET = "❌ کاربری مشخص نشده‌است."

#: Accepted ``/schedule`` timestamps, split by how Telegram tokenises the line:
#: ``/schedule 2026-10-01 18:30 text`` arrives as three arguments, while the
#: ISO form arrives as one.
_SPACE_FORMATS = ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S")
_ISO_FORMATS = ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S")


def manager_for(context: Any) -> ChannelManager | None:
    """The application's :class:`ChannelManager`, built once and bound to the bot."""
    from nexus_ai_agent.features.channel_manager import ChannelManager  # lazy, see header

    data = bot_data(context)
    bot = bot_of(context)
    existing = data.get(MANAGER_KEY)
    if isinstance(existing, ChannelManager):
        if bot is not None and existing.bot is None:
            existing.bot = bot
        return existing
    if bot is None:
        return None
    manager = ChannelManager(bot)
    data[MANAGER_KEY] = manager
    return manager


# ── parsing and rendering (pure) ───────────────────────────────────────────


def parse_schedule_args(command_args: Sequence[str]) -> tuple[datetime, str] | str:
    """Split ``/schedule`` into a UTC deadline and the text, or an error to show.

    Two shapes are accepted, because both are what people actually type: the
    space-separated ``/schedule 2026-10-01 18:30 <text>`` (date and time are
    separate Telegram arguments) and the single-token ISO form
    ``/schedule 2026-10-01T18:30 <text>``.
    """
    tokens = list(command_args)
    parsed: datetime | None = None
    if tokens and "T" in tokens[0]:
        rest = tokens[1:]
        candidate = tokens[0]
        templates = _ISO_FORMATS
    else:
        if len(tokens) < 3:
            return _USAGE_SCHEDULE
        rest = tokens[2:]
        candidate = f"{tokens[0]} {tokens[1]}"
        templates = _SPACE_FORMATS
    for template in templates:
        try:
            parsed = datetime.strptime(candidate, template)
        except ValueError:
            continue
        break
    if parsed is None:
        return f"❌ قالب زمان شناخته نشد: {candidate}\n{_USAGE_SCHEDULE}"
    when = parsed.replace(tzinfo=timezone.utc)
    if when <= datetime.now(timezone.utc):
        return "❌ زمان گذشته را نمی‌توان زمان‌بندی کرد؛ یک زمان آینده (UTC) بدهید."
    text = " ".join(rest).strip()
    if not text:
        return _USAGE_SCHEDULE
    return when, text


def parse_target_id(command_args: Sequence[str], update: Any, *, command: str) -> int | str:
    """The user a moderation command acts on: an argument, or the quoted sender."""
    if command_args:
        try:
            identifier = int(command_args[0])
        except ValueError:
            return _USAGE_TARGET.format(command)
        if identifier <= 0:
            return _USAGE_TARGET.format(command)
        return identifier
    quoted = reply_to_user_id(update)
    if quoted is None:
        return _NO_TARGET
    return quoted


def format_posted(chat: int, text: str, *, pinned: bool) -> str:
    """Render a successful ``/post``."""
    tail = " و سنجاق شد" if pinned else ""
    return f"✅ پیام در گفتگوی {chat} ارسال شد{tail} ({len(text)} نویسه)."


def format_scheduled(schedule_id: int, when: datetime) -> str:
    """Render a successful ``/schedule``; the id is the DB row's own id."""
    return f"📅 زمان‌بندی ثبت شد\n{_SEPARATOR}\nشناسه: {schedule_id}\nارسال در: {when.isoformat()}"


def format_ban(*, verb: str, ok: bool, target: int) -> str:
    """Render a ban/unban result; ``ok`` is what the engine actually achieved."""
    if not ok:
        return (
            f"⚠️ {verb} انجام نشد (کاربر {target}). معمولاً یعنی ربات در این گفتگو "
            "دسترسی مدیریتی ندارد یا عضو محدودیت دارد."
        )
    return f"✅ {verb} انجام شد (کاربر {target})."


def format_members(count: int) -> str:
    """Render ``/stats`` — a real number, and an explicit non-number."""
    return (
        "📊 وضعیت گفتگو\n"
        f"{_SEPARATOR}\n"
        f"اعضا: {count}\n"
        "ℹ️ «پیام در روز» در این نسخه اندازه‌گیری نمی‌شود؛ عدد بالا شمارش زندهٔ "
        "اعضاست، نه برآورد."
    )


def format_welcome_get(current: str) -> str:
    """Render ``/welcome`` with no arguments."""
    if not current:
        return "👋 پیام خوش‌آمدی تنظیم نشده‌است.\n\nاستفاده: /welcome <متن> ({name} = نام عضو)"
    return f"👋 پیام خوش‌آمدی فعلی:\n{_SEPARATOR}\n{current}"


def format_welcome_set(text: str) -> str:
    """Render a stored welcome message."""
    return f"✅ پیام خوش‌آمدی ذخیره شد ({len(text)} نویسه).\n{_SEPARATOR}\n{text}"


# ── commands ──────────────────────────────────────────────────────────────


async def post_cmd(update: Any, context: Any) -> None:
    """``/post [--pin] <text>`` — send to this chat through the manager."""
    chat, uid = chat_id(update), user_id(update)
    if chat is None or uid is None:
        return
    if not is_owner(uid):
        await reply(update, _DENIED)
        return
    command_args = args(context)
    pinned = bool(command_args) and command_args[0] == "--pin"
    text = " ".join(command_args[1:] if pinned else command_args).strip()
    if not text:
        await reply(update, _USAGE_POST)
        return
    manager = manager_for(context)
    if manager is None:
        await reply(update, _NO_BOT)
        return
    try:
        await manager.post_to_channel(chat, text, pin=pinned)
    except Exception as exc:  # noqa: BLE001 - Telegram errors become user text
        await reply(update, f"⚠️ ارسال ناموفق: {exc}")
        return
    await reply(update, format_posted(chat, text, pinned=pinned))


async def pin_cmd(update: Any, context: Any) -> None:
    """``/pin [message_id]`` — pin the quoted message, or the given id."""
    chat, uid = chat_id(update), user_id(update)
    if chat is None or uid is None:
        return
    if not is_owner(uid):
        await reply(update, _DENIED)
        return
    command_args = args(context)
    if command_args:
        try:
            message_id: int | None = int(command_args[0])
        except ValueError:
            await reply(update, "❌ شناسهٔ پیام باید عدد باشد. مثال: /pin 421")
            return
    else:
        message_id = reply_to_message_id(update)
    if message_id is None:
        await reply(update, "❌ پیامی که پاسخ داده‌اید پیدا نشد؛ شناسه را بدهید: /pin 421")
        return
    manager = manager_for(context)
    if manager is None:
        await reply(update, _NO_BOT)
        return
    try:
        await manager.pin_message(chat, message_id)
    except Exception as exc:  # noqa: BLE001 - Telegram errors become user text
        await reply(update, f"⚠️ سنجاق کردن ناموفق: {exc}")
        return
    await reply(update, f"📌 پیام {message_id} سنجاق شد.")


async def schedule_cmd(update: Any, context: Any) -> None:
    """``/schedule <when> <text>`` — persist a :class:`ChannelSchedule` row."""
    chat, uid = chat_id(update), user_id(update)
    if chat is None or uid is None:
        return
    if not is_owner(uid):
        await reply(update, _DENIED)
        return
    parsed = parse_schedule_args(args(context))
    if isinstance(parsed, str):
        await reply(update, parsed)
        return
    when, text = parsed
    manager = manager_for(context)
    if manager is None:
        await reply(update, _NO_BOT)
        return
    try:
        schedule_id = await manager.schedule_post(chat, text, when)
    except Exception as exc:  # noqa: BLE001 - Telegram errors become user text
        await reply(update, f"⚠️ زمان‌بندی ثبت نشد: {exc}")
        return
    await reply(update, format_scheduled(int(schedule_id), when))


async def ban_cmd(update: Any, context: Any) -> None:
    """``/ban [user_id]`` — real ``ban_chat_member`` on the current chat."""
    chat, uid = chat_id(update), user_id(update)
    if chat is None or uid is None:
        return
    if not is_owner(uid):
        await reply(update, _DENIED)
        return
    target = parse_target_id(args(context), update, command="/ban")
    if isinstance(target, str):
        await reply(update, target)
        return
    manager = manager_for(context)
    if manager is None:
        await reply(update, _NO_BOT)
        return
    reason = " ".join(args(context)[1:]).strip()
    ok = await manager.ban_user(chat, target, reason=reason)
    await reply(update, format_ban(verb="بن", ok=bool(ok), target=target))


async def unban_cmd(update: Any, context: Any) -> None:
    """``/unban [user_id]`` — lift the ban."""
    chat, uid = chat_id(update), user_id(update)
    if chat is None or uid is None:
        return
    if not is_owner(uid):
        await reply(update, _DENIED)
        return
    target = parse_target_id(args(context), update, command="/unban")
    if isinstance(target, str):
        await reply(update, target)
        return
    manager = manager_for(context)
    if manager is None:
        await reply(update, _NO_BOT)
        return
    ok = await manager.unban_user(chat, target)
    await reply(update, format_ban(verb="رفع بن", ok=bool(ok), target=target))


async def stats_cmd(update: Any, context: Any) -> None:
    """``/stats`` — the live member count of this chat."""
    chat = chat_id(update)
    if chat is None:
        return
    manager = manager_for(context)
    if manager is None:
        await reply(update, _NO_BOT)
        return
    try:
        count = await manager.get_members_count(chat)
    except Exception as exc:  # noqa: BLE001 - Telegram errors become user text
        await reply(update, f"⚠️ شمارش اعضا ناموفق: {exc}")
        return
    await reply(update, format_members(int(count)))


async def welcome_cmd(update: Any, context: Any) -> None:
    """``/welcome [text]`` — read or write the chat's welcome message."""
    chat, uid = chat_id(update), user_id(update)
    if chat is None or uid is None:
        return
    if not is_owner(uid):
        await reply(update, _DENIED)
        return
    manager = manager_for(context)
    if manager is None:
        await reply(update, _NO_BOT)
        return
    text = " ".join(args(context)).strip()
    if not text:
        current = await asyncio.to_thread(manager.get_welcome_message, chat)
        await reply(update, format_welcome_get(str(current)))
        return
    await asyncio.to_thread(manager.set_welcome_message, chat, text)
    await reply(update, format_welcome_set(text))
