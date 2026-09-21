"""Behavioural tests for the commands wired in the feature-wiring batch.

The previous test suite only asserted that a ``CommandHandler`` existed for a
name; the callbacks returned hard-coded strings and nothing failed.  Every
test here drives the *real* callback and asserts on the outcome — the
calculation, the DB row, the forwarded message — so a command that goes back
to being a stub fails immediately.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlmodel import Session, SQLModel, select

from nexus_ai_agent.bot.handlers import build_handlers
from nexus_ai_agent.config import settings as settings_module
from nexus_ai_agent.config.settings import Settings
from nexus_ai_agent.features.tools import Translator
from nexus_ai_agent.presence import PresenceStore
from nexus_ai_agent.storage.models import (
    QuizScore,
    Referral,
    Reminder,
    UserXP,
)

OWNER_ID = 111
CHAT_ID = 222
USER_ID = 333
PARTNER_ID = 444


class _FakeMessage:
    def __init__(self, text: str = "") -> None:
        self.text = text
        self.replies: list[str] = []
        self.markups: list[Any] = []

    async def reply_text(self, text: str, **kwargs: Any) -> None:
        self.replies.append(text)
        self.markups.append(kwargs.get("reply_markup"))


class _FakeQuery:
    def __init__(self, data: str, user_id: int) -> None:
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self.answers: list[Any] = []
        self.edits: list[str] = []

    async def answer(self, text: str | None = None, **_kwargs: Any) -> None:
        self.answers.append(text)

    async def edit_message_text(self, text: str, **_kwargs: Any) -> None:
        self.edits.append(text)


class _FakeUpdate:
    def __init__(
        self,
        user_id: int = USER_ID,
        chat_id: int = CHAT_ID,
        text: str = "",
        query: _FakeQuery | None = None,
    ) -> None:
        self.effective_user = SimpleNamespace(id=user_id, username="tester")
        self.effective_chat = SimpleNamespace(id=chat_id)
        self.message = _FakeMessage(text)
        self.edited_message = None
        self.callback_query = query


class _FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str, **_kwargs: Any) -> None:
        self.sent.append((chat_id, text))


class _FakeGraph:
    def __init__(self) -> None:
        self.invocations = 0

    async def ainvoke(self, state: Any, **_kwargs: Any) -> dict[str, Any]:
        self.invocations += 1
        return {"response": "ai-said-this", "intent": "unknown", "tool_results": []}


@pytest.fixture()
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    db_path = tmp_path / "app.sqlite"
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("NEXUS_DB_PATH", str(db_path))
    monkeypatch.setenv("NEXUS_CHECKPOINT_PATH", str(tmp_path / "langgraph.sqlite"))
    monkeypatch.setenv("NEXUS_VECTOR_PATH", str(tmp_path / "vector.sqlite"))
    monkeypatch.setenv("NEXUS_MODEL_PATH", str(tmp_path / "missing.gguf"))
    monkeypatch.setenv("NEXUS_OWNER_TELEGRAM_ID", str(OWNER_ID))
    settings_module.get_settings.cache_clear()
    engine = create_engine(f"sqlite:///{db_path}")
    SQLModel.metadata.create_all(engine)
    engine.dispose()
    yield db_path
    settings_module.get_settings.cache_clear()


@pytest.fixture()
def bot() -> _FakeBot:
    return _FakeBot()


@pytest.fixture()
def graph() -> _FakeGraph:
    return _FakeGraph()


@pytest.fixture()
def handlers(env: Path, graph: _FakeGraph) -> list[Any]:
    return build_handlers(object(), lambda: None, Settings(), PresenceStore(), object())


def _command(handlers: list[Any], name: str) -> Any:
    for handler in handlers:
        commands = getattr(handler, "commands", None)
        if commands and name in commands:
            return handler
    raise AssertionError(f"no handler registered for /{name}")


def _callback_handlers(handlers: list[Any], prefix: str) -> list[Any]:
    return [h for h in handlers if getattr(h, "pattern", None) and h.pattern.match(prefix)]


async def _run(
    handlers: list[Any],
    name: str,
    update: _FakeUpdate,
    bot: _FakeBot,
    *,
    args: list[str] | None = None,
) -> list[str]:
    context = SimpleNamespace(
        args=args or [],
        bot=bot,
        application=SimpleNamespace(bot=bot, bot_data={}),
    )
    await _command(handlers, name).callback(update, context)
    assert update.message.replies, f"/{name} did not reply"
    return update.message.replies


def _rows(db_path: Path, model: Any) -> list[Any]:
    engine = create_engine(f"sqlite:///{db_path}")
    with Session(engine) as session:
        found = list(session.exec(select(model)).all())
    engine.dispose()
    return found


# ── utility tools ─────────────────────────────────────────────────────────


async def test_calc_evaluates_the_expression_it_is_given(
    handlers: list[Any], bot: _FakeBot
) -> None:
    replies = await _run(handlers, "calc", _FakeUpdate(), bot, args=["2+3*4"])
    assert "14" in replies[0]
    assert "2+3*4" in replies[0]


async def test_calc_supports_functions_and_rejects_garbage(
    handlers: list[Any], bot: _FakeBot
) -> None:
    assert "12" in (await _run(handlers, "calc", _FakeUpdate(), bot, args=["sqrt(144)"]))[0]
    bad = await _run(handlers, "calc", _FakeUpdate(), bot, args=["__import__('os')"])
    assert "❌" in bad[0]


async def test_calc_without_arguments_explains_usage(handlers: list[Any], bot: _FakeBot) -> None:
    replies = await _run(handlers, "calc", _FakeUpdate(), bot)
    assert "ماشین‌حساب" in replies[0]


async def test_convert_uses_the_real_conversion_table(handlers: list[Any], bot: _FakeBot) -> None:
    replies = await _run(handlers, "convert", _FakeUpdate(), bot, args=["2", "km", "m"])
    assert "2,000" in replies[0] and "m" in replies[0]
    # Not the hard-coded "100 USD = 6,000,000 IRT" stub.
    assert "6,000,000" not in replies[0]


async def test_convert_rejects_a_non_numeric_amount(handlers: list[Any], bot: _FakeBot) -> None:
    replies = await _run(handlers, "convert", _FakeUpdate(), bot, args=["abc", "usd", "irt"])
    assert "❌" in replies[0]


async def test_translate_detects_persian_and_calls_the_real_engine(
    handlers: list[Any],
    bot: _FakeBot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, str]] = []

    async def _fake_translate(self: Translator, text: str, *, source: str, target: str) -> str:
        calls.append((text, source, target))
        return "hello"

    monkeypatch.setattr(Translator, "translate", _fake_translate)
    replies = await _run(handlers, "tr", _FakeUpdate(), bot, args=["سلام", "دنیا"])
    assert calls == [("سلام دنیا", "fa", "en")]
    assert "hello" in replies[0]


async def test_translate_honours_an_explicit_target_language(
    handlers: list[Any],
    bot: _FakeBot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, str]] = []

    async def _fake_translate(self: Translator, text: str, *, source: str, target: str) -> str:
        calls.append((text, source, target))
        return "bonjour"

    monkeypatch.setattr(Translator, "translate", _fake_translate)
    await _run(handlers, "tr", _FakeUpdate(), bot, args=["fr", "hello"])
    assert calls == [("hello", "en", "fr")]


async def test_remind_persists_a_row_and_fires_the_message(
    handlers: list[Any], bot: _FakeBot, env: Path
) -> None:
    replies = await _run(handlers, "remind", _FakeUpdate(), bot, args=["1s", "نوشیدن", "آب"])
    assert "یادآوری تنظیم شد" in replies[0]

    pending = _rows(env, Reminder)
    assert len(pending) == 1
    assert pending[0].text == "نوشیدن آب"
    assert pending[0].status == "pending"

    # The scheduled task must actually deliver through the bound bot.
    await asyncio.sleep(1.2)
    assert any("نوشیدن آب" in text for _, text in bot.sent)
    assert _rows(env, Reminder)[0].status == "sent"


async def test_remind_without_arguments_explains_usage(handlers: list[Any], bot: _FakeBot) -> None:
    replies = await _run(handlers, "remind", _FakeUpdate(), bot)
    assert "/remind" in replies[0]


# ── games ─────────────────────────────────────────────────────────────────


async def test_quiz_scores_a_real_answer(handlers: list[Any], bot: _FakeBot, env: Path) -> None:
    update = _FakeUpdate()
    await _command(handlers, "quiz").callback(
        update, SimpleNamespace(args=[], bot=bot, application=SimpleNamespace(bot=bot))
    )
    markup = update.message.markups[0]
    assert markup is not None, "/quiz must offer answer buttons"

    # Answer index 0; whether it is right depends on the drawn question, so
    # read the persisted score instead of guessing.
    query = _FakeQuery("quiz_0", USER_ID)
    callback_update = _FakeUpdate(query=query)
    await _callback_handlers(handlers, "quiz_0")[0].callback(
        callback_update, SimpleNamespace(args=[], bot=bot)
    )
    assert query.edits and "امتیاز شما" in query.edits[0]
    scores = _rows(env, QuizScore)
    assert len(scores) == 1
    assert scores[0].answered == 1
    assert scores[0].score in (0, 1)


async def test_quiz_leaderboard_reports_the_real_player(
    handlers: list[Any], bot: _FakeBot, env: Path
) -> None:
    from nexus_ai_agent.features.games import QuizGame

    QuizGame().update_score(USER_ID, CHAT_ID, correct=True)
    replies = await _run(handlers, "leaderboard", _FakeUpdate(), bot)
    assert str(USER_ID) in replies[0]
    assert "UserA" not in replies[0]


async def test_number_guess_gives_directional_hints(handlers: list[Any], bot: _FakeBot) -> None:
    await _run(handlers, "guess_start", _FakeUpdate(), bot)
    router = _text_router(handlers)

    replies = await _route(router, _FakeUpdate(text="1"), bot)
    assert any("بزرگ‌تره" in r or "کوچک‌تره" in r or "آفرین" in r for r in replies)

    stop = await _run(handlers, "guess_stop", _FakeUpdate(), bot)
    assert "🛑" in stop[0]


async def test_wordle_scores_a_guess(handlers: list[Any], bot: _FakeBot) -> None:
    await _run(handlers, "wordle", _FakeUpdate(), bot)
    router = _text_router(handlers)
    replies = await _route(router, _FakeUpdate(text="کتابخ"), bot)
    assert replies and any(mark in replies[0] for mark in ("🟩", "🟨", "⬛"))


async def test_wordle_stop_ends_the_game(handlers: list[Any], bot: _FakeBot) -> None:
    await _run(handlers, "wordle", _FakeUpdate(), bot)
    replies = await _run(handlers, "wordle_stop", _FakeUpdate(), bot)
    assert "🛑" in replies[0]


async def test_poll_counts_one_vote_per_user(handlers: list[Any], bot: _FakeBot) -> None:
    update = _FakeUpdate()
    await _run_command_with_markup(handlers, "poll", update, bot, ["سوال؟", "|", "الف", "|", "ب"])
    markup = update.message.markups[0]
    assert markup is not None

    poll_handler = _callback_handlers(handlers, "poll:")[0]
    first = _FakeUpdate(query=_FakeQuery(_button_data(markup, 0), USER_ID))
    await poll_handler.callback(first, SimpleNamespace(args=[], bot=bot))
    assert "۱" in first.callback_query.edits[0] or "1" in first.callback_query.edits[0]

    # Same user, second option → rejected, and the tally does not move.
    second = _FakeUpdate(query=_FakeQuery(_button_data(markup, 1), USER_ID))
    await poll_handler.callback(second, SimpleNamespace(args=[], bot=bot))
    assert second.callback_query.answers and "ثبت نشد" in str(second.callback_query.answers[0])

    other = _FakeUpdate(query=_FakeQuery(_button_data(markup, 1), PARTNER_ID))
    await poll_handler.callback(other, SimpleNamespace(args=[], bot=bot))
    assert "مجموع آرا: 2" in other.callback_query.edits[0]


async def test_poll_without_enough_parts_explains_usage(handlers: list[Any], bot: _FakeBot) -> None:
    replies = await _run(handlers, "poll", _FakeUpdate(), bot, args=["فقط", "سوال"])
    assert "/poll" in replies[0]


# ── referral deep link ────────────────────────────────────────────────────


async def test_start_with_a_referral_code_records_the_referral(
    handlers: list[Any], bot: _FakeBot, env: Path
) -> None:
    from nexus_ai_agent.features.referral import ReferralEngine

    engine = ReferralEngine(db_path=str(env))
    code = engine.get_or_create_code(OWNER_ID)

    replies = await _run(handlers, "start", _FakeUpdate(), bot, args=[f"ref_{code}"])
    assert "دعوت شما ثبت شد" in replies[0]

    referrals = _rows(env, Referral)
    assert len(referrals) == 1
    assert referrals[0].referrer_id == OWNER_ID
    assert referrals[0].referee_id == USER_ID
    assert referrals[0].status == "completed"

    # The referee's XP was booked for real.
    xp = _rows(env, UserXP)
    assert any(row.user_id == USER_ID and row.xp >= 50 for row in xp)


async def test_start_with_an_unknown_code_falls_back_to_the_welcome(
    handlers: list[Any], bot: _FakeBot
) -> None:
    replies = await _run(handlers, "start", _FakeUpdate(), bot, args=["ref_DOES-NOT-EXIST"])
    assert "Welcome to NEXUS AI Agent" in replies[0]


async def test_there_is_exactly_one_start_handler(handlers: list[Any]) -> None:
    """Two registrations meant the second one was dead code in PTB."""
    starts = [h for h in handlers if getattr(h, "commands", None) == {"start"}]
    assert len(starts) == 1


async def test_self_referral_is_not_rewarded(handlers: list[Any], bot: _FakeBot, env: Path) -> None:
    from nexus_ai_agent.features.referral import ReferralEngine

    code = ReferralEngine(db_path=str(env)).get_or_create_code(USER_ID)
    replies = await _run(handlers, "start", _FakeUpdate(), bot, args=[f"ref_{code}"])
    assert "Welcome to NEXUS AI Agent" in replies[0]
    assert _rows(env, Referral) == []


# ── gamification ──────────────────────────────────────────────────────────


async def test_daily_reward_claims_once_per_day(
    handlers: list[Any], bot: _FakeBot, env: Path
) -> None:
    first = await _run(handlers, "daily", _FakeUpdate(), bot)
    assert "جایزه روزانه دریافت شد" in first[0]
    second = await _run(handlers, "daily", _FakeUpdate(), bot)
    assert "قبلاً گرفته‌اید" in second[0]
    assert any(row.user_id == USER_ID for row in _rows(env, UserXP))


async def test_xp_leaderboard_shows_real_players(
    handlers: list[Any], bot: _FakeBot, env: Path
) -> None:
    await _run(handlers, "daily", _FakeUpdate(), bot)
    replies = await _run(handlers, "xp_leaderboard", _FakeUpdate(), bot)
    assert str(USER_ID) in replies[0]
    assert "UserX" not in replies[0]


# ── anonymous chat ────────────────────────────────────────────────────────


async def test_anonymous_chat_delivers_messages_between_partners(
    handlers: list[Any], bot: _FakeBot
) -> None:
    first = _FakeUpdate(user_id=USER_ID)
    await _run_command_with_markup(handlers, "anon_start", first, bot, [])
    assert "صف انتظار" in first.message.replies[0]

    second = _FakeUpdate(user_id=PARTNER_ID)
    await _run_command_with_markup(handlers, "anon_start", second, bot, [])
    assert "وصل شدید" in second.message.replies[0]
    assert any(chat_id == USER_ID for chat_id, _ in bot.sent)

    router = _text_router(handlers)
    await _route(router, _FakeUpdate(user_id=USER_ID, text="سلام ناشناس"), bot)
    assert (PARTNER_ID, "📩 پیام ناشناس:\nسلام ناشناس") in bot.sent

    stop = _FakeUpdate(user_id=PARTNER_ID)
    await _run_command_with_markup(handlers, "anon_stop", stop, bot, [])
    assert "پایان یافت" in stop.message.replies[0]


async def test_plain_text_still_reaches_the_ai_when_no_lane_owns_it(
    handlers: list[Any], bot: _FakeBot, graph: _FakeGraph
) -> None:
    from nexus_ai_agent.storage.db import get_session

    real_handlers = build_handlers(graph, get_session, Settings(), PresenceStore(), object())
    router = _text_router(real_handlers)
    replies = await _route(router, _FakeUpdate(user_id=OWNER_ID, text="just chatting"), bot)
    assert graph.invocations == 1
    assert "ai-said-this" in replies[0]


async def test_plain_text_from_an_unlisted_user_is_refused(
    handlers: list[Any], bot: _FakeBot, graph: _FakeGraph
) -> None:
    """The owner-only deployment gate also holds on the free-text path."""
    real_handlers = build_handlers(graph, lambda: None, Settings(), PresenceStore(), object())
    router = _text_router(real_handlers)
    replies = await _route(router, _FakeUpdate(user_id=USER_ID, text="just chatting"), bot)
    assert "Access denied" in replies[0]
    assert graph.invocations == 0


# ── async session API regression (P0: ``session.exec`` on AsyncSession) ──


async def test_myfiles_reads_through_the_real_session_factory(
    env: Path, bot: _FakeBot, graph: _FakeGraph
) -> None:
    """``get_session`` yields a plain AsyncSession: SQLModel's .exec() crashed."""
    from nexus_ai_agent.bot.handlers import build_handlers as _build
    from nexus_ai_agent.storage.db import get_session
    from nexus_ai_agent.storage.models import CloudFile

    engine = create_engine(f"sqlite:///{env}")
    with Session(engine) as session:
        session.add(
            CloudFile(
                user_id=OWNER_ID,
                file_name="report.pdf",
                provider="dropbox",
                remote_path="remote/report.pdf",
                file_size=2048,
            )
        )
        session.commit()
    engine.dispose()

    real_handlers = _build(graph, get_session, Settings(), PresenceStore(), object())
    update = _FakeUpdate(user_id=OWNER_ID)
    await _run_command_with_markup(real_handlers, "myfiles", update, bot, [])
    assert "report.pdf" in update.message.replies[0]


async def test_language_command_persists_the_choice(
    env: Path, bot: _FakeBot, graph: _FakeGraph
) -> None:
    from nexus_ai_agent.bot.handlers import build_handlers as _build
    from nexus_ai_agent.storage.db import get_session
    from nexus_ai_agent.storage.models import UserLanguage

    real_handlers = _build(graph, get_session, Settings(), PresenceStore(), object())
    update = _FakeUpdate(user_id=OWNER_ID)
    await _run_command_with_markup(real_handlers, "language", update, bot, ["fa"])
    assert "Language set to" in update.message.replies[0]

    engine = create_engine(f"sqlite:///{env}")
    with Session(engine) as session:
        rows = list(session.exec(select(UserLanguage)).all())
    engine.dispose()
    assert [row.language for row in rows] == ["fa"]


async def test_first_time_user_detection_is_not_swallowed_by_the_except(
    env: Path, graph: _FakeGraph
) -> None:
    """The broad ``except`` used to hide the same .exec() AttributeError."""
    from nexus_ai_agent.features.onboarding import is_first_time_user
    from nexus_ai_agent.storage.db import get_session
    from nexus_ai_agent.storage.models import UserLanguage

    assert await is_first_time_user(OWNER_ID, get_session) is True
    engine = create_engine(f"sqlite:///{env}")
    with Session(engine) as session:
        session.add(UserLanguage(user_id=OWNER_ID, language="fa"))
        session.commit()
    engine.dispose()
    assert await is_first_time_user(OWNER_ID, get_session) is False


# ── helpers ───────────────────────────────────────────────────────────────


def _text_router(handlers: list[Any]) -> Any:
    """The catch-all text handler is the last MessageHandler registered."""
    from telegram.ext import MessageHandler

    routers = [h for h in handlers if isinstance(h, MessageHandler)]
    assert routers, "no catch-all text handler registered"
    return routers[-1]


async def _route(router: Any, update: _FakeUpdate, bot: _FakeBot) -> list[str]:
    context = SimpleNamespace(args=[], bot=bot, application=SimpleNamespace(bot=bot, bot_data={}))
    await router.callback(update, context)
    return update.message.replies


async def _run_command_with_markup(
    handlers: list[Any], name: str, update: _FakeUpdate, bot: _FakeBot, args: list[str]
) -> None:
    context = SimpleNamespace(args=args, bot=bot, application=SimpleNamespace(bot=bot, bot_data={}))
    await _command(handlers, name).callback(update, context)
    assert update.message.replies, f"/{name} did not reply"


def _button_data(markup: Any, index: int) -> str:
    return markup.inline_keyboard[index][0].callback_data
