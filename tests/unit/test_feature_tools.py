"""B1 — ``/calc``, ``/tr``, ``/convert``, ``/remind`` wired to ``features/tools.py``."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine
from sqlmodel import Session, select
from surface_fakes import FakeBot, make_context, make_update

from nexus_ai_agent.bot.surface import tools as surface
from nexus_ai_agent.features.tools import (
    MAX_EXPRESSION_LENGTH,
    Calculator,
    CalculatorError,
    ReminderSystem,
    UnitConverter,
    safe_eval,
)
from nexus_ai_agent.storage.models import Reminder

# ── Calculator ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("expr", "expected"),
    [
        ("2+2", 4),
        ("(2+3)*4", 20),
        ("2^10", 1024),
        ("2**10", 1024),
        ("10 % 3", 1),
        ("20%", 0.2),
        ("7 // 2", 3),
        ("-5 + 2", -3),
        ("sqrt(16)", 4),
        ("round(pi, 2)", 3.14),
        ("factorial(5)", 120),
        ("gcd(12, 18)", 6),
        ("۲+۲", 4),
        ("3 × 4 ÷ 2", 6),
        ("abs(-3.5)", 3.5),
        ("min(4, 2, 8)", 2),
        ("max(1, 9)", 9),
        ("pow(2, 8)", 256),
    ],
)
def test_safe_eval_arithmetic(expr: str, expected: float) -> None:
    assert safe_eval(expr) == pytest.approx(expected)


@pytest.mark.parametrize(
    "expr",
    [
        "__import__('os').system('id')",
        "().__class__.__bases__[0].__subclasses__()",
        "open('/etc/passwd').read()",
        "x + 1",
        "lambda: 1",
        "[i for i in range(10)]",
        "'a' * 3",
        "1 if 1 else 2",
        "a.b",
        "2 ** 100000",
        "9 ** 9 ** 9",
        "factorial(5000)",
        "1/0",
        "sqrt(-1)",
        "round(1.5, 10**9)",
        "",
        "   ",
        "((((((",
        "1 +",
        "1 << 64",
        "~5",
        "eval('1')",
        "int('1')",
    ],
)
def test_safe_eval_rejects(expr: str) -> None:
    with pytest.raises(CalculatorError):
        safe_eval(expr)


def test_safe_eval_length_guard() -> None:
    with pytest.raises(CalculatorError):
        safe_eval("1+" * (MAX_EXPRESSION_LENGTH // 2) + "1")


def test_safe_eval_huge_int_result_is_rejected_not_hanging() -> None:
    # bit-length guard: never triggers the CPython int→str limit
    with pytest.raises(CalculatorError):
        safe_eval("factorial(1000) * factorial(1000) * factorial(1000) * factorial(1000)")


def test_calculator_never_raises_and_formats() -> None:
    calc = Calculator()
    assert calc.evaluate("2+2") == "🧮 2+2 = 4"
    assert calc.evaluate("10/4") == "🧮 10/4 = 2.5"
    assert calc.evaluate("2^20") == "🧮 2^20 = 1,048,576"
    assert calc.evaluate("1/3").startswith("🧮 1/3 = 0.3333333333")
    assert calc.evaluate("__import__('os')").startswith("❌")
    assert calc.evaluate("").startswith("❌")


@pytest.mark.asyncio
async def test_calc_cmd_replies_with_real_result() -> None:
    update = make_update()
    await surface.calc_cmd(update, make_context(["2+2"]))
    assert update.message.last == "🧮 2+2 = 4"

    update = make_update()
    await surface.calc_cmd(update, make_context(["(1", "+", "2)", "*", "3"]))
    assert update.message.last.endswith("= 9")

    update = make_update()
    await surface.calc_cmd(update, make_context([]))
    assert "استفاده" in update.message.last


# ── /tr parsing ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (["سلام", "دنیا"], ("fa", "en", "سلام دنیا")),
        (["hello", "world"], ("en", "fa", "hello world")),
        (["de", "سلام", "دنیا"], ("fa", "de", "سلام دنیا")),
        (["fa>en", "سلام"], ("fa", "en", "سلام")),
        (["en->ar", "hello"], ("en", "ar", "hello")),
        (["en:tr", "hello"], ("en", "tr", "hello")),
        ([">it", "hello"], ("en", "it", "hello")),
        (["it", "is", "fine"], ("en", "fa", "it is fine")),  # ambiguous code stays text
        (["no", "way"], ("en", "fa", "no way")),
        (["en", "hello"], ("en", "fa", "hello")),  # same src/dst → flip target
        (["fa"], None),
        ([], None),
        (["de"], None),
    ],
)
def test_parse_translate_args(raw: list[str], expected: tuple[str, str, str] | None) -> None:
    assert surface.parse_translate_args(raw) == expected


@pytest.mark.asyncio
async def test_tr_cmd_uses_translator(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = AsyncMock(return_value="Hello world")
    monkeypatch.setattr(surface._translator, "translate", fake)
    update = make_update()
    await surface.tr_cmd(update, make_context(["سلام", "دنیا"]))
    fake.assert_awaited_once_with("سلام دنیا", source="fa", target="en")
    assert update.message.last == "🌐 fa→en\nHello world"


@pytest.mark.asyncio
async def test_tr_cmd_rejects_long_text(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = AsyncMock(return_value="x")
    monkeypatch.setattr(surface._translator, "translate", fake)
    update = make_update()
    await surface.tr_cmd(update, make_context(["a" * (surface.MAX_TRANSLATE_CHARS + 1)]))
    fake.assert_not_awaited()
    assert update.message.last.startswith("❌")


@pytest.mark.asyncio
async def test_tr_cmd_without_text_shows_usage() -> None:
    update = make_update()
    await surface.tr_cmd(update, make_context([]))
    assert "استفاده" in update.message.last


# ── /convert ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (["100", "usd", "irt"], (100.0, "usd", "irt")),
        (["100usd", "irt"], (100.0, "usd", "irt")),
        (["5", "km", "to", "mile"], (5.0, "km", "mile")),
        (["۳۰", "c", "f"], (30.0, "c", "f")),
        (["1,5", "kg", "lb"], (1.5, "kg", "lb")),
        (["abc", "usd", "irt"], None),
        (["100", "usd"], None),
        ([], None),
    ],
)
def test_parse_convert_args(raw: list[str], expected: tuple[float, str, str] | None) -> None:
    assert surface.parse_convert_args(raw) == expected


@pytest.mark.asyncio
async def test_convert_cmd_uses_unit_converter() -> None:
    update = make_update()
    await surface.convert_cmd(update, make_context(["5", "km", "mile"]))
    assert update.message.last == UnitConverter().convert(5, "km", "mile")
    assert "3.1" in update.message.last

    update = make_update()
    await surface.convert_cmd(update, make_context(["100", "c", "f"]))
    assert "212" in update.message.last

    update = make_update()
    await surface.convert_cmd(update, make_context([]))
    assert "استفاده" in update.message.last


# ── Reminders ────────────────────────────────────────────────────────


def _reminder_rows(db_path: str) -> list[Reminder]:
    engine = create_engine(f"sqlite:///{db_path}")
    with Session(engine) as session:
        rows = list(session.exec(select(Reminder)).all())
    engine.dispose()
    return rows


@pytest.mark.asyncio
async def test_reminder_is_persisted_and_delivered_to_chat(feature_db, fake_bot: FakeBot) -> None:
    system = ReminderSystem(fake_bot)
    msg = await system.set_reminder(user_id=1, chat_id=-500, time_str="1s", text="نماز")
    assert msg.startswith("✅ یادآوری #")
    assert system.scheduled_count == 1
    assert [r["text"] for r in system.list_pending(1)] == ["نماز"]

    await asyncio.sleep(1.3)
    assert fake_bot.sent == [{"chat_id": -500, "text": "⏰ یادآوری: نماز"}]
    rows = _reminder_rows(feature_db.db_path)
    assert [r.status for r in rows] == ["sent"]
    assert system.list_pending(1) == []
    assert system.scheduled_count == 0


@pytest.mark.asyncio
async def test_reminder_validation(feature_db, fake_bot: FakeBot) -> None:
    system = ReminderSystem(fake_bot)
    assert (await system.set_reminder(1, 1, "soon", "x")).startswith("❌")
    assert (await system.set_reminder(1, 1, "0m", "x")).startswith("❌")
    assert (await system.set_reminder(1, 1, "31d", "x")).startswith("❌")
    assert (await system.set_reminder(1, 1, "5m", "   ")).startswith("❌")
    assert _reminder_rows(feature_db.db_path) == []


@pytest.mark.asyncio
async def test_reminder_cancel_only_by_owner(feature_db, fake_bot: FakeBot) -> None:
    system = ReminderSystem(fake_bot)
    await system.set_reminder(1, 1, "1h", "a")
    rid = system.list_pending(1)[0]["id"]
    assert system.cancel(2, rid) is False
    assert system.cancel(1, rid) is True
    assert system.cancel(1, rid) is False
    assert system.list_pending(1) == []
    await asyncio.sleep(0)
    assert system.scheduled_count == 0
    assert [r.status for r in _reminder_rows(feature_db.db_path)] == ["cancelled"]


@pytest.mark.asyncio
async def test_restore_pending_rearms_and_delivers_overdue(feature_db, fake_bot: FakeBot) -> None:
    engine = create_engine(f"sqlite:///{feature_db.db_path}")
    with Session(engine) as session:
        session.add(
            Reminder(
                user_id=7,
                chat_id=77,
                text="overdue",
                remind_at=datetime.now(timezone.utc) - timedelta(minutes=5),
                status="pending",
            )
        )
        session.add(
            Reminder(
                user_id=7,
                chat_id=77,
                text="later",
                remind_at=datetime.now(timezone.utc) + timedelta(hours=1),
                status="pending",
            )
        )
        session.commit()
    engine.dispose()

    system = ReminderSystem(fake_bot)
    assert await system.restore_pending() == 2
    assert await system.restore_pending() == 0  # already armed
    await asyncio.sleep(0.2)
    assert [m["text"] for m in fake_bot.sent] == ["⏰ یادآوری: overdue"]
    assert [r["text"] for r in system.list_pending(7)] == ["later"]
    system.cancel(7, system.list_pending(7)[0]["id"])


@pytest.mark.asyncio
async def test_remind_and_reminders_cmds(feature_db, fake_bot: FakeBot) -> None:
    surface.reset_reminder_system()
    update = make_update(user_id=5, chat_id=-42)
    await surface.remind_cmd(update, make_context(["۲h", "جلسه"], bot=fake_bot))
    assert update.message.last.startswith("✅ یادآوری #")
    assert surface.get_reminder_system().bot is fake_bot

    update = make_update(user_id=5, chat_id=-42)
    await surface.reminders_cmd(update, make_context([]))
    assert "جلسه" in update.message.last

    rid = surface.get_reminder_system().list_pending(5)[0]["id"]
    update = make_update(user_id=5, chat_id=-42)
    await surface.reminders_cmd(update, make_context(["cancel", str(rid)]))
    assert update.message.last == f"🗑️ یادآوری #{rid} لغو شد."

    update = make_update(user_id=5, chat_id=-42)
    await surface.reminders_cmd(update, make_context([]))
    assert update.message.last == "⏰ یادآوری فعالی ندارید."

    update = make_update(user_id=5)
    await surface.remind_cmd(update, make_context(["30m"]))
    assert "استفاده" in update.message.last
