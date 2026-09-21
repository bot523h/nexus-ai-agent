"""Telegram surface for the utility tools (``/calc``, ``/tr``, ``/convert``, ``/remind``).

Wires the real engines in :mod:`nexus_ai_agent.features.tools` — which the
audit found to be dead code behind fixed-string stubs — to command
handlers.  Framework-free by design (see :mod:`._ptb`); the process-wide
:class:`ReminderSystem` receives the bot lazily from the handler context.

Registration (``bot/handlers.py``, owned by another agent at the time of
writing) is a plain swap of the stub closures for these coroutines::

    from nexus_ai_agent.bot.surface import calc_cmd, convert_cmd, remind_cmd, tr_cmd
"""

from __future__ import annotations

import re
from typing import Any

from nexus_ai_agent.features.tools import (
    Calculator,
    ReminderSystem,
    Translator,
    UnitConverter,
)

from ._ptb import args, bot_of, chat_id, reply, user_id

__all__ = [
    "calc_cmd",
    "convert_cmd",
    "get_reminder_system",
    "parse_convert_args",
    "parse_translate_args",
    "remind_cmd",
    "reminders_cmd",
    "reset_reminder_system",
    "tr_cmd",
]

#: MyMemory rejects requests above ~500 bytes; keep a safe character budget.
MAX_TRANSLATE_CHARS = 450
_LANG_CODE_RE = re.compile(r"^[a-z]{2}(?:-[a-z]{2})?$", re.IGNORECASE)
_PAIR_RE = re.compile(r"^([a-z]{2}(?:-[a-z]{2})?)\s*(?:>|->|:|→)\s*([a-z]{2}(?:-[a-z]{2})?)$", re.I)
_TARGET_ONLY_RE = re.compile(r"^(?:>|->|→|to:)\s*([a-z]{2}(?:-[a-z]{2})?)$", re.IGNORECASE)
#: Two-letter English words that are also ISO codes — never treated as a target language
#: when they start an otherwise English sentence (``/tr it is fine`` translates the whole).
_AMBIGUOUS_CODES = frozenset(
    "am an as at be by do go he hi id if in is it me my no of ok on or so to up us we".split()
)
_RTL_SCRIPT_RE = re.compile(r"[\u0600-\u06FF\u0750-\u077F\uFB50-\uFDFF\uFE70-\uFEFF]")
_PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")

_calculator = Calculator()
_converter = UnitConverter()
_translator = Translator()
_reminders = ReminderSystem()


def get_reminder_system() -> ReminderSystem:
    """The process-wide reminder scheduler (exposed for startup restore and tests)."""
    return _reminders


def reset_reminder_system() -> ReminderSystem:
    """Replace the process-wide scheduler (tests only)."""
    global _reminders
    _reminders = ReminderSystem()
    return _reminders


# ── /calc ────────────────────────────────────────────────────────────


async def calc_cmd(update: Any, context: Any) -> None:
    """``/calc <expression>`` — safe arithmetic (no ``eval``)."""
    expr = " ".join(args(context)).strip()
    if not expr:
        await reply(
            update,
            "🧮 ماشین‌حساب\n\n"
            "استفاده: /calc <عبارت>\n"
            "مثال: /calc (2+3)*4 ^ 2\n"
            "توابع: sqrt, sin, cos, tan, log, log10, abs, round, factorial, gcd, pi, e",
        )
        return
    await reply(update, _calculator.evaluate(expr))


# ── /tr ──────────────────────────────────────────────────────────────


def parse_translate_args(raw: list[str]) -> tuple[str, str, str] | None:
    """Return ``(source, target, text)`` or ``None`` when there is nothing to translate.

    Accepted forms::

        /tr سلام دنیا            → auto (Persian script → en, otherwise → fa)
        /tr de سلام دنیا         → target ``de`` (source auto-detected)
        /tr >it hello            → explicit target marker (needed for ``it``/``no``/...)
        /tr fa>en سلام دنیا      → explicit pair (also ``fa:en``, ``fa->en``)
    """
    if not raw:
        return None
    source = target = ""
    body = list(raw)
    pair = _PAIR_RE.match(body[0])
    target_only = _TARGET_ONLY_RE.match(body[0])
    if pair is not None:
        source, target = pair.group(1).lower(), pair.group(2).lower()
        body = body[1:]
    elif target_only is not None:
        target = target_only.group(1).lower()
        body = body[1:]
    elif _LANG_CODE_RE.match(body[0]) and body[0].lower() not in _AMBIGUOUS_CODES:
        if len(body) == 1:
            return None  # ``/tr de`` — a target without text: show usage
        target = body[0].lower()
        body = body[1:]
    text = " ".join(body).strip()
    if not text:
        return None
    if not source:
        source = "fa" if _RTL_SCRIPT_RE.search(text) else "en"
    if not target:
        target = "en" if source == "fa" else "fa"
    if source == target:
        target = "en" if source != "en" else "fa"
    return source, target, text


async def tr_cmd(update: Any, context: Any) -> None:
    """``/tr [lang|src>dst] <text>`` — free translation via MyMemory."""
    parsed = parse_translate_args(args(context))
    if parsed is None:
        await reply(
            update,
            "🌐 مترجم\n\n"
            "استفاده: /tr <متن>  (فارسی→انگلیسی یا برعکس، خودکار)\n"
            "زبان مقصد: /tr de سلام دنیا\n"
            "جفت زبان: /tr fa>ar سلام دنیا",
        )
        return
    source, target, text = parsed
    if len(text) > MAX_TRANSLATE_CHARS:
        await reply(update, f"❌ متن طولانی است؛ حداکثر {MAX_TRANSLATE_CHARS} کاراکتر.")
        return
    translated = await _translator.translate(text, source=source, target=target)
    if translated.startswith("❌"):
        await reply(update, translated)
        return
    await reply(update, f"🌐 {source}→{target}\n{translated}")


# ── /convert ─────────────────────────────────────────────────────────


def parse_convert_args(raw: list[str]) -> tuple[float, str, str] | None:
    """Parse ``<amount> <from> [to] <to>`` (also ``100usd irt``). ``None`` when invalid."""
    tokens = [t.translate(_PERSIAN_DIGITS) for t in raw if t.strip()]
    if not tokens:
        return None
    m = re.match(r"^([-+]?\d+(?:[.,]\d+)?)([a-zA-Z°]+)?$", tokens[0])
    if m is None:
        return None
    amount = float(m.group(1).replace(",", "."))
    rest = tokens[1:]
    if m.group(2):
        rest = [m.group(2), *rest]
    rest = [t for t in rest if t.lower() not in {"to", "in", "به", "->", "→"}]
    if len(rest) != 2:
        return None
    return amount, rest[0].strip("°"), rest[1].strip("°")


async def convert_cmd(update: Any, context: Any) -> None:
    """``/convert <amount> <from> <to>`` — currency, length, weight, temperature."""
    parsed = parse_convert_args(args(context))
    if parsed is None:
        await reply(
            update,
            "💱 تبدیل واحد\n\n"
            "استفاده: /convert <مقدار> <از> <به>\n"
            "مثال: /convert 100 usd irt\n"
            "مثال: /convert 5 km mile · /convert 30 c f · /convert 2 kg lb\n"
            "⚠️ نرخ ارزها تقریبی و ثابت است (زنده نیست).",
        )
        return
    amount, from_unit, to_unit = parsed
    await reply(update, _converter.convert(amount, from_unit, to_unit))


# ── /remind ──────────────────────────────────────────────────────────


async def remind_cmd(update: Any, context: Any) -> None:
    """``/remind <30m|2h|1d> <text>`` — persistent reminder delivered to this chat."""
    uid, cid = user_id(update), chat_id(update)
    if uid is None or cid is None:
        return
    raw = args(context)
    if len(raw) < 2:
        await reply(
            update,
            "⏰ یادآور\n\n"
            "استفاده: /remind <زمان> <متن>\n"
            "مثال: /remind 30m نماز · /remind 2h جلسه · /remind 1d قبض\n"
            "لیست: /reminders · لغو: /reminders cancel <شماره>",
        )
        return
    _reminders.bind_bot(bot_of(context))
    time_str = raw[0].translate(_PERSIAN_DIGITS)
    text = " ".join(raw[1:])
    await reply(update, await _reminders.set_reminder(uid, cid, time_str, text))


async def reminders_cmd(update: Any, context: Any) -> None:
    """``/reminders`` lists pending reminders; ``/reminders cancel <id>`` cancels one."""
    uid = user_id(update)
    if uid is None:
        return
    raw = args(context)
    if len(raw) >= 2 and raw[0].lower() in {"cancel", "del", "delete", "لغو", "حذف"}:
        try:
            rid = int(raw[1].translate(_PERSIAN_DIGITS).lstrip("#"))
        except ValueError:
            await reply(update, "❌ شماره یادآوری نامعتبر است.")
            return
        ok = _reminders.cancel(uid, rid)
        await reply(update, f"🗑️ یادآوری #{rid} لغو شد." if ok else "❌ یادآوری پیدا نشد.")
        return
    pending = _reminders.list_pending(uid)
    if not pending:
        await reply(update, "⏰ یادآوری فعالی ندارید.")
        return
    lines = ["⏰ یادآوری‌های فعال:"]
    for item in pending[:20]:
        when = item["remind_at"].strftime("%Y-%m-%d %H:%M UTC")
        lines.append(f"  #{item['id']} — {item['text']} ({when})")
    await reply(update, "\n".join(lines))
