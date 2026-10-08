"""Behavioural tests for the channel-management surface.

``bot/handlers.py`` used to answer these seven commands with literals that
announced their own falseness::

    "✅ Post sent to channel (simulated)."
    "🚫 User banned (simulated)."
    "📊 Group stats: 150 members, 1.2k messages/day."
    "👋 Welcome message updated."

``ChannelManager`` — the module that actually calls the Bot API — had no
importer. So the operator was told a post had gone out and a user had been
banned, when nothing had happened; and ``/ban`` answered *any* user that way.

Each test asserts against something observable: the call the bot double
recorded, the row the database holds, or the error the double raises. Nothing
here passes if a handler falls back to a fixed string.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlmodel import Session, SQLModel, select
from surface_fakes import FakeBot, FakeMessage, make_context, make_update

from nexus_ai_agent.bot.surface import channel_management as surface
from nexus_ai_agent.config import settings as settings_module
from nexus_ai_agent.config.settings import get_settings
from nexus_ai_agent.features import owner_control
from nexus_ai_agent.features.channel_manager import ChannelManager
from nexus_ai_agent.storage import models as _models  # noqa: F401  (registers tables)
from nexus_ai_agent.storage.models import ChannelSchedule, WelcomeMessage

OWNER = 4242
INTRUDER = 99
CHAT = 10

#: The literals this batch removed, verbatim.
STUB_STRINGS = (
    "Post sent to channel (simulated).",
    "Post scheduled (simulated).",
    "User banned (simulated).",
    "User unbanned (simulated).",
    "Group stats: 150 members, 1.2k messages/day.",
    "Welcome message updated.",
    "Message pinned.",
)


@pytest.fixture()
def db(settings_override: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """A temp SQLite database with the schema, and a configured owner."""
    monkeypatch.setenv("NEXUS_OWNER_TELEGRAM_ID", str(OWNER))
    settings_module.get_settings.cache_clear()
    owner_control._owner_id = None  # type: ignore[attr-defined]

    settings = get_settings()
    engine = create_engine(f"sqlite:///{settings.db_path}")
    SQLModel.metadata.create_all(engine)
    engine.dispose()
    yield settings
    owner_control._owner_id = None  # type: ignore[attr-defined]


def _harness(args: list[str] | None = None, *, bot: FakeBot | None = None) -> Any:
    """Return ``(bot_double, context)`` with a caller-owned bot.

    Assertions read the bot directly rather than the manager, so a denied
    command — which must never construct a manager — can still be checked.
    """
    bot = bot or FakeBot()
    return bot, make_context(list(args or []), bot=bot)


def _manager(context: Any) -> ChannelManager:
    manager = context.application.bot_data[surface.MANAGER_KEY]
    assert isinstance(manager, ChannelManager)
    return manager


def _cancel_pending(manager: ChannelManager) -> None:
    """Drop the asyncio task ``schedule_post`` armed for the future deadline."""
    for task in list(manager._scheduled_tasks.values()):
        task.cancel()
    manager._scheduled_tasks.clear()


def _rows(model: type) -> list[Any]:
    settings = get_settings()
    with Session(create_engine(f"sqlite:///{settings.db_path}")) as session:
        return list(session.exec(select(model)).all())


# ── manager_for: one instance per application, bound to the live bot ──────


def test_manager_for_returns_none_without_a_bot() -> None:
    assert surface.manager_for(make_context([], bot=None)) is None


def test_manager_for_memoises_one_manager(db: Any) -> None:
    bot, context = _harness()

    first = surface.manager_for(context)
    second = surface.manager_for(context)

    assert first is second
    assert first is not None and first.bot is bot
    assert context.application.bot_data[surface.MANAGER_KEY] is first


def test_manager_for_binds_a_manager_created_without_a_bot(db: Any) -> None:
    bot, _ = _harness()
    unbound = ChannelManager(None)
    context = make_context([], bot=bot, **{surface.MANAGER_KEY: unbound})

    assert surface.manager_for(context) is unbound
    assert unbound.bot is bot


# ── /post ─────────────────────────────────────────────────────────────────


async def test_post_sends_through_the_bot_and_reports_the_size(db: Any) -> None:
    update = make_update(user_id=OWNER, chat_id=CHAT)
    bot, context = _harness(["📣", "اعلام", "نسخه"])

    await surface.post_cmd(update, context)

    assert bot.sent == [(CHAT, "📣 اعلام نسخه")]
    assert "ارسال شد" in update.last_reply
    assert f"{len('📣 اعلام نسخه')} نویسه" in update.last_reply


async def test_post_can_pin_in_the_same_call(db: Any) -> None:
    update = make_update(user_id=OWNER, chat_id=CHAT)
    bot, context = _harness(["--pin", "پین‌شونده"])

    await surface.post_cmd(update, context)

    assert bot.sent == [(CHAT, "پین‌شونده")]
    assert "سنجاق" in update.last_reply


async def test_post_is_owner_only_and_sends_nothing(db: Any) -> None:
    update = make_update(user_id=INTRUDER, chat_id=CHAT)
    bot, context = _harness(["متن"])

    await surface.post_cmd(update, context)

    assert "مالک" in update.last_reply
    assert bot.sent == []
    assert surface.MANAGER_KEY not in context.application.bot_data


async def test_post_without_text_shows_usage(db: Any) -> None:
    update = make_update(user_id=OWNER, chat_id=CHAT)
    bot, context = _harness(["--pin"])

    await surface.post_cmd(update, context)

    assert update.last_reply.startswith("❌")
    assert bot.sent == []


async def test_post_reports_a_real_failure_instead_of_claiming_success(db: Any) -> None:
    """The removed stub said "sent" no matter what; this says what happened."""
    update = make_update(user_id=OWNER, chat_id=CHAT)
    _, context = _harness(["متن"], bot=FakeBot(raises=RuntimeError("CHAT_ADMIN_REQUIRED")))

    await surface.post_cmd(update, context)

    assert "⚠️ ارسال ناموفق" in update.last_reply
    assert "CHAT_ADMIN_REQUIRED" in update.last_reply


# ── /pin ──────────────────────────────────────────────────────────────────


async def test_pin_pins_the_quoted_message(db: Any) -> None:
    update = make_update(user_id=OWNER, chat_id=CHAT, reply_to=FakeMessage("a", message_id=421))
    bot, context = _harness()

    await surface.pin_cmd(update, context)

    assert bot.pinned == [(CHAT, 421)]
    assert "📌 پیام 421 سنجاق شد" in update.last_reply


async def test_pin_accepts_an_explicit_message_id(db: Any) -> None:
    update = make_update(user_id=OWNER, chat_id=CHAT)
    bot, context = _harness(["77"])

    await surface.pin_cmd(update, context)

    assert bot.pinned == [(CHAT, 77)]
    assert "سنجاق شد" in update.last_reply


async def test_pin_rejects_a_non_numeric_id(db: Any) -> None:
    update = make_update(user_id=OWNER, chat_id=CHAT)
    bot, context = _harness(["forty-two"])

    await surface.pin_cmd(update, context)

    assert "باید عدد باشد" in update.last_reply
    assert bot.pinned == []


async def test_pin_without_a_quoted_message_asks_for_an_id(db: Any) -> None:
    update = make_update(user_id=OWNER, chat_id=CHAT)
    bot, context = _harness()

    await surface.pin_cmd(update, context)

    assert "/pin 421" in update.last_reply
    assert bot.pinned == []


async def test_pin_is_owner_only(db: Any) -> None:
    update = make_update(user_id=INTRUDER, chat_id=CHAT)
    bot, context = _harness(["77"])

    await surface.pin_cmd(update, context)

    assert "مالک" in update.last_reply
    assert bot.pinned == []


# ── /ban · /unban ─────────────────────────────────────────────────────────


async def test_ban_calls_the_api_and_reports_success(db: Any) -> None:
    update = make_update(user_id=OWNER, chat_id=CHAT)
    bot, context = _harness(["5150", "اسپم"])

    await surface.ban_cmd(update, context)

    assert bot.banned == [(CHAT, 5150)]
    assert "✅ بن انجام شد" in update.last_reply
    assert "5150" in update.last_reply


async def test_ban_without_rights_is_reported_as_a_failure(db: Any) -> None:
    """``ChannelManager.ban_user`` swallows the API error into ``False``."""
    update = make_update(user_id=OWNER, chat_id=CHAT)
    bot, context = _harness(["5150"], bot=FakeBot(raises=RuntimeError("not enough rights")))

    await surface.ban_cmd(update, context)

    assert "⚠️ بن انجام نشد" in update.last_reply
    assert "دسترسی مدیریتی" in update.last_reply
    assert bot.banned == []


async def test_ban_is_owner_only(db: Any) -> None:
    update = make_update(user_id=INTRUDER, chat_id=CHAT)
    bot, context = _harness(["5150"])

    await surface.ban_cmd(update, context)

    assert "مالک" in update.last_reply
    assert bot.banned == []


async def test_ban_targets_the_quoted_author_when_no_id_is_given(db: Any) -> None:
    update = make_update(user_id=OWNER, chat_id=CHAT, reply_to=FakeMessage("spam", author_id=777))
    bot, context = _harness()

    await surface.ban_cmd(update, context)

    assert bot.banned == [(CHAT, 777)]
    assert "777" in update.last_reply


async def test_ban_with_no_target_at_all_is_refused(db: Any) -> None:
    update = make_update(user_id=OWNER, chat_id=CHAT)
    bot, context = _harness()

    await surface.ban_cmd(update, context)

    assert "کاربر" in update.last_reply
    assert bot.banned == []


async def test_unban_reports_the_api_result(db: Any) -> None:
    update = make_update(user_id=OWNER, chat_id=CHAT)
    bot, context = _harness(["5150"])

    await surface.unban_cmd(update, context)

    assert bot.unbanned == [(CHAT, 5150)]
    assert "✅ رفع بن انجام شد" in update.last_reply


# ── /stats ────────────────────────────────────────────────────────────────


async def test_stats_reads_the_live_member_count(db: Any) -> None:
    update = make_update(user_id=INTRUDER, chat_id=CHAT)
    bot, context = _harness(bot=FakeBot(member_count=7))

    await surface.stats_cmd(update, context)

    assert "اعضا: 7" in update.last_reply
    assert "1.2k" not in update.last_reply
    assert "اندازه‌گیری نمی‌شود" in update.last_reply
    assert bot.member_count == 7


async def test_stats_is_readable_by_anyone_but_honest_on_failure(db: Any) -> None:
    """``/stats`` is not owner-gated (it reads one count); an API error is shown."""
    update = make_update(user_id=INTRUDER, chat_id=CHAT)
    _, context = _harness([], bot=FakeBot(raises=RuntimeError("bad chat")))

    await surface.stats_cmd(update, context)

    assert "⚠️ شمارش اعضا ناموفق" in update.last_reply


# ── /welcome ──────────────────────────────────────────────────────────────


async def test_welcome_persists_the_message_for_the_chat(db: Any) -> None:
    update = make_update(user_id=OWNER, chat_id=CHAT)
    _, context = _harness(["👋", "خوش", "آمدی", "{name}"])

    await surface.welcome_cmd(update, context)

    rows = _rows(WelcomeMessage)
    assert [row.text for row in rows] == ["👋 خوش آمدی {name}"]
    assert rows[0].chat_id == CHAT
    assert "ذخیره شد" in update.last_reply


async def test_welcome_read_back_comes_from_the_database_not_the_cache(db: Any) -> None:
    writer = make_update(user_id=OWNER, chat_id=CHAT)
    _, write_context = _harness(["👋 سلام {name}"])
    await surface.welcome_cmd(writer, write_context)

    reader = make_update(user_id=OWNER, chat_id=CHAT)
    _, read_context = _harness()  # a fresh manager: an empty welcome cache
    await surface.welcome_cmd(reader, read_context)

    assert "👋 سلام {name}" in reader.last_reply
    assert reader.last_reply != writer.last_reply


async def test_welcome_says_when_nothing_is_configured(db: Any) -> None:
    update = make_update(user_id=OWNER, chat_id=CHAT)
    _, context = _harness()

    await surface.welcome_cmd(update, context)

    assert "تنظیم نشده" in update.last_reply


async def test_welcome_update_replaces_the_row_instead_of_adding_one(db: Any) -> None:
    first = make_update(user_id=OWNER, chat_id=CHAT)
    await surface.welcome_cmd(first, _harness(["پسند اول"])[1])
    second = make_update(user_id=OWNER, chat_id=CHAT)
    await surface.welcome_cmd(second, _harness(["پسند دوم"])[1])

    assert [row.text for row in _rows(WelcomeMessage)] == ["پسند دوم"]


# ── /schedule ─────────────────────────────────────────────────────────────


async def test_schedule_persists_a_pending_row_with_the_utc_deadline(db: Any) -> None:
    update = make_update(user_id=OWNER, chat_id=CHAT)
    # minute precision, because that is what /schedule accepts
    when = (datetime.now(timezone.utc) + timedelta(days=3)).replace(second=0, microsecond=0)
    bot, context = _harness(
        [when.strftime("%Y-%m-%d"), when.strftime("%H:%M"), "📣", "اعلام", "نسخه"]
    )

    await surface.schedule_cmd(update, context)

    rows = _rows(ChannelSchedule)
    assert len(rows) == 1
    assert rows[0].status == "pending"
    assert rows[0].chat_id == CHAT
    assert rows[0].text == "📣 اعلام نسخه"
    # SQLite's DateTime column round-trips naive (the model has no timezone
    # flag), so the row must be compared to the same wall time without the UTC
    # marker. A future delivery tick therefore has to *treat* it as UTC — the
    # caveat recorded for task-159 in docs/DECISION_LOG.md D-0009.
    assert rows[0].scheduled_at == when.replace(tzinfo=None)
    assert f"شناسه: {rows[0].id}" in update.last_reply
    assert bot.sent == []  # nothing goes out before the deadline
    _cancel_pending(_manager(context))


@pytest.mark.parametrize(
    "argv,needle",
    [
        ([], "استفاده"),
        (["2026-13-45", "10:00", "متن"], "قالب زمان"),
        (["2026-01-01", "10:00", "متن"], "گذشته"),
        (["2030-01-01T10:00"], "استفاده"),
    ],
)
async def test_schedule_rejects_bad_input_with_a_specific_reason(
    db: Any, argv: list[str], needle: str
) -> None:
    update = make_update(user_id=OWNER, chat_id=CHAT)
    _, context = _harness(argv)

    await surface.schedule_cmd(update, context)

    assert needle in update.last_reply
    assert _rows(ChannelSchedule) == []


async def test_schedule_is_owner_only(db: Any) -> None:
    update = make_update(user_id=INTRUDER, chat_id=CHAT)
    now = datetime.now(timezone.utc) + timedelta(days=1)
    argv = [now.strftime("%Y-%m-%d"), now.strftime("%H:%M"), "متن"]

    await surface.schedule_cmd(update, _harness(argv)[1])

    assert "مالک" in update.last_reply
    assert _rows(ChannelSchedule) == []


def test_parse_schedule_accepts_both_separator_styles() -> None:
    # The space form arrives as two arguments, the ISO form as one.
    for argv in (["2030-05-04", "06:07", "متن"], ["2030-05-04T06:07", "متن"]):
        parsed = surface.parse_schedule_args(argv)
        assert isinstance(parsed, tuple), argv
        when, text = parsed
        assert when.tzinfo is timezone.utc
        assert (when.year, when.month, when.day, when.hour, when.minute) == (2030, 5, 4, 6, 7)
        assert text == "متن"


def test_parse_schedule_joins_a_multi_word_text() -> None:
    parsed = surface.parse_schedule_args(["2030-05-04", "06:07", "a", "b", "c"])
    assert isinstance(parsed, tuple)
    assert parsed[1] == "a b c"


def test_parse_target_id_prefers_the_argument_then_the_quote() -> None:
    quoted = make_update(user_id=OWNER, reply_to=FakeMessage("x", author_id=31))
    assert surface.parse_target_id(["12"], quoted, command="/ban") == 12
    assert surface.parse_target_id([], quoted, command="/ban") == 31
    assert isinstance(surface.parse_target_id(["x"], quoted, command="/ban"), str)
    assert surface.parse_target_id([], make_update(), command="/ban") == "❌ کاربری مشخص نشده‌است."


@pytest.mark.parametrize("stub", STUB_STRINGS)
async def test_no_command_answers_with_a_removed_stub(db: Any, stub: str) -> None:
    """Every one of the seven is exercised end to end; none may fall back."""
    now = datetime.now(timezone.utc) + timedelta(days=2)
    when = [now.strftime("%Y-%m-%d"), now.strftime("%H:%M"), "متن"]
    commands: tuple[tuple[Any, list[str]], ...] = (
        (surface.post_cmd, ["متن"]),
        (surface.schedule_cmd, when),
        (surface.pin_cmd, ["5"]),
        (surface.ban_cmd, ["5"]),
        (surface.unban_cmd, ["5"]),
        (surface.stats_cmd, []),
        (surface.welcome_cmd, ["👋"]),
    )
    bot = FakeBot()
    managers: list[ChannelManager] = []
    for command, argv in commands:
        update = make_update(user_id=OWNER, chat_id=CHAT)
        context = make_context(argv, bot=bot)
        await command(update, context)
        assert stub not in update.last_reply
        assert "simulated" not in update.last_reply
        if surface.MANAGER_KEY in context.application.bot_data:
            managers.append(_manager(context))
    for manager in managers:
        _cancel_pending(manager)
