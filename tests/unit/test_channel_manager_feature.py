"""Contract tests for channel commands (task-160).

Destructive commands are owner-gated. Schedules persist and a restart restores
them at most once. ``/post`` targets the chat the command was issued in.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine
from sqlmodel import Session, SQLModel

from nexus_ai_agent.bot.feature_handlers import (
    build_feature_command_handlers,
    build_feature_engines,
)
from nexus_ai_agent.config import settings as settings_module
from nexus_ai_agent.config.settings import Settings
from nexus_ai_agent.features import owner_control
from nexus_ai_agent.features.channel_manager import (
    ChannelValidationError,
    parse_schedule_when,
)
from nexus_ai_agent.storage import models as models_module  # noqa: F401
from nexus_ai_agent.storage.models import ChannelSchedule, WelcomeMessage

OWNER = 100
CHAT = -1001


class FakeBot:
    def __init__(self, *, fail_send: bool = False, fail_pin: bool = False) -> None:
        self.sent: list[tuple[int, str]] = []
        self.pinned: list[tuple[int, int]] = []
        self.banned: list[tuple[int, int]] = []
        self.unbanned: list[tuple[int, int]] = []
        self.fail_send = fail_send
        self.fail_pin = fail_pin
        self.member_count = 42

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> Any:
        del kwargs
        if self.fail_send:
            raise RuntimeError("send failed")
        self.sent.append((chat_id, text))
        return SimpleNamespace(message_id=len(self.sent))

    async def pin_chat_message(self, chat_id: int, message_id: int, **kwargs: Any) -> None:
        del kwargs
        if self.fail_pin:
            raise RuntimeError("pin failed")
        self.pinned.append((chat_id, message_id))

    async def ban_chat_member(self, chat_id: int, user_id: int, **kwargs: Any) -> None:
        del kwargs
        self.banned.append((chat_id, user_id))

    async def unban_chat_member(self, chat_id: int, user_id: int, **kwargs: Any) -> None:
        del kwargs
        self.unbanned.append((chat_id, user_id))

    async def get_chat_member_count(self, chat_id: int) -> int:
        del chat_id
        return self.member_count


def make_update(
    user_id: int = OWNER,
    chat_id: int = CHAT,
    *,
    reply_to: int | None = None,
    members: list[Any] | None = None,
) -> SimpleNamespace:
    reply = SimpleNamespace(message_id=reply_to) if reply_to is not None else None
    message = SimpleNamespace(
        reply_text=AsyncMock(),
        reply_to_message=reply,
        new_chat_members=members,
    )
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=chat_id),
        message=message,
        edited_message=None,
        callback_query=None,
    )


def make_context(args: list[str] | None = None, bot: Any = None) -> SimpleNamespace:
    return SimpleNamespace(args=args or [], bot=bot)


def reply_text(update: SimpleNamespace) -> str:
    return str(update.message.reply_text.call_args.args[0])


@pytest.fixture()
def env(tmp_path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    db_file = str(tmp_path / "channel.sqlite")
    engine = create_engine(f"sqlite:///{db_file}")
    SQLModel.metadata.create_all(engine)
    engine.dispose()
    monkeypatch.setenv("NEXUS_DB_PATH", db_file)
    monkeypatch.setenv("NEXUS_OWNER_TELEGRAM_ID", str(OWNER))
    settings_module.get_settings.cache_clear()
    owner_control._owner_id = None
    settings = Settings().model_copy(update={"db_path": db_file, "owner_telegram_id": OWNER})
    engines = build_feature_engines(settings)
    try:
        yield SimpleNamespace(
            db=db_file,
            settings=settings,
            engines=engines,
            cmds=build_feature_command_handlers(engines, settings),
        )
    finally:
        engines.ads.close()
        engines.channel.close()
        engines.onboarding.close()
        owner_control._owner_id = None
        settings_module.get_settings.cache_clear()


def _schedules(db: str) -> list[ChannelSchedule]:
    engine = create_engine(f"sqlite:///{db}")
    with Session(engine) as session:
        rows = list(session.query(ChannelSchedule).all())
    engine.dispose()
    return rows


async def test_post_targets_current_chat_not_hardcoded_channel(env: SimpleNamespace) -> None:
    bot = FakeBot()
    update = make_update()
    await env.cmds["post"](update, make_context(["سلام", "گروه"], bot=bot))
    assert bot.sent == [(CHAT, "سلام گروه")]
    assert CHAT != env.engines.channel.channel_id
    assert "ارسال شد" in reply_text(update)
    assert "simulated" not in reply_text(update).lower()


async def test_non_owner_post_ban_welcome_do_not_touch_telegram(env: SimpleNamespace) -> None:
    bot = FakeBot()
    for name, args in (("post", ["hi"]), ("ban", ["5"]), ("welcome", ["hello {name}"])):
        update = make_update(user_id=7)
        await env.cmds[name](update, make_context(args, bot=bot))
        assert reply_text(update) == "⛔ Access denied"
    assert bot.sent == []
    assert bot.banned == []
    assert _schedules(env.db) == []


async def test_empty_and_invalid_commands_do_not_call_telegram(env: SimpleNamespace) -> None:
    bot = FakeBot()
    empty = make_update()
    await env.cmds["post"](empty, make_context([], bot=bot))
    assert reply_text(empty).startswith("❌")
    bad_ban = make_update()
    await env.cmds["ban"](bad_ban, make_context(["-3"], bot=bot))
    assert reply_text(bad_ban).startswith("❌")
    bad_pin = make_update()
    await env.cmds["pin"](bad_pin, make_context(["abc"], bot=bot))
    assert reply_text(bad_pin).startswith("❌")
    assert bot.sent == []
    assert bot.banned == []
    assert bot.pinned == []


def test_schedule_parser_rejects_past_far_future_and_garbage() -> None:
    now = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
    ok = parse_schedule_when("2026-09-22", "12:01", now=now)
    assert ok == datetime(2026, 9, 22, 12, 1, tzinfo=timezone.utc)
    with pytest.raises(ChannelValidationError):
        parse_schedule_when("2026-09-22", "11:00", now=now)
    with pytest.raises(ChannelValidationError):
        parse_schedule_when("2026-11-22", "12:00", now=now)
    with pytest.raises(ChannelValidationError):
        parse_schedule_when("2026-13-01", "12:00", now=now)


async def test_schedule_persists_and_cancel_is_chat_scoped(env: SimpleNamespace) -> None:
    bot = FakeBot()
    when = datetime.now(timezone.utc) + timedelta(hours=2)
    update = make_update()
    await env.cmds["schedule"](
        update,
        make_context([when.strftime("%Y-%m-%d"), when.strftime("%H:%M"), "بعداً"], bot=bot),
    )
    assert "زمان‌بندی شد" in reply_text(update)
    rows = _schedules(env.db)
    assert len(rows) == 1
    assert rows[0].status == "pending"
    assert rows[0].chat_id == CHAT
    other = make_update(chat_id=-2002)
    await env.cmds["schedule_cancel"](other, make_context([str(rows[0].id)], bot=bot))
    assert "پیدا نشد" in reply_text(other)
    assert _schedules(env.db)[0].status == "pending"
    owner = make_update()
    await env.cmds["schedule_cancel"](owner, make_context([str(rows[0].id)], bot=bot))
    assert "لغو شد" in reply_text(owner)
    assert _schedules(env.db)[0].status == "cancelled"
    await env.engines.channel.shutdown()


async def test_past_schedule_writes_no_row(env: SimpleNamespace) -> None:
    bot = FakeBot()
    update = make_update()
    await env.cmds["schedule"](update, make_context(["2020-01-01", "00:00", "قدیمی"], bot=bot))
    assert reply_text(update).startswith("❌")
    assert _schedules(env.db) == []
    assert bot.sent == []


async def test_restore_delivers_overdue_once(env: SimpleNamespace) -> None:
    bot = FakeBot()
    manager = env.engines.channel
    manager.bind(bot)
    engine = create_engine(f"sqlite:///{env.db}")
    with Session(engine) as session:
        session.add(
            ChannelSchedule(
                chat_id=CHAT,
                text="عقب‌افتاده",
                scheduled_at=datetime.now(timezone.utc) - timedelta(minutes=5),
                status="pending",
            )
        )
        session.commit()
    engine.dispose()
    assert await manager.restore_pending() == 1
    pending = list(manager._tasks.values())
    if pending:
        await pending[0]
    assert bot.sent == [(CHAT, "عقب‌افتاده")]
    assert await manager.restore_pending() == 0
    assert len(bot.sent) == 1
    stored = _schedules(env.db)[0]
    assert stored.status == "sent"


async def test_shutdown_cancels_future_task_without_sending(env: SimpleNamespace) -> None:
    bot = FakeBot()
    manager = env.engines.channel
    manager.bind(bot)
    when = datetime.now(timezone.utc) + timedelta(hours=3)
    schedule_id = await manager.schedule_post(CHAT, "هنوز نه", when)
    assert schedule_id in manager._tasks
    await manager.shutdown()
    assert manager._tasks == {}
    assert bot.sent == []
    assert _schedules(env.db)[0].status == "pending"
    # A new manager in the same process restores the still-pending row.
    again = type(manager)(bot=bot, db_path=env.db)
    try:
        assert await again.restore_pending() == 1
        assert schedule_id in again._tasks
    finally:
        await again.shutdown()


async def test_failed_send_is_not_retried(env: SimpleNamespace) -> None:
    bot = FakeBot(fail_send=True)
    manager = env.engines.channel
    manager.bind(bot)
    engine = create_engine(f"sqlite:///{env.db}")
    with Session(engine) as session:
        session.add(
            ChannelSchedule(
                chat_id=CHAT,
                text="خراب",
                scheduled_at=datetime.now(timezone.utc) - timedelta(minutes=1),
                status="pending",
            )
        )
        session.commit()
    engine.dispose()
    await manager.restore_pending()
    pending = list(manager._tasks.values())
    if pending:
        await pending[0]
    assert bot.sent == []
    assert _schedules(env.db)[0].status == "failed"
    assert await manager.restore_pending() == 0


async def test_welcome_substitutes_name_without_evaluating_other_braces(
    env: SimpleNamespace,
) -> None:
    bot = FakeBot()
    update = make_update()
    template = "سلام {name} {__import__('os')}"
    await env.cmds["welcome"](
        update, make_context(["سلام", "{name}", "{__import__('os')}"], bot=bot)
    )
    assert "ذخیره شد" in reply_text(update)
    member = SimpleNamespace(id=5, full_name="نگار", is_bot=False)
    joined = make_update(members=[member, SimpleNamespace(id=9, full_name="ربات", is_bot=True)])
    await env.cmds["new_member"](joined, make_context(bot=bot))
    assert bot.sent == [(CHAT, template.replace("{name}", "نگار"))]
    assert "__import__" in bot.sent[0][1]
    engine = create_engine(f"sqlite:///{env.db}")
    with Session(engine) as session:
        row = session.query(WelcomeMessage).one()
    engine.dispose()
    assert "{name}" in row.text


async def test_missing_welcome_keeps_generic_text(env: SimpleNamespace) -> None:
    bot = FakeBot()
    member = SimpleNamespace(id=5, full_name="Ali", is_bot=False)
    update = make_update(members=[member])
    await env.cmds["new_member"](update, make_context(bot=bot))
    assert bot.sent == []
    assert reply_text(update) == "Welcome Ali to the group!"


async def test_ban_unban_stats_and_pin(env: SimpleNamespace) -> None:
    bot = FakeBot()
    ban = make_update()
    await env.cmds["ban"](ban, make_context(["۴۲", "اسپم"], bot=bot))
    assert bot.banned == [(CHAT, 42)]
    assert "مسدود شد" in reply_text(ban)
    unban = make_update()
    await env.cmds["unban"](unban, make_context(["42"], bot=bot))
    assert bot.unbanned == [(CHAT, 42)]
    stats = make_update()
    await env.cmds["stats"](stats, make_context(bot=bot))
    assert reply_text(stats) == "📊 اعضای این چت: 42"
    pin = make_update(reply_to=77)
    await env.cmds["pin"](pin, make_context(bot=bot))
    assert bot.pinned == [(CHAT, 77)]
    assert "سنجاق شد" in reply_text(pin)


async def test_pin_failure_is_not_reported_as_success(env: SimpleNamespace) -> None:
    bot = FakeBot(fail_pin=True)
    update = make_update()
    await env.cmds["pin"](update, make_context(["15"], bot=bot))
    assert "ناموفق" in reply_text(update)
    assert "سنجاق شد" not in reply_text(update)


async def test_pending_cap(env: SimpleNamespace) -> None:
    manager = env.engines.channel
    when = datetime.now(timezone.utc) + timedelta(days=1)
    for _ in range(50):
        # Insert without scheduling 50 tasks; the cap is on pending rows.
        engine = create_engine(f"sqlite:///{env.db}")
        with Session(engine) as session:
            session.add(
                ChannelSchedule(chat_id=CHAT, text="x", scheduled_at=when, status="pending")
            )
            session.commit()
        engine.dispose()
        break
    # Fill the rest through the engine only once the table has one row, then
    # the remaining inserts go through the same session loop below.
    engine = create_engine(f"sqlite:///{env.db}")
    with Session(engine) as session:
        for index in range(49):
            session.add(
                ChannelSchedule(chat_id=CHAT, text=f"x{index}", scheduled_at=when, status="pending")
            )
        session.commit()
    engine.dispose()
    with pytest.raises(ChannelValidationError):
        await manager.schedule_post(CHAT, "اضافه", when)
