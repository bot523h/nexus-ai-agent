"""Utility tools for NEXUS AI Telegram bot.

Includes:
- ReminderSystem: Persistent reminders with asyncio (deliver to the
  originating chat, bindable bot, cancel + list support, survives restarts)
- Translator: Free translation via MyMemory API
- UnitConverter: Currency and metric conversions (pure Python)
- Calculator: Safe math expression evaluator (no ``eval`` — see
  :mod:`nexus_ai_agent.features.calculator`)
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy import create_engine as _create_engine
from sqlmodel import Session, select

from nexus_ai_agent.config.settings import get_settings
from nexus_ai_agent.features.calculator import (
    CalculatorError,
    ExpressionTooComplex,
    MathError,
    SafeCalculator,
    format_result,
)
from nexus_ai_agent.observability.logging import get_logger
from nexus_ai_agent.storage.models import Reminder

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════════
# Reminder System
# ═══════════════════════════════════════════════════════════════════════

# Parse time strings like "30m", "2h", "1d"
_TIME_RE = re.compile(r"^(\d+)([smhd])$", re.IGNORECASE)


def _parse_remind_time(text: str) -> tuple[timedelta, str] | None:
    """Parse a reminder time string. Returns (timedelta, original_text) or None."""
    m = _TIME_RE.match(text.strip())
    if m is None:
        return None
    amount = int(m.group(1))
    if amount <= 0:
        return None
    unit = m.group(2).lower()
    if unit == "s":
        return timedelta(seconds=amount), text
    if unit == "m":
        return timedelta(minutes=amount), text
    if unit == "h":
        return timedelta(hours=amount), text
    if unit == "d":
        return timedelta(days=amount), text
    return None


def _as_utc(dt: datetime) -> datetime:
    """Normalise a possibly-naive stored datetime to aware UTC.

    SQLite stores ``DATETIME`` without an offset, so values read back are
    naive even though they were written as UTC.
    """
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


class ReminderSystem:
    """Persistent reminder system with asyncio-based scheduling.

    Guarantees implemented here:

    - **Originating-chat delivery**: a reminder fires into the chat it was
      created in (``chat_id``), never into ``user_id`` as a chat.
    - **Bindable bot**: the Telegram bot may be attached any time via
      :meth:`bind` (application startup) without rebuilding the system.
    - **Cancel support**: users may cancel their own pending reminders
      with :meth:`cancel_reminder` (ownership-checked).
    - **Restart safety**: :meth:`restore_pending` reschedules pending rows;
      overdue rows are delivered immediately instead of being dropped.
    - **Bounded I/O**: every Telegram send is wrapped in a timeout.
    - **Non-blocking event loop** (P1-2): the SQLite core is synchronous
      and runs *off the event loop* via :func:`asyncio.to_thread`.  Each
      public async method is a thin wrapper over a ``*_sync`` core — the
      reference pattern for the rest of the feature engines.  The engine
      is created with ``check_same_thread=False`` because pooled
      connections legitimately cross the loop/worker thread boundary.
    """

    SEND_TIMEOUT_SECONDS = 30.0

    def __init__(self, bot: Any | None = None, db_path: str | None = None) -> None:
        self.bot = bot
        # Optional explicit DB path (tests); production uses settings.db_path.
        self._db_path = db_path
        self._engine: Any | None = None
        self._tasks: dict[int, asyncio.Task[None]] = {}

    # -- bot binding -------------------------------------------------------

    def bind(self, bot: Any) -> None:
        """Attach (or replace) the Telegram bot used for delivery."""
        self.bot = bot

    @property
    def is_bound(self) -> bool:
        return self.bot is not None

    def _require_bot(self) -> Any:
        if self.bot is None:
            raise RuntimeError("Bot instance not set on ReminderSystem")
        return self.bot

    # -- engine --------------------------------------------------------------

    def _engine_ref(self) -> Any:
        if self._engine is None:
            path = self._db_path or get_settings().db_path
            # P1-2: sessions are executed in worker threads (to_thread);
            # pooled connections may therefore be created and used on
            # different threads, which sqlite3 allows only with
            # check_same_thread=False.
            self._engine = _create_engine(
                f"sqlite:///{path}", echo=False, connect_args={"check_same_thread": False}
            )
        return self._engine

    def close(self) -> None:
        """Cancel scheduled tasks and dispose the engine (shutdown hook)."""
        for task in self._tasks.values():
            if not task.done():
                task.cancel()
        self._tasks.clear()
        if self._engine is not None:
            self._engine.dispose()
            self._engine = None

    # -- user-facing API -----------------------------------------------------

    async def set_reminder(self, user_id: int, chat_id: int, time_str: str, text: str) -> str:
        """Set a reminder. *time_str* is like '30m', '2h', '1d'."""
        parsed = _parse_remind_time(time_str)
        if parsed is None:
            return "❌ فرمت نادرست. مثال: /remind 30m نماز"
        if not self.is_bound:
            logger.error("reminder_set_unbound", user_id=user_id)
            return "❌ سیستم یادآوری در دسترس نیست."
        delta, _ = parsed
        remind_at = datetime.now(timezone.utc) + delta

        # P1-2: sync SQLite core off the event loop.
        rid = await asyncio.to_thread(
            self._persist_reminder_sync, user_id, chat_id, remind_at, text
        )

        self._schedule(rid, user_id, chat_id, text, delta)
        return f"✅ یادآوری #{rid} تنظیم شد: {text} ({time_str})"

    def _persist_reminder_sync(
        self, user_id: int, chat_id: int, remind_at: datetime, text: str
    ) -> int:
        """Synchronous DB core for :meth:`set_reminder` (runs in a worker
        thread via :func:`asyncio.to_thread`)."""
        with Session(self._engine_ref()) as session:
            reminder = Reminder(
                user_id=user_id,
                chat_id=chat_id,
                text=text,
                remind_at=remind_at,
                status="pending",
            )
            session.add(reminder)
            session.commit()
            session.refresh(reminder)
            return reminder.id if reminder.id is not None else 0

    async def cancel_reminder(self, reminder_id: int, user_id: int) -> str:
        """Cancel a pending reminder. Only the creator may cancel it."""
        # P1-2: sync SQLite core off the event loop.
        error = await asyncio.to_thread(self._cancel_reminder_sync, reminder_id, user_id)
        if error is not None:
            return error

        task = self._tasks.pop(reminder_id, None)
        if task is not None and not task.done():
            task.cancel()
        return f"✅ یادآوری #{reminder_id} لغو شد."

    def _cancel_reminder_sync(self, reminder_id: int, user_id: int) -> str | None:
        """Synchronous DB core for :meth:`cancel_reminder`.

        Returns the user-facing error message when the cancellation is
        refused, or ``None`` when the row was marked cancelled (the caller
        then cancels the live task on the event-loop thread).
        """
        with Session(self._engine_ref()) as session:
            obj = session.get(Reminder, reminder_id)
            if obj is None:
                return "❌ یادآوری‌ای با این شناسه پیدا نشد."
            if obj.user_id != user_id:
                return "❌ شما مجوز لغو این یادآوری را ندارید."
            if obj.status != "pending":
                return f"⚠️ این یادآوری «{obj.status}» است و دیگر لغو نمی‌شود."
            obj.status = "cancelled"
            session.commit()
        return None

    async def list_reminders(self, user_id: int) -> str:
        """List the user's pending reminders with their ids."""
        # P1-2: sync SQLite core off the event loop.
        rows = await asyncio.to_thread(self._list_reminders_sync, user_id)
        if not rows:
            return "📭 یادآوری در انتظار ندارید."
        lines = ["⏰ یادآوری‌های در انتظار:", ""]
        for r in rows:
            when = _as_utc(r.remind_at).strftime("%Y-%m-%d %H:%M UTC")
            rid: int = r.id if r.id is not None else 0
            lines.append(f"#{rid} — {r.text} ({when})")
        lines.append("")
        lines.append("لغو: /cancel_remind <id>")
        return "\n".join(lines)

    def _list_reminders_sync(self, user_id: int) -> list[Reminder]:
        """Synchronous DB core for :meth:`list_reminders`."""
        with Session(self._engine_ref()) as session:
            return list(
                session.exec(
                    select(Reminder)
                    .where(Reminder.user_id == user_id, Reminder.status == "pending")
                    .order_by(Reminder.remind_at)  # type: ignore[arg-type]
                ).all()
            )

    async def restore_pending(self) -> int:
        """Restore pending reminders from DB (after restart).

        Overdue reminders are delivered immediately; future ones are
        rescheduled. Returns the number of pending reminders processed.
        """
        now = datetime.now(timezone.utc)
        # P1-2: sync SQLite core off the event loop.
        pending = await asyncio.to_thread(self._restore_pending_sync)

        count = 0
        for rid, user_id, chat_id, text, remind_at in pending:
            delay = (remind_at - now).total_seconds()
            self._schedule(rid, user_id, chat_id, text, timedelta(seconds=max(delay, 0.0)))
            count += 1
        if count:
            logger.info("reminders_restored", count=count)
        return count

    def _restore_pending_sync(
        self,
    ) -> list[tuple[int, int, int, str, datetime]]:
        """Synchronous DB core for :meth:`restore_pending`."""
        with Session(self._engine_ref()) as session:
            stmt = select(Reminder).where(Reminder.status == "pending")
            rows = session.exec(stmt).all()
            return [
                (r.id, r.user_id, r.chat_id, r.text, _as_utc(r.remind_at))
                for r in rows
                if r.id is not None
            ]

    # -- internals ------------------------------------------------------------

    def _schedule(self, rid: int, user_id: int, chat_id: int, text: str, delay: timedelta) -> None:
        old = self._tasks.get(rid)
        if old is not None and not old.done():
            old.cancel()
        task = asyncio.create_task(
            self._fire(rid, user_id, chat_id, text, max(delay.total_seconds(), 0.0))
        )
        self._tasks[rid] = task

    async def _fire(
        self, rid: int, user_id: int, chat_id: int, text: str, delay_seconds: float
    ) -> None:
        if delay_seconds > 0:
            await asyncio.sleep(delay_seconds)
        await self._deliver(rid, chat_id, text)

    async def _deliver(self, rid: int, chat_id: int, text: str) -> None:
        try:
            bot = self._require_bot()
            await asyncio.wait_for(
                bot.send_message(chat_id=chat_id, text=f"⏰ یادآوری: {text}"),
                timeout=self.SEND_TIMEOUT_SECONDS,
            )
            await self._mark_status(rid, "sent")
        except RuntimeError:
            # Bot unbound at fire time (e.g. restore before post_init bound it).
            logger.error("reminder_unbound_at_fire", reminder_id=rid)
            await self._mark_status(rid, "failed")
        except Exception:  # noqa: BLE001 — never let a reminder kill the loop
            logger.exception("reminder_send_failed", reminder_id=rid)
            await self._mark_status(rid, "failed")
        finally:
            self._tasks.pop(rid, None)

    async def _mark_status(self, rid: int, status: str) -> None:
        """Mark a reminder row (P1-2: sync core off the event loop)."""
        await asyncio.to_thread(self._mark_status_sync, rid, status)

    def _mark_status_sync(self, rid: int, status: str) -> None:
        try:
            with Session(self._engine_ref()) as session:
                obj = session.get(Reminder, rid)
                if obj is not None:
                    obj.status = status
                    session.commit()
        except Exception:  # noqa: BLE001 — status bookkeeping must not raise
            logger.exception("reminder_status_update_failed", reminder_id=rid)


# ═══════════════════════════════════════════════════════════════════════
# Translator (Free — MyMemory API)
# ═══════════════════════════════════════════════════════════════════════


class Translator:
    """Free translation via MyMemory API (no API key needed)."""

    MYMEMORY_URL = "https://api.mymemory.translated.net/get"

    async def translate(self, text: str, *, source: str = "fa", target: str = "en") -> str:
        """Translate *text* from *source* to *target* language code."""
        async with httpx.AsyncClient(timeout=15.0) as client:
            params = {
                "q": text,
                "langpair": f"{source}|{target}",
            }
            try:
                resp = await client.get(self.MYMEMORY_URL, params=params)
                resp.raise_for_status()
                data = resp.json()
                return data.get("responseData", {}).get("translatedText", text)
            except Exception:  # noqa: BLE001
                logger.exception("translate_failed", text=text[:50])
                return f"❌ خطا در ترجمه: {text}"


# ═══════════════════════════════════════════════════════════════════════
# Unit Converter (Pure Python — no external API)
# ═══════════════════════════════════════════════════════════════════════

# Approximate exchange rates (as of 2025, subject to change)
_EXCHANGE_RATES: dict[str, dict[str, float]] = {
    "usd": {
        "irt": 830000,
        "eur": 0.92,
        "gbp": 0.79,
        "cad": 1.37,
        "aud": 1.53,
        "jpy": 155.0,
        "cny": 7.25,
    },
    "eur": {
        "usd": 1.09,
        "irt": 905000,
        "gbp": 0.86,
        "cad": 1.49,
        "aud": 1.67,
        "jpy": 168.0,
        "cny": 7.88,
    },
    "irt": {
        "usd": 0.0000012,
        "eur": 0.0000011,
        "gbp": 0.00000095,
        "cad": 0.0000016,
        "aud": 0.0000018,
    },
    "gbp": {
        "usd": 1.27,
        "eur": 1.16,
        "irt": 1050000,
        "cad": 1.73,
        "aud": 1.94,
        "jpy": 196.0,
    },
    "cad": {
        "usd": 0.73,
        "eur": 0.67,
        "irt": 605000,
        "gbp": 0.58,
        "aud": 1.12,
        "jpy": 113.0,
    },
    "aud": {
        "usd": 0.65,
        "eur": 0.60,
        "irt": 542000,
        "gbp": 0.52,
        "cad": 0.89,
        "jpy": 101.0,
    },
    "jpy": {
        "usd": 0.0065,
        "eur": 0.0060,
        "irt": 5350,
        "gbp": 0.0051,
        "cad": 0.0088,
        "aud": 0.0099,
    },
    "cny": {
        "usd": 0.14,
        "eur": 0.13,
        "irt": 114000,
        "gbp": 0.11,
        "cad": 0.19,
        "aud": 0.21,
    },
}

_LENGTH: dict[str, float] = {
    "km": 1000,
    "m": 1,
    "cm": 0.01,
    "mm": 0.001,
    "mile": 1609.34,
    "yard": 0.9144,
    "ft": 0.3048,
    "in": 0.0254,
}
_WEIGHT: dict[str, float] = {
    "kg": 1,
    "g": 0.001,
    "mg": 0.000001,
    "lb": 0.453592,
    "oz": 0.0283495,
    "ton": 1000,
}
_TEMP_UNITS = {"c", "f", "k"}


class UnitConverter:
    """Convert currencies, lengths, weights, and temperatures."""

    def convert(self, amount: float, from_unit: str, to_unit: str) -> str:
        """Convert *amount* from *from_unit* to *to_unit*."""
        fu = from_unit.lower().strip()
        tu = to_unit.lower().strip()

        # Currency
        if fu in _EXCHANGE_RATES and tu in _EXCHANGE_RATES[fu]:
            result = amount * _EXCHANGE_RATES[fu][tu]
            return f"💰 {amount:,.2f} {from_unit.upper()} = {result:,.2f} {to_unit.upper()}"

        # Length
        if fu in _LENGTH and tu in _LENGTH:
            meters = amount * _LENGTH[fu]
            result = meters / _LENGTH[tu]
            return f"📏 {amount:,.4g} {fu} = {result:,.4g} {tu}"

        # Weight
        if fu in _WEIGHT and tu in _WEIGHT:
            kg = amount * _WEIGHT[fu]
            result = kg / _WEIGHT[tu]
            return f"⚖️ {amount:,.4g} {fu} = {result:,.4g} {tu}"

        # Temperature
        if fu in _TEMP_UNITS and tu in _TEMP_UNITS:
            result = self._convert_temp(amount, fu, tu)
            return f"🌡️ {amount:,.1f}°{fu.upper()} = {result:,.1f}°{tu.upper()}"

        return f"❌ تبدیل {from_unit} به {to_unit} پشتیبانی نمی‌شود."

    def _convert_temp(self, value: float, from_u: str, to_u: str) -> float:
        """Convert temperature between C, F, K."""
        # Convert to Celsius first
        if from_u == "c":
            c = value
        elif from_u == "f":
            c = (value - 32) * 5 / 9
        else:  # K
            c = value - 273.15
        # Convert from Celsius
        if to_u == "c":
            return c
        if to_u == "f":
            return c * 9 / 5 + 32
        return c + 273.15  # K


# ═══════════════════════════════════════════════════════════════════════
# Calculator (UI wrapper — the safe engine lives in features/calculator.py)
# ═══════════════════════════════════════════════════════════════════════


class Calculator:
    """Safe math expression evaluator.

    Delegates to :class:`~nexus_ai_agent.features.calculator.SafeCalculator`
    (strict AST whitelist — no ``eval``, no attribute access, bounded
    expressions) and maps errors back to the familiar Persian messages.
    """

    def evaluate(self, expr: str) -> str:
        """Evaluate *expr* and return a user-facing message."""
        try:
            result = SafeCalculator().evaluate(expr)
        except ExpressionTooComplex as exc:
            logger.info("calc_too_complex", expr=expr[:80], reason=str(exc))
            return "❌ عبارت نامعتبر است. (عبارت بیش از حد بزرگ یا پیچیده است)"
        except MathError as exc:
            return f"❌ خطا در محاسبه: {exc}"
        except CalculatorError:
            return "❌ عبارت نامعتبر است."
        return f"🧮 {expr} = {format_result(result)}"
