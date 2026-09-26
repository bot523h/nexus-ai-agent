"""Telegram surface for smart moderation (``features/moderation.py``).

Commands: ``/mod_config``, ``/warn``, ``/mute``, ``/unmute``, ``/reputation``
(the handler symbols keep the ``mod_`` prefix of the stubs they replaced).

What this replaces
------------------
Five commands in ``bot/handlers.py`` answered with a fixed sentence and
performed **no action at all**::

    async def mod_config_cmd(...)     -> "🛡️ Moderation rules updated."
    async def mod_warn_cmd(...)       -> "⚠️ User warned (1/3)."
    async def mod_mute_cmd(...)       -> "🔇 User muted for 10 minutes."
    async def mod_unmute_cmd(...)     -> "🔊 User unmuted."
    async def mod_reputation_cmd(...) -> "👤 User Reputation: 85/100 (Good)."

This is the most damaging shape of stub in the repository's taxonomy: an
operator read *"User muted for 10 minutes"*, believed the abuse had stopped and
moved on, while the offender kept posting. ``"⚠️ User warned (1/3)"`` even
invented a counter — no row was written, so the second warning also said 1/3.
``"85/100 (Good)"`` reported a score from a scale that does not exist: the
schema stores an unbounded integer ``reputation``, not a percentage.

Meanwhile ``ModerationEngine.add_warning`` / ``mute_user`` / ``unmute_user`` /
``get_reputation`` / ``clear_warnings`` were fully implemented and had **no
importer anywhere in ``src/``** — the same "dead engine behind a live stub"
defect recorded for the advertisement engine and the channel manager in
``docs/DECISION_LOG.md`` D-0009.

Decisions a reader should be able to find
-----------------------------------------
* **Owner-gated writes.** ``/mod_config /warn /mute /unmute`` are
  owner-only, matching ``surface/channel_management.py``: the repository still
  has no "is admin of *this* chat" helper, and inventing a looser privilege in
  a wiring change is out of scope. ``/reputation`` is a read and stays
  open — it exposes counters about the *caller's own* chat, and the global
  access guard (handler group ``-1``) is what gates the bot overall.
* **Chat-scoped, always.** Every engine call carries ``chat_id(update)``;
  reputation and warnings are stored per ``(user, chat)`` and must not leak
  across groups.
* **The event loop is never blocked.** Every engine call is synchronous SQLite
  CRUD, so it is off-loaded with :func:`asyncio.to_thread`, the convention the
  P0 wiring batch adopted.
* **No number we cannot read.** ``format_reputation`` prints the stored
  integer, the warning count and the live mute deadline — never a fabricated
  ``85/100``. Only ``max_warnings`` comes from the chat's config, so "3/3" is
  the chat's real threshold.
* **Formatting is separated from I/O.** The ``format_*``/``parse_*`` helpers
  are pure functions over engine payloads, which is what makes this layer
  testable without a bot, a database or an event loop.
* **``telegram`` is never imported** (see :mod:`._ptb`) — the frozen import
  boundary owns this package.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

from nexus_ai_agent.core.timeutil import as_naive_utc, utcnow
from nexus_ai_agent.features.moderation import ModerationEngine
from nexus_ai_agent.features.owner_control import is_owner

from ._ptb import args, chat_id, reply, reply_to_user_id, user_id

__all__ = [
    "format_config",
    "format_muted",
    "format_reputation",
    "format_unmuted",
    "format_warned",
    "mod_config_cmd",
    "mod_mute_cmd",
    "mod_reputation_cmd",
    "mod_unmute_cmd",
    "mod_warn_cmd",
    "parse_duration_minutes",
    "parse_switches",
    "parse_target_user",
]

_SEPARATOR = "━━━━━━━━━━━━━━━━"

#: The exact strings ``bot/handlers.py`` used to answer with; they must never
#: come back, and ``tests/unit/test_surface_registration.py`` holds them.
STUB_STRINGS = (
    "🛡️ Moderation rules updated.",
    "⚠️ User warned (1/3).",
    "🔇 User muted for 10 minutes.",
    "🔊 User unmuted.",
    "👤 User Reputation: 85/100 (Good).",
)

_DENIED = "⛔ این فرمان فقط برای مالک ربات است."
_NO_CHAT = "⚠️ این فرمان باید داخل یک گفتگو اجرا شود."
_NO_TARGET = (
    "❌ کاربری مشخص نشده‌است — فرمان را روی پیام آن کاربر پاسخ دهید یا شناسهٔ عددی او را بنویسید."
)
_USAGE_CONFIG = (
    "❌ استفاده: /mod_config <کلید>=<مقدار> …\n"
    "کلیدهای سوییچ: spam | flood | links | profanity (on/off)\n"
    "کلیدهای عددی: warnings=<۱..۲۰> | mute=<۱..۱۴۴۰ دقیقه>\n"
    "مثال: /mod_config links=off warnings=5 mute=30"
)

#: ``/mod_config`` switch name → the :meth:`ModerationEngine.set_config` kwarg.
_SWITCHES: dict[str, str] = {
    "spam": "anti_spam",
    "anti_spam": "anti_spam",
    "flood": "anti_flood",
    "anti_flood": "anti_flood",
    "links": "link_filter",
    "link_filter": "link_filter",
    "profanity": "profanity_filter",
    "profanity_filter": "profanity_filter",
}
_TRUE = {"on", "true", "1", "yes", "enable", "enabled", "روشن", "فعال"}
_FALSE = {"off", "false", "0", "no", "disable", "disabled", "خاموش", "غیرفعال"}

#: Bounds that keep an operator typo from pinning a user forever.
MIN_WARNINGS, MAX_WARNINGS = 1, 20
MIN_MUTE_MINUTES, MAX_MUTE_MINUTES = 1, 1440  # one minute … one day
#: ``/mod_mute`` default when no duration is given — the engine's own default.
DEFAULT_MUTE_MINUTES = 30


# ── parsing (pure) ─────────────────────────────────────────────────────────


def parse_switches(command_args: Sequence[str]) -> dict[str, Any] | str:
    """Parse ``key=value`` pairs into :meth:`ModerationEngine.set_config` kwargs.

    Returns the kwargs mapping, or the error sentence to show the operator.
    An empty argument list is an error rather than a silent no-op: the old stub
    claimed "rules updated" for *every* invocation, including an empty one.
    """
    if not command_args:
        return _USAGE_CONFIG

    parsed: dict[str, Any] = {}
    for raw in command_args:
        key, separator, value = raw.partition("=")
        key = key.strip().lower().lstrip("-")
        value = value.strip().lower()
        if not separator or not value:
            return f"❌ «{raw}» قالب درستی ندارد.\n{_USAGE_CONFIG}"

        if key in _SWITCHES:
            if value in _TRUE:
                parsed[_SWITCHES[key]] = True
            elif value in _FALSE:
                parsed[_SWITCHES[key]] = False
            else:
                return f"❌ مقدار «{value}» برای «{key}» معتبر نیست (on/off).\n{_USAGE_CONFIG}"
            continue

        if key in ("warnings", "max_warnings"):
            bounded = _bounded_int(value, MIN_WARNINGS, MAX_WARNINGS)
            if bounded is None:
                return f"❌ تعداد اخطار باید عددی بین {MIN_WARNINGS} و {MAX_WARNINGS} باشد."
            parsed["max_warnings"] = bounded
            continue

        if key in ("mute", "mute_minutes", "mute_duration_minutes"):
            bounded = _bounded_int(value, MIN_MUTE_MINUTES, MAX_MUTE_MINUTES)
            if bounded is None:
                return (
                    f"❌ مدت سکوت باید عددی بین {MIN_MUTE_MINUTES} و {MAX_MUTE_MINUTES} دقیقه باشد."
                )
            parsed["mute_duration_minutes"] = bounded
            continue

        return f"❌ کلید ناشناخته: «{key}».\n{_USAGE_CONFIG}"

    return parsed


def _bounded_int(value: str, low: int, high: int) -> int | None:
    try:
        number = int(value)
    except ValueError:
        return None
    return number if low <= number <= high else None


def parse_target_user(update: Any, command_args: Sequence[str]) -> int | None:
    """Resolve the user a moderation command acts on.

    A quoted message wins (the natural Telegram gesture); otherwise the first
    argument must be a numeric user id. ``None`` means "no target" and the
    caller refuses — a moderation command must never silently act on the
    caller.
    """
    quoted = reply_to_user_id(update)
    if quoted is not None:
        return quoted
    for raw in command_args:
        candidate = raw.strip().lstrip("@")
        if candidate.isdigit():
            return int(candidate)
    return None


def parse_duration_minutes(command_args: Sequence[str], *, target_from_reply: bool) -> int | str:
    """Return the ``/mute`` duration in minutes, or the error sentence to show.

    Which number is the duration depends on how the target was named, so the
    caller has to say:

    * replying to a message — ``/mute 45`` — the *first* number is the duration;
    * naming an id — ``/mute 12345 45`` — the *second* number is.

    Getting this wrong is not cosmetic: an earlier draft of this function took
    ``numbers[-1]`` only when two numbers were present, so the reply-based
    ``/mute 45`` silently fell back to the 30-minute default and muted the user
    for a third longer than the operator asked. A moderation command that
    quietly disregards its own argument is the same class of defect as the stub
    this module replaced, so the ambiguity is resolved by the caller rather
    than guessed at here.
    """
    numbers = [raw.strip() for raw in command_args if raw.strip().lstrip("@").isdigit()]
    index = 0 if target_from_reply else 1
    if len(numbers) <= index:
        return DEFAULT_MUTE_MINUTES
    bounded = _bounded_int(numbers[index], MIN_MUTE_MINUTES, MAX_MUTE_MINUTES)
    if bounded is None:
        return f"❌ مدت سکوت باید بین {MIN_MUTE_MINUTES} و {MAX_MUTE_MINUTES} دقیقه باشد."
    return bounded


# ── pure rendering ─────────────────────────────────────────────────────────


def _onoff(value: bool) -> str:
    return "روشن ✅" if value else "خاموش ⛔"


def format_config(config: Any) -> str:
    """Render the persisted :class:`ModerationConfig` row — never a claim."""
    if config is None:
        return "🛡️ برای این گفتگو هنوز تنظیماتی ذخیره نشده است. با /mod_on فعالش کنید."
    return "\n".join(
        [
            "🛡️ تنظیمات نظارت این گفتگو",
            _SEPARATOR,
            f"  ضداسپم: {_onoff(bool(config.anti_spam))}",
            f"  ضدسیل پیام: {_onoff(bool(config.anti_flood))}",
            f"  فیلتر لینک: {_onoff(bool(config.link_filter))}",
            f"  فیلتر ناسزا: {_onoff(bool(config.profanity_filter))}",
            f"  سقف اخطار: {int(config.max_warnings)}",
            f"  مدت سکوت: {int(config.mute_duration_minutes)} دقیقه",
        ]
    )


def format_warned(target: int, warnings: int, max_warnings: int, muted: bool) -> str:
    """Render the *persisted* warning count — the stub always said ``1/3``."""
    head = f"⚠️ اخطار ثبت شد برای کاربر {target}: {warnings}/{max_warnings}"
    if muted:
        return f"{head}\n🔇 سقف اخطار پر شد؛ کاربر ساکت شد."
    return head


def format_muted(target: int, minutes: int) -> str:
    return f"🔇 کاربر {target} برای {minutes} دقیقه ساکت شد (ثبت‌شده در پایگاه‌داده)."


def format_unmuted(target: int, was_muted: bool) -> str:
    if not was_muted:
        return f"🔊 کاربر {target} ساکت نبود؛ تغییری لازم نبود."
    return f"🔊 سکوت کاربر {target} برداشته شد."


def format_reputation(target: int, reputation: Any) -> str:
    """Render the stored reputation row. No invented ``/100`` scale."""
    if reputation is None:
        return f"👤 برای کاربر {target} در این گفتگو هنوز سابقه‌ای ثبت نشده است."
    lines = [
        f"👤 سابقهٔ کاربر {target}",
        _SEPARATOR,
        f"  امتیاز: {int(reputation.reputation)}",
        f"  اخطارها: {int(reputation.warnings)}",
    ]
    deadline = as_naive_utc(getattr(reputation, "mute_until", None))
    if bool(reputation.is_muted) and deadline is not None and deadline > utcnow():
        remaining = max(1, int((deadline - utcnow()).total_seconds() // 60))
        lines.append(f"  وضعیت: ساکت تا {remaining} دقیقهٔ دیگر 🔇")
    elif bool(reputation.is_muted):
        lines.append("  وضعیت: مهلت سکوت تمام شده است 🔊")
    else:
        lines.append("  وضعیت: آزاد ✅")
    return "\n".join(lines)


# ── commands ───────────────────────────────────────────────────────────────


def _owner_of(update: Any) -> int | None:
    caller = user_id(update)
    return caller if caller is not None and is_owner(caller) else None


async def mod_config_cmd(update: Any, context: Any) -> None:
    """Persist moderation switches for this chat, then echo the stored row."""
    if _owner_of(update) is None:
        await reply(update, _DENIED)
        return
    chat = chat_id(update)
    if chat is None:
        await reply(update, _NO_CHAT)
        return

    command_args = args(context)
    if not command_args:
        config = await asyncio.to_thread(ModerationEngine.get_config, chat)
        await reply(update, f"{format_config(config)}\n\n{_USAGE_CONFIG}")
        return

    parsed = parse_switches(command_args)
    if isinstance(parsed, str):
        await reply(update, parsed)
        return

    stored = await asyncio.to_thread(lambda: ModerationEngine.set_config(chat, **parsed))
    await reply(update, format_config(stored))


async def mod_warn_cmd(update: Any, context: Any) -> None:
    """Record a real warning and mute at the chat's configured threshold."""
    if _owner_of(update) is None:
        await reply(update, _DENIED)
        return
    chat = chat_id(update)
    if chat is None:
        await reply(update, _NO_CHAT)
        return
    command_args = args(context)
    target = parse_target_user(update, command_args)
    if target is None:
        await reply(update, _NO_TARGET)
        return

    reason = " ".join(a for a in command_args if not a.strip().lstrip("@").isdigit()).strip()

    def _apply() -> tuple[int, int, bool]:
        config = ModerationEngine.get_config(chat)
        max_warnings = int(config.max_warnings) if config is not None else 3
        mute_minutes = int(config.mute_duration_minutes) if config is not None else 30
        warnings = ModerationEngine.add_warning(target, chat, reason=reason or "manual")
        muted = warnings >= max_warnings
        if muted:
            ModerationEngine.mute_user(target, chat, mute_minutes)
        return warnings, max_warnings, muted

    warnings, max_warnings, muted = await asyncio.to_thread(_apply)
    await reply(update, format_warned(target, warnings, max_warnings, muted))


async def mod_mute_cmd(update: Any, context: Any) -> None:
    """Mute a user for a bounded number of minutes — and persist it."""
    if _owner_of(update) is None:
        await reply(update, _DENIED)
        return
    chat = chat_id(update)
    if chat is None:
        await reply(update, _NO_CHAT)
        return
    command_args = args(context)
    from_reply = reply_to_user_id(update) is not None
    target = parse_target_user(update, command_args)
    if target is None:
        await reply(update, _NO_TARGET)
        return
    minutes = parse_duration_minutes(command_args, target_from_reply=from_reply)
    if isinstance(minutes, str):
        await reply(update, minutes)
        return

    await asyncio.to_thread(ModerationEngine.mute_user, target, chat, minutes)
    await reply(update, format_muted(target, minutes))


async def mod_unmute_cmd(update: Any, context: Any) -> None:
    """Lift a mute, reporting truthfully whether one was in force."""
    if _owner_of(update) is None:
        await reply(update, _DENIED)
        return
    chat = chat_id(update)
    if chat is None:
        await reply(update, _NO_CHAT)
        return
    target = parse_target_user(update, args(context))
    if target is None:
        await reply(update, _NO_TARGET)
        return

    def _apply() -> bool:
        was_muted = ModerationEngine.is_muted(target, chat)
        ModerationEngine.unmute_user(target, chat)
        return was_muted

    was_muted = await asyncio.to_thread(_apply)
    await reply(update, format_unmuted(target, was_muted))


async def mod_reputation_cmd(update: Any, context: Any) -> None:
    """Show the stored reputation row for a user (defaults to the caller)."""
    chat = chat_id(update)
    if chat is None:
        await reply(update, _NO_CHAT)
        return
    target = parse_target_user(update, args(context)) or user_id(update)
    if target is None:
        await reply(update, _NO_TARGET)
        return

    reputation = await asyncio.to_thread(ModerationEngine.get_reputation, target, chat)
    await reply(update, format_reputation(target, reputation))
