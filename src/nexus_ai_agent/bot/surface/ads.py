"""Telegram surface for the advertisement engine (``features/ads.py``).

Commands: ``/ad_create``, ``/ad_list``, ``/ad_pause``, ``/ad_resume``,
``/ad_delete``, ``/ad_stats``.

What this replaces
------------------
All six commands were hard-coded replies inside ``bot/handlers.py``::

    async def ad_create_cmd(...) -> "📢 Ad campaign created successfully."
    async def ad_list_cmd(...)   -> "📢 Active Ads: 2, Paused: 1."
    async def ad_stats_cmd(...)  -> "📊 Ad Stats: 5k impressions, 200 clicks."

None of them touched a database: the reply was identical whether the engine
existed or not, and the ``/ad_stats`` numbers describe columns that do not
exist — :class:`~nexus_ai_agent.storage.models.AdCampaign` has no
impressions/clicks field. ``AdManager`` (242 lines, ten methods) had **zero**
importers anywhere in ``src/``, ``scripts/`` or ``migrations/``; the only
reference was a commented-out ``# ad_manager = AdManager()``.

Three engineering rules this module adds
----------------------------------------
* **Loop hygiene.** Every ``AdManager`` method is synchronous SQLModel CRUD, so
  each call is off-loaded with :func:`asyncio.to_thread` — the convention
  :mod:`.gamification` and the P0 batch established.
* **Chat scoping (an authorisation fix, not a refactor).** The engine keys
  ``pause_campaign`` / ``resume_campaign`` / ``delete_campaign`` on a bare
  ``campaign_id`` with no owner check. Calling it straight from a command would
  let any user in any group pause or delete another chat's campaign by guessing
  an integer (an IDOR). :func:`_load_owned` re-reads the row and compares
  ``chat_id`` before mutating; the bot owner bypasses it.
* **No invented metrics.** The formatters render only fields the row actually
  has, and :func:`format_stats` states that impression/click counts are not
  modelled. A truthful "no campaigns yet" beats a confident "2 active, 1 paused".

Out of scope (recorded on the board, not silently dropped): nothing calls
:meth:`AdManager.get_due_campaigns` / :meth:`AdManager.mark_delivered` yet, so a
campaign is *registered and scheduled in the data*, but no background tick
delivers it. Wiring the tick belongs to the worker lane, which PR#33 is
currently editing — see ``docs/DECISION_LOG.md`` D-0009.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from nexus_ai_agent.features.ads import AdManager
from nexus_ai_agent.features.owner_control import is_owner

from ._ptb import args, chat_id, reply, user_id

__all__ = [
    "CampaignDraft",
    "ad_create_cmd",
    "ad_delete_cmd",
    "ad_list_cmd",
    "ad_pause_cmd",
    "ad_resume_cmd",
    "ad_stats_cmd",
    "format_created",
    "format_list",
    "format_state_change",
    "format_stats",
    "parse_campaign_id",
    "parse_create_args",
    "parse_status_filter",
]

_SEPARATOR = "━━━━━━━━━━━━━━━━"
#: The exact strings ``bot/handlers.py`` used to answer with; they must never
#: come back, and ``tests/unit/test_surface_registration.py`` holds them.
STUB_STRINGS = (
    "📢 Ad campaign created successfully.",
    "📢 Active Ads: 2, Paused: 1.",
    "⏸️ Ad paused.",
    "▶️ Ad resumed.",
    "🗑️ Ad deleted.",
    "📊 Ad Stats: 5k impressions, 200 clicks.",
)

_DENIED = "⛔ این فرمان فقط برای مالک ربات است."
_NOT_YOURS = "🚫 این کمپین متعلق به این گفتگو نیست."
_USAGE_CREATE = (
    "❌ استفاده: /ad_create <متن آگهی> [--interval ساعت] [--repeats تعداد]\n"
    "مثال: /ad_create 📣 فروش ویژه فردا --interval 6 --repeats 4\n"
    "«تعداد ۰» یعنی نامحدود."
)
_USAGE_ID = "❌ استفاده: {} <شناسهٔ کمپین> — شناسه‌ها را با /ad_list ببینید."
_USAGE_FILTER = "❌ فیلترهای مجاز: {}"

#: ``create_campaign``'s own default; kept here so the usage text and the row
#: cannot drift apart when the engine default changes.
_DEFAULT_INTERVAL_HOURS = 24.0
#: One year. A typo like ``--interval 876000`` would otherwise pin a campaign
#: to a chat nobody ever sees again.
_MAX_INTERVAL_HOURS = 8760.0
#: ``AdManager.get_stats`` counts exactly these three plus the total.
_STATUSES = ("active", "paused", "completed")


@dataclass(frozen=True, slots=True)
class CampaignDraft:
    """A parsed ``/ad_create`` invocation, ready for :meth:`AdManager.create_campaign`."""

    text: str
    interval_hours: float
    max_repeats: int


# ── parsing (pure) ─────────────────────────────────────────────────────────


def parse_create_args(command_args: Sequence[str]) -> CampaignDraft | str:
    """Split ``/ad_create`` arguments into a draft, or return the error to show.

    Flags may appear anywhere in the line; the remaining words are the ad text,
    so ``/ad_create فروش --interval 6 ویژه`` keeps its words in order.
    """
    interval_hours = _DEFAULT_INTERVAL_HOURS
    max_repeats = 0
    words: list[str] = []

    index = 0
    while index < len(command_args):
        token = command_args[index]
        if token not in ("--interval", "--repeats"):
            words.append(token)
            index += 1
            continue
        if index + 1 >= len(command_args):
            return f"❌ بعد از {token} مقداری نیامده است.\n{_USAGE_CREATE}"
        raw = command_args[index + 1]
        try:
            value = float(raw)
        except ValueError:
            return f"❌ مقدار {token} باید عدد باشد (دریافت شد: {raw}).\n{_USAGE_CREATE}"
        if token == "--interval":
            if not 0 < value <= _MAX_INTERVAL_HOURS:
                return (
                    f"❌ فاصلهٔ زمانی باید بیشتر از ۰ و حداکثر {_MAX_INTERVAL_HOURS:.0f} ساعت باشد."
                )
            interval_hours = value
        else:
            if value < 0 or value != int(value):
                return f"❌ تعداد تکرار باید عدد صحیح نامنفی باشد (۰ = نامحدود؛ دریافت شد: {raw})."
            max_repeats = int(value)
        index += 2

    text = " ".join(words).strip()
    if not text:
        return _USAGE_CREATE
    return CampaignDraft(text=text, interval_hours=interval_hours, max_repeats=max_repeats)


def parse_campaign_id(command_args: Sequence[str], *, command: str) -> int | str:
    """Return the campaign id, or the usage error the caller should reply with."""
    if not command_args:
        return _USAGE_ID.format(command)
    try:
        identifier = int(command_args[0])
    except ValueError:
        return _USAGE_ID.format(command)
    if identifier <= 0:
        return _USAGE_ID.format(command)
    return identifier


def parse_status_filter(command_args: Sequence[str]) -> str | None | str:
    """``None`` (no filter), a valid status, or an error string to display."""
    if not command_args:
        return None
    candidate = command_args[0].strip().lower()
    if candidate in _STATUSES:
        return candidate
    return _USAGE_FILTER.format(" · ".join(_STATUSES))


# ── rendering (pure) ───────────────────────────────────────────────────────


def _hours(value: Any) -> str:
    """Render ``24.0`` as ``24`` and ``6.5`` as ``۶٫۵``-free ``6.5``."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "؟"
    return str(int(number)) if number == int(number) else f"{number:g}"


def format_created(campaign: dict[str, Any] | None, campaign_id: int) -> str:
    """Render the row the engine just wrote (:meth:`AdManager.create_campaign`)."""
    if campaign is None:
        # The INSERT committed but the read-back found nothing: say exactly
        # that instead of claiming success or hiding the inconsistency.
        return (
            f"⚠️ کمپین با شناسهٔ {campaign_id} ثبت شد اما بازخوانی آن ممکن نشد؛ "
            "با /ad_list وضعیت را بررسی کنید."
        )
    repeats = int(campaign.get("max_repeats") or 0)
    return "\n".join(
        [
            "📢 کمپین آگهی ثبت شد",
            _SEPARATOR,
            f"شناسه: {campaign.get('id')}",
            f"وضعیت: {campaign.get('status')}",
            f"فاصلهٔ اجرا: {_hours(campaign.get('interval_hours'))} ساعت",
            f"تعداد تکرار: {repeats if repeats else 'نامحدود'}",
            f"اجرای بعدی: {campaign.get('next_run') or '—'}",
        ]
    )


def format_list(rows: list[dict[str, Any]], status: str | None) -> str:
    """Render :meth:`AdManager.list_campaigns` for this chat."""
    scope = f" ({status})" if status else ""
    if not rows:
        empty = "هنوز کمپینی در این گفتگو ثبت نشده است."
        if status:
            empty = f"کمپینی با وضعیت «{status}» در این گفتگو نیست."
        return f"📋 آگهی‌ها{scope}\n{_SEPARATOR}\n{empty}\n\nبا /ad_create شروع کنید."

    lines = [f"📋 آگهی‌های این گفتگو{scope}", _SEPARATOR]
    for row in rows:
        repeats = int(row.get("max_repeats") or 0)
        sent = int(row.get("repeat_count") or 0)
        progress = f"{sent}/{repeats}" if repeats else str(sent)
        lines.append(
            f"• {row.get('id')} · {row.get('status')} · "
            f"هر {_hours(row.get('interval_hours'))} ساعت · ارسال‌شده {progress}"
        )
        lines.append(f"   «{row.get('text')}»")
    return "\n".join(lines)


def format_state_change(*, verb: str, campaign: dict[str, Any] | None, ok: bool) -> str:
    """Render the outcome of a pause/resume/delete, including the new state."""
    if not ok:
        return f"❌ {verb} انجام نشد: کمپین پیدا نشد (شاید همین حالا حذف شده باشد)."
    if campaign is None:
        return f"✅ {verb} انجام شد (بازخوانی وضعیت ممکن نشد)."
    status = campaign.get("status")
    if status is None:
        return f"✅ {verb} انجام شد؛ کمپین دیگر در جدول نیست."
    return (
        f"✅ {verb} انجام شد\n"
        f"کمپین {campaign.get('id')} · وضعیت فعلی: {status} · "
        f"ارسال‌شده: {campaign.get('repeat_count', 0)}"
    )


def format_stats(stats: dict[str, int]) -> str:
    """Render :meth:`AdManager.get_stats` — row counts, and nothing else."""
    return "\n".join(
        [
            "📊 آگهی‌های این گفتگو",
            _SEPARATOR,
            f"کل کمپین‌ها: {stats.get('total', 0)}",
            f"فعال: {stats.get('active', 0)} · متوقف: {stats.get('paused', 0)} · "
            f"کامل‌شده: {stats.get('completed', 0)}",
            "ℹ️ این شمارش، تعداد ردیف‌های جدول AdCampaign است؛ بازدید و کلیک در "
            "مدل دادهٔ این نسخه ثبت نمی‌شود.",
        ]
    )


# ── commands ──────────────────────────────────────────────────────────────


async def _load_owned(campaign_id: int, chat: int, uid: int) -> dict[str, Any] | None | str:
    """Return the row, ``None`` (missing), or an error string (not this chat's).

    The engine has no per-chat guard, so the guard lives here; the bot owner may
    still act on any chat's campaign (that is what ``is_owner`` means).
    """
    campaign = await asyncio.to_thread(AdManager.get_campaign, campaign_id)
    if campaign is None:
        return None
    if campaign.get("chat_id") != chat and not is_owner(uid):
        return _NOT_YOURS
    return campaign


async def _change_state(
    update: Any,
    context: Any,
    *,
    command: str,
    verb: str,
    action: Callable[[int], bool],
) -> None:
    """Shared body of ``/ad_pause`` · ``/ad_resume`` · ``/ad_delete``."""
    uid, chat = user_id(update), chat_id(update)
    if uid is None or chat is None:
        return
    parsed = parse_campaign_id(args(context), command=command)
    if isinstance(parsed, str):
        await reply(update, parsed)
        return
    campaign_id = parsed
    outcome = await _load_owned(campaign_id, chat, uid)
    if outcome is None:
        await reply(update, f"❌ کمپینی با شناسهٔ {campaign_id} وجود ندارد.")
        return
    if isinstance(outcome, str):
        await reply(update, outcome)
        return
    changed = await asyncio.to_thread(action, campaign_id)
    after = await asyncio.to_thread(AdManager.get_campaign, campaign_id)
    await reply(update, format_state_change(verb=verb, campaign=after, ok=bool(changed)))


async def ad_create_cmd(update: Any, context: Any) -> None:
    """``/ad_create`` — persist a campaign through :meth:`AdManager.create_campaign`."""
    uid, chat = user_id(update), chat_id(update)
    if uid is None or chat is None:
        return
    if not is_owner(uid):
        await reply(update, _DENIED)
        return
    parsed = parse_create_args(args(context))
    if isinstance(parsed, str):
        await reply(update, parsed)
        return
    campaign_id = await asyncio.to_thread(
        AdManager.create_campaign,
        chat,
        parsed.text,
        parsed.interval_hours,
        parsed.max_repeats,
        uid,
    )
    campaign = await asyncio.to_thread(AdManager.get_campaign, int(campaign_id))
    await reply(update, format_created(campaign, int(campaign_id)))


async def ad_list_cmd(update: Any, context: Any) -> None:
    """``/ad_list [status]`` — the chat's campaigns, from the database."""
    chat = chat_id(update)
    if chat is None:
        return
    status = parse_status_filter(args(context))
    if isinstance(status, str) and status not in _STATUSES:
        await reply(update, status)
        return
    rows = await asyncio.to_thread(AdManager.list_campaigns, chat, status)
    await reply(update, format_list(list(rows), status))


async def ad_stats_cmd(update: Any, context: Any) -> None:
    """``/ad_stats`` — real row counts for this chat."""
    chat = chat_id(update)
    if chat is None:
        return
    stats = await asyncio.to_thread(AdManager.get_stats, chat)
    await reply(update, format_stats(dict(stats)))


async def ad_pause_cmd(update: Any, context: Any) -> None:
    """``/ad_pause <id>`` — set a campaign to ``paused``."""
    await _change_state(
        update,
        context,
        command="/ad_pause",
        verb="توقف",
        action=AdManager.pause_campaign,
    )


async def ad_resume_cmd(update: Any, context: Any) -> None:
    """``/ad_resume <id>`` — set a paused campaign back to ``active``."""
    await _change_state(
        update,
        context,
        command="/ad_resume",
        verb="ازسرگیری",
        action=AdManager.resume_campaign,
    )


async def ad_delete_cmd(update: Any, context: Any) -> None:
    """``/ad_delete <id>`` — remove the row."""
    await _change_state(
        update,
        context,
        command="/ad_delete",
        verb="حذف",
        action=AdManager.delete_campaign,
    )
