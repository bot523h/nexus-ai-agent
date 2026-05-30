"""Utility tools for NEXUS AI Telegram bot.

Includes:
- ReminderSystem: Persistent reminders with asyncio
- Translator: Free translation via MyMemory API
- UnitConverter: Currency and metric conversions (pure Python)
- Calculator: Safe math expression evaluator
"""

from __future__ import annotations

import asyncio
import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlmodel import Session, select

from nexus_ai_agent.config.settings import get_settings
from nexus_ai_agent.observability.logging import get_logger
from nexus_ai_agent.storage.models import Reminder

logger = get_logger(__name__)


def _sync_engine() -> Any:
    """Return a synchronous SQLAlchemy engine for feature CRUD."""
    from sqlalchemy import create_engine as _ce

    settings = get_settings()
    return _ce(f"sqlite:///{settings.db_path}", echo=False)


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


class ReminderSystem:
    """Persistent reminder system with asyncio-based scheduling."""

    def __init__(self, bot: Any | None = None) -> None:
        self.bot = bot
        self._tasks: dict[int, asyncio.Task[Any]] = {}  # reminder_id → task

    def _require_bot(self) -> Any:
        if self.bot is None:
            raise RuntimeError("Bot instance not set on ReminderSystem")
        return self.bot

    async def set_reminder(self, user_id: int, chat_id: int, time_str: str, text: str) -> str:
        """Set a reminder. *time_str* is like '30m', '2h', '1d'."""
        parsed = _parse_remind_time(time_str)
        if parsed is None:
            return "❌ فرمت نادرست. مثال: /remind 30m نماز"
        delta, _ = parsed
        remind_at = datetime.now(timezone.utc) + delta

        # Save to DB
        engine = _sync_engine()
        with Session(engine) as session:
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
            rid: int = reminder.id if reminder.id is not None else 0

        # Schedule the background task
        async def _fire() -> None:
            await asyncio.sleep(delta.total_seconds())
            bot = self._require_bot()
            try:
                await bot.send_message(chat_id=user_id, text=f"⏰ یادآوری: {text}")
            except Exception:  # noqa: BLE001
                logger.exception("reminder_send_failed", reminder_id=rid)
            engine2 = _sync_engine()
            with Session(engine2) as s2:
                obj = s2.get(Reminder, rid)
                if obj is not None:
                    obj.status = "sent"
                    s2.commit()

        task = asyncio.create_task(_fire())
        self._tasks[rid] = task
        return f"✅ یادآوری تنظیم شد: {text} ({time_str})"

    async def restore_pending(self) -> int:
        """Restore pending reminders from DB (after restart). Returns count."""
        engine = _sync_engine()
        count = 0
        with Session(engine) as session:
            stmt = select(Reminder).where(Reminder.status == "pending")
            results = session.exec(stmt).all()
            now = datetime.now(timezone.utc)
            for r in results:
                if r.remind_at <= now:
                    # Already overdue — send immediately
                    r.status = "sent"
                    count += 1
                    continue
                delay = (r.remind_at - now).total_seconds()
                rid: int = r.id if r.id is not None else 0

                async def _fire(
                    _rid: int = rid,
                    _chat_id: int = r.chat_id,
                    _text: str = r.text,
                    _delay: float = delay,
                ) -> None:
                    await asyncio.sleep(_delay)
                    bot = self._require_bot()
                    try:
                        await bot.send_message(
                            chat_id=_chat_id,
                            text=f"⏰ یادآوری: {_text}",
                        )
                    except Exception:  # noqa: BLE001
                        logger.exception("reminder_send_failed", reminder_id=_rid)
                    engine2 = _sync_engine()
                    with Session(engine2) as s2:
                        obj = s2.get(Reminder, _rid)
                        if obj is not None:
                            obj.status = "sent"
                            s2.commit()

                task = asyncio.create_task(_fire())
                self._tasks[rid] = task
                count += 1
            session.commit()
        return count


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
# Safe Calculator
# ═══════════════════════════════════════════════════════════════════════

# Only allow safe math operations
_SAFE_NAMES: dict[str, Any] = {
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
    "pow": pow,
    "sum": sum,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "sqrt": math.sqrt,
    "log": math.log,
    "log10": math.log10,
    "pi": math.pi,
    "e": math.e,
    "ceil": math.ceil,
    "floor": math.floor,
    "factorial": math.factorial,
    "gcd": math.gcd,
    "exp": math.exp,
}


class Calculator:
    """Safe math expression evaluator."""

    def evaluate(self, expr: str) -> str:
        """Safely evaluate a math expression."""
        # Replace ^ with ** for exponentiation
        cleaned = expr.replace("^", "**").strip()
        # Remove percent-style
        cleaned = cleaned.replace("%", "/100")
        # Basic safety: only digits, operators, parens, dots, spaces
        # Allow letters for function names
        check = cleaned.replace("**", "  ").replace(" ", "")
        # We need to allow letters for sin, cos, etc.
        if not re.match(r"^[\d\s\+\-\*/\.\(\),a-zA-Z_]+$", check):
            return "❌ عبارت نامعتبر است."
        try:
            result = eval(cleaned, {"__builtins__": {}}, _SAFE_NAMES)  # noqa: S307
            return f"🧮 {expr} = {result}"
        except Exception as exc:  # noqa: BLE001
            return f"❌ خطا در محاسبه: {exc}"
