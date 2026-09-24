"""Behavioural tests for the rewritten ReminderSystem.

Covers the bugs the previous session set out to fix:

- **originating-chat delivery**: the old code sent the reminder to
  ``chat_id=user_id`` (a user id used as a chat id). The delivery must go
  to the chat the reminder was created in.
- **bindable bot**: the bot can be attached after construction.
- **cancel support**: only the creator can cancel; the task is stopped
  and the row marked ``cancelled``.
- **restore_pending**: overdue rows are *delivered* (previously they were
  silently marked "sent" without any send), and naive/aware datetime
  comparisons from SQLite must not raise.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlmodel import Session, SQLModel, select

from nexus_ai_agent.features.tools import ReminderSystem
from nexus_ai_agent.storage import models as models_module  # noqa: F401  (registers tables)
from nexus_ai_agent.storage.models import Reminder


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str) -> None:
        self.sent.append((chat_id, text))


@pytest.fixture()
def db_path(tmp_path: Path) -> str:
    path = tmp_path / "reminders.sqlite"
    engine = create_engine(f"sqlite:///{path}")
    SQLModel.metadata.create_all(engine)
    engine.dispose()
    return str(path)


@pytest.fixture()
def reminder_sys(db_path: str) -> ReminderSystem:
    return ReminderSystem(db_path=db_path)


def rows(db_path: str) -> list[Reminder]:
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with Session(engine) as session:
            return list(session.exec(select(Reminder)).all())
    finally:
        engine.dispose()


async def test_unbound_set_fails_cleanly(reminder_sys: ReminderSystem) -> None:
    result = await reminder_sys.set_reminder(111, 222, "30m", "نماز")
    assert "در دسترس نیست" in result
    assert rows(reminder_sys._db_path) == []  # type: ignore[arg-type]


async def test_set_persists_row(reminder_sys: ReminderSystem) -> None:
    bot = FakeBot()
    reminder_sys.bind(bot)
    result = await reminder_sys.set_reminder(111, 222, "30m", "نماز")
    assert "یادآوری #" in result
    saved = rows(reminder_sys._db_path)  # type: ignore[arg-type]
    assert len(saved) == 1
    assert saved[0].user_id == 111
    assert saved[0].chat_id == 222
    assert saved[0].status == "pending"


async def test_delivers_to_originating_chat_not_user_id(reminder_sys: ReminderSystem) -> None:
    """Regression: the old code called send_message(chat_id=user_id)."""
    bot = FakeBot()
    reminder_sys.bind(bot)
    # user_id and chat_id deliberately differ and chat is a *group* id.
    await reminder_sys.set_reminder(user_id=111, chat_id=-1001234567, time_str="1s", text="آب بنوش")
    await _sleep_until(lambda: bool(bot.sent), seconds=3.0)
    assert len(bot.sent) == 1
    chat_id, text = bot.sent[0]
    assert chat_id == -1001234567  # the originating chat, NOT user 111
    assert "آب بنوش" in text

    # Delivery (in-memory send) precedes the threaded DB write by design
    # (send-then-mark), so poll for the status like the "failed" test does —
    # a single immediate read races the commit on loaded runners (PR#67 CI).
    def _marked_sent() -> bool:
        saved = rows(reminder_sys._db_path)  # type: ignore[arg-type]
        return bool(saved) and saved[0].status == "sent"

    await _sleep_until(_marked_sent, seconds=3.0)
    saved = rows(reminder_sys._db_path)  # type: ignore[arg-type]
    assert saved[0].status == "sent"


async def test_cancel_by_non_owner_rejected(reminder_sys: ReminderSystem) -> None:
    bot = FakeBot()
    reminder_sys.bind(bot)
    await reminder_sys.set_reminder(111, 222, "1h", "جلسه")
    saved = rows(reminder_sys._db_path)  # type: ignore[arg-type]
    rid = saved[0].id
    result = await reminder_sys.cancel_reminder(rid, user_id=999)
    assert "مجوز" in result
    assert rows(reminder_sys._db_path)[0].status == "pending"  # type: ignore[arg-type]
    await asyncio.sleep(0.3)  # a 1h reminder must not fire, delivered or not
    assert bot.sent == []


async def test_cancel_by_owner_stops_delivery(reminder_sys: ReminderSystem) -> None:
    bot = FakeBot()
    reminder_sys.bind(bot)
    await reminder_sys.set_reminder(111, 222, "1h", "جلسه")
    saved = rows(reminder_sys._db_path)  # type: ignore[arg-type]
    rid = saved[0].id
    result = await reminder_sys.cancel_reminder(rid, user_id=111)
    assert "لغو شد" in result
    assert rows(reminder_sys._db_path)[0].status == "cancelled"  # type: ignore[arg-type]
    await _sleep_until(lambda: bool(bot.sent), seconds=1.0, expect=False)
    assert bot.sent == []


async def test_list_reminders_shows_pending(reminder_sys: ReminderSystem) -> None:
    reminder_sys.bind(FakeBot())
    await reminder_sys.set_reminder(111, 222, "2h", "دارو")
    listing = await reminder_sys.list_reminders(111)
    assert "دارو" in listing
    assert "cancel_remind" in listing
    assert "📭" not in listing
    empty = await reminder_sys.list_reminders(333)
    assert "📭" in empty


async def test_restore_delivers_overdue_immediately(reminder_sys: ReminderSystem) -> None:
    """Overdue rows must be SENT, not silently marked 'sent' (old bug)."""
    bot = FakeBot()
    engine = create_engine(f"sqlite:///{reminder_sys._db_path}")  # type: ignore[arg-type]
    with Session(engine) as session:
        session.add(
            Reminder(
                user_id=5,
                chat_id=6,
                text="overdue",
                remind_at=datetime.now(timezone.utc) - timedelta(minutes=5),
                status="pending",
            )
        )
        session.commit()
    engine.dispose()
    reminder_sys.bind(bot)
    count = await reminder_sys.restore_pending()
    assert count == 1
    await _sleep_until(lambda: bool(bot.sent), seconds=3.0)
    assert bot.sent == [(6, "⏰ یادآوری: overdue")]


async def test_restore_reschedules_future(reminder_sys: ReminderSystem) -> None:
    bot = FakeBot()
    engine = create_engine(f"sqlite:///{reminder_sys._db_path}")  # type: ignore[arg-type]
    with Session(engine) as session:
        session.add(
            Reminder(
                user_id=5,
                chat_id=6,
                text="later",
                remind_at=datetime.now(timezone.utc) + timedelta(hours=1),
                status="pending",
            )
        )
        session.commit()
    engine.dispose()
    reminder_sys.bind(bot)
    count = await reminder_sys.restore_pending()
    assert count == 1
    # A task is scheduled; nothing is delivered yet.
    assert len(reminder_sys._tasks) == 1
    await _sleep_until(lambda: bool(bot.sent), seconds=1.0, expect=False)
    assert bot.sent == []


async def test_send_failure_marks_failed(reminder_sys: ReminderSystem, monkeypatch: Any) -> None:
    class FailingBot(FakeBot):
        async def send_message(self, chat_id: int, text: str) -> None:  # type: ignore[override]
            raise RuntimeError("telegram down")

    reminder_sys.bind(FailingBot())
    reminder_sys.SEND_TIMEOUT_SECONDS = 5.0
    await reminder_sys.set_reminder(111, 222, "1s", "x")

    def _failed() -> bool:
        saved = rows(reminder_sys._db_path)  # type: ignore[arg-type]
        return bool(saved) and saved[0].status == "failed"

    await _sleep_until(_failed, seconds=3.0)
    assert rows(reminder_sys._db_path)[0].status == "failed"  # type: ignore[arg-type]


async def _sleep_until(predicate, seconds: float, expect: bool = True) -> None:
    import asyncio

    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate() == expect:
            return
        await asyncio.sleep(0.05)
    if expect:
        pytest.fail(f"condition not met within {seconds}s")
