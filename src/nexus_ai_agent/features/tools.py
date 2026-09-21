"""Utility tools for NEXUS AI Telegram bot.

Includes:
- ReminderSystem: Persistent reminders with asyncio
- Translator: Free translation via MyMemory API
- UnitConverter: Currency and metric conversions (pure Python)
- Calculator: Safe math expression evaluator
"""

from __future__ import annotations

import ast
import asyncio
import math
import operator
import re
from datetime import datetime, timedelta, timezone
from typing import Any, cast

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


#: Upper bound for a single reminder (``asyncio.sleep`` on a 32-bit float is fine,
#: but a bot process rarely lives longer than this and the DB row survives restarts).
MAX_REMINDER_DELAY = timedelta(days=30)
#: Reminder text is truncated to this many characters (Telegram-friendly, DB-friendly).
MAX_REMINDER_TEXT = 500


class ReminderSystem:
    """Persistent reminder system with asyncio-based scheduling.

    The Telegram ``bot`` may be attached after construction via
    :meth:`bind_bot` (the PTB context only exposes it inside a handler), so a
    process-wide instance can be created at import time and wired later.
    Reminders are delivered to the *chat* they were created in.
    """

    def __init__(self, bot: Any | None = None) -> None:
        self.bot = bot
        self._tasks: dict[int, asyncio.Task[Any]] = {}  # reminder_id → task

    def bind_bot(self, bot: Any) -> None:
        """Attach (or replace) the bot used for delivery."""
        if bot is not None:
            self.bot = bot

    def _require_bot(self) -> Any:
        if self.bot is None:
            raise RuntimeError("Bot instance not set on ReminderSystem")
        return self.bot

    @property
    def scheduled_count(self) -> int:
        """Number of in-process timers still pending."""
        return sum(1 for t in self._tasks.values() if not t.done())

    async def _deliver(self, rid: int, chat_id: int, text: str, delay: float) -> None:
        await asyncio.sleep(max(delay, 0.0))
        try:
            await self._require_bot().send_message(chat_id=chat_id, text=f"⏰ یادآوری: {text}")
        except Exception:  # noqa: BLE001 - delivery failure must not kill the loop
            logger.exception("reminder_send_failed", reminder_id=rid)
        engine = _sync_engine()
        with Session(engine) as session:
            obj = session.get(Reminder, rid)
            if obj is not None and obj.status == "pending":
                obj.status = "sent"
                session.commit()
        self._tasks.pop(rid, None)

    def _schedule(self, rid: int, chat_id: int, text: str, delay: float) -> None:
        task = asyncio.create_task(self._deliver(rid, chat_id, text, delay))
        self._tasks[rid] = task  # strong reference: never GC'd mid-flight

    async def set_reminder(self, user_id: int, chat_id: int, time_str: str, text: str) -> str:
        """Set a reminder. *time_str* is like ``30m``, ``2h``, ``1d``."""
        parsed = _parse_remind_time(time_str)
        if parsed is None:
            return "❌ فرمت زمان نادرست است. مثال: /remind 30m نماز  (واحدها: s, m, h, d)"
        delta, _ = parsed
        if delta <= timedelta(0):
            return "❌ زمان یادآوری باید بزرگ‌تر از صفر باشد."
        if delta > MAX_REMINDER_DELAY:
            return f"❌ حداکثر فاصله یادآوری {MAX_REMINDER_DELAY.days} روز است."
        text = text.strip()[:MAX_REMINDER_TEXT]
        if not text:
            return "❌ متن یادآوری خالی است. مثال: /remind 30m نماز"
        remind_at = datetime.now(timezone.utc) + delta

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

        self._schedule(rid, chat_id, text, delta.total_seconds())
        return f"✅ یادآوری #{rid} تنظیم شد: {text} ({time_str})"

    def list_pending(self, user_id: int, chat_id: int | None = None) -> list[dict[str, Any]]:
        """Pending reminders for *user_id* (optionally limited to *chat_id*)."""
        engine = _sync_engine()
        with Session(engine) as session:
            stmt = select(Reminder).where(Reminder.user_id == user_id, Reminder.status == "pending")
            if chat_id is not None:
                stmt = stmt.where(Reminder.chat_id == chat_id)
            rows = session.exec(stmt).all()
        return [
            {"id": r.id, "text": r.text, "remind_at": r.remind_at, "chat_id": r.chat_id}
            for r in sorted(rows, key=lambda r: r.remind_at)
        ]

    def cancel(self, user_id: int, reminder_id: int) -> bool:
        """Cancel a pending reminder owned by *user_id*. Returns ``True`` when cancelled."""
        engine = _sync_engine()
        with Session(engine) as session:
            obj = session.get(Reminder, reminder_id)
            if obj is None or obj.user_id != user_id or obj.status != "pending":
                return False
            obj.status = "cancelled"
            session.commit()
        task = self._tasks.pop(reminder_id, None)
        if task is not None:
            task.cancel()
        return True

    async def restore_pending(self) -> int:
        """Re-arm pending reminders from the DB (after a restart). Returns the count.

        Overdue reminders are delivered immediately rather than silently dropped.
        """
        engine = _sync_engine()
        with Session(engine) as session:
            rows = session.exec(select(Reminder).where(Reminder.status == "pending")).all()
            pending = [
                (r.id or 0, r.chat_id, r.text, r.remind_at)
                for r in rows
                if r.id is not None and r.id not in self._tasks
            ]
        now = datetime.now(timezone.utc)
        for rid, chat_id, text, remind_at in pending:
            if remind_at.tzinfo is None:
                remind_at = remind_at.replace(tzinfo=timezone.utc)
            self._schedule(rid, chat_id, text, (remind_at - now).total_seconds())
        return len(pending)


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
# Safe Calculator — AST allow-list evaluator (no ``eval``)
# ═══════════════════════════════════════════════════════════════════════

#: Longest expression the calculator accepts (keeps parsing and error text short).
MAX_EXPRESSION_LENGTH = 200
#: Integer results wider than this are refused *before* they are materialised
#: (~4 000 decimal digits — safely under CPython's int→str conversion limit).
_MAX_RESULT_BITS = 13_300
#: ``a ** b`` is refused when ``|b|`` exceeds this (for any ``|a| > 1``).
_MAX_EXPONENT = 10_000
#: ``factorial(n)`` is refused beyond this (1000! has 2 568 digits).
_MAX_FACTORIAL = 1_000
#: ``round(x, n)`` is refused beyond this many digits either side of the point.
_MAX_ROUND_DIGITS = 100


class CalculatorError(ValueError):
    """Raised for any expression the safe evaluator refuses."""


def _guard_size(value: Any) -> Any:
    """Refuse integers that would be too wide to print or keep computing with."""
    if isinstance(value, int) and not isinstance(value, bool):
        if value.bit_length() > _MAX_RESULT_BITS:
            raise CalculatorError("نتیجه بیش از حد بزرگ است.")
    return value


def _safe_pow(base: Any, exp: Any) -> Any:
    if isinstance(exp, int) and abs(exp) > _MAX_EXPONENT and abs(base) > 1:
        raise CalculatorError("توان بیش از حد بزرگ است.")
    if isinstance(exp, float) and abs(exp) > _MAX_EXPONENT:
        raise CalculatorError("توان بیش از حد بزرگ است.")
    if isinstance(base, int) and isinstance(exp, int) and exp > 0 and abs(base) > 1:
        # Estimate the result width before touching it: bits(base) * exp.
        if abs(base).bit_length() * exp > _MAX_RESULT_BITS:
            raise CalculatorError("نتیجه بیش از حد بزرگ است.")
    try:
        return _guard_size(operator.pow(base, exp))
    except OverflowError as exc:
        raise CalculatorError("نتیجه بیش از حد بزرگ است.") from exc
    except ZeroDivisionError as exc:
        raise CalculatorError("تقسیم بر صفر ممکن نیست.") from exc


def _safe_factorial(n: Any) -> int:
    if isinstance(n, float) and n.is_integer():
        n = int(n)
    if not isinstance(n, int) or isinstance(n, bool) or n < 0:
        raise CalculatorError("factorial فقط برای اعداد صحیح نامنفی تعریف شده است.")
    if n > _MAX_FACTORIAL:
        raise CalculatorError(f"factorial حداکثر تا {_MAX_FACTORIAL} پشتیبانی می‌شود.")
    return math.factorial(n)


def _safe_round(value: Any, ndigits: Any = None) -> Any:
    if ndigits is None:
        return round(value)
    if not isinstance(ndigits, int) or abs(ndigits) > _MAX_ROUND_DIGITS:
        raise CalculatorError(f"round حداکثر تا {_MAX_ROUND_DIGITS} رقم پشتیبانی می‌شود.")
    return round(value, ndigits)


_SAFE_FUNCS: dict[str, Any] = {
    "abs": abs,
    "round": _safe_round,
    "min": min,
    "max": max,
    "sum": lambda *xs: sum(xs),
    "pow": _safe_pow,
    "factorial": _safe_factorial,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "sqrt": math.sqrt,
    "log": math.log,
    "log10": math.log10,
    "log2": math.log2,
    "ceil": math.ceil,
    "floor": math.floor,
    "exp": math.exp,
    "gcd": math.gcd,
}
_SAFE_CONSTS: dict[str, float] = {"pi": math.pi, "e": math.e, "tau": math.tau}

_BIN_OPS: dict[type[ast.operator], Any] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
}
_UNARY_OPS: dict[type[ast.unaryop], Any] = {ast.UAdd: operator.pos, ast.USub: operator.neg}

# ``20%`` (a percent literal with no operand after it) → ``(20/100)``;
# ``10 % 3`` keeps its modulo meaning.
_PERCENT_LITERAL_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%(?!\s*[\d(\w])")
_PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


class _SafeEvaluator:
    """Evaluate a parsed arithmetic expression using only allow-listed nodes."""

    def visit(self, node: ast.AST) -> Any:
        method = getattr(self, f"visit_{type(node).__name__}", None)
        if method is None:
            raise CalculatorError("عبارت نامعتبر است.")
        return method(node)

    def visit_Expression(self, node: ast.Expression) -> Any:  # noqa: N802
        return self.visit(node.body)

    def visit_Constant(self, node: ast.Constant) -> Any:  # noqa: N802
        if isinstance(node.value, bool) or not isinstance(node.value, int | float):
            raise CalculatorError("فقط اعداد مجاز هستند.")
        return _guard_size(node.value)

    def visit_Name(self, node: ast.Name) -> Any:  # noqa: N802
        try:
            return _SAFE_CONSTS[node.id]
        except KeyError:
            raise CalculatorError(f"نام ناشناخته: {node.id}") from None

    def visit_UnaryOp(self, node: ast.UnaryOp) -> Any:  # noqa: N802
        op = _UNARY_OPS.get(type(node.op))
        if op is None:
            raise CalculatorError("عملگر پشتیبانی نمی‌شود.")
        return op(self.visit(node.operand))

    def visit_BinOp(self, node: ast.BinOp) -> Any:  # noqa: N802
        left = self.visit(node.left)
        right = self.visit(node.right)
        if isinstance(node.op, ast.Pow):
            return _safe_pow(left, right)
        op = _BIN_OPS.get(type(node.op))
        if op is None:
            raise CalculatorError("عملگر پشتیبانی نمی‌شود.")
        try:
            return _guard_size(op(left, right))
        except ZeroDivisionError as exc:
            raise CalculatorError("تقسیم بر صفر ممکن نیست.") from exc
        except OverflowError as exc:
            raise CalculatorError("نتیجه بیش از حد بزرگ است.") from exc

    def visit_Call(self, node: ast.Call) -> Any:  # noqa: N802
        if not isinstance(node.func, ast.Name) or node.keywords:
            raise CalculatorError("فراخوانی نامعتبر است.")
        func = _SAFE_FUNCS.get(node.func.id)
        if func is None:
            raise CalculatorError(f"تابع ناشناخته: {node.func.id}")
        if len(node.args) > 8:
            raise CalculatorError("تعداد آرگومان‌ها زیاد است.")
        args = [self.visit(a) for a in node.args]
        try:
            return _guard_size(func(*args))
        except CalculatorError:
            raise
        except (ValueError, TypeError, OverflowError, ZeroDivisionError) as exc:
            raise CalculatorError(f"خطا در {node.func.id}: {exc}") from exc


def normalize_expression(expr: str) -> str:
    """Normalise user input: Persian digits, ``^`` power, ``×``/``÷``, percent literals."""
    cleaned = expr.strip().translate(_PERSIAN_DIGITS)
    cleaned = cleaned.replace("^", "**").replace("×", "*").replace("÷", "/").replace("،", ",")
    return _PERCENT_LITERAL_RE.sub(r"(\1/100)", cleaned)


def safe_eval(expr: str) -> float | int:
    """Evaluate an arithmetic expression without ``eval``.

    Raises :class:`CalculatorError` for anything outside the allow-list
    (names, attribute access, strings, comprehensions, lambdas, ...) and for
    results that would be too expensive to compute or print.
    """
    if not expr or not expr.strip():
        raise CalculatorError("عبارتی وارد نشده است.")
    if len(expr) > MAX_EXPRESSION_LENGTH:
        raise CalculatorError(f"عبارت طولانی‌تر از {MAX_EXPRESSION_LENGTH} کاراکتر است.")
    cleaned = normalize_expression(expr)
    try:
        tree = ast.parse(cleaned, mode="eval")
    except (SyntaxError, ValueError, RecursionError, MemoryError) as exc:
        raise CalculatorError("عبارت نامعتبر است.") from exc
    result = _SafeEvaluator().visit(tree)
    if isinstance(result, float) and (math.isnan(result) or math.isinf(result)):
        raise CalculatorError("نتیجه تعریف‌نشده است.")
    return cast(float | int, result)


def format_number(value: float | int) -> str:
    """Render a result compactly (``4`` not ``4.0``; floats trimmed to 10 significant digits)."""
    if isinstance(value, bool):
        return str(int(value))
    if isinstance(value, int):
        return f"{value:,}" if abs(value) >= 10_000 else str(value)
    if value.is_integer() and abs(value) < 1e15:
        return format_number(int(value))
    return f"{value:.10g}"


class Calculator:
    """Safe math expression evaluator (AST allow-list; never calls ``eval``)."""

    def evaluate(self, expr: str) -> str:
        """Evaluate *expr* and return a user-facing line (never raises)."""
        try:
            result = safe_eval(expr)
        except CalculatorError as exc:
            return f"❌ {exc}"
        return f"🧮 {expr.strip()} = {format_number(result)}"
