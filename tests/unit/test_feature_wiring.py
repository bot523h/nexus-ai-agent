"""Behavioural tests for the feature-engine wiring (feature-wiring batch).

The old handlers were stubs that replied with hard-coded strings; these
tests assert that the *real* engines run: ``/calc`` computes, ``/remind``
persists a row, games keep state across calls, the referral loop records
referrals, force-join performs a real membership check, and anonymous
chat delivers messages between paired users.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine
from sqlmodel import Session, SQLModel

from nexus_ai_agent.bot.feature_handlers import (
    ActiveAnonSessionFilter,
    build_feature_command_handlers,
    build_feature_engines,
)
from nexus_ai_agent.config import settings as settings_module
from nexus_ai_agent.config.settings import Settings
from nexus_ai_agent.storage import models as models_module  # noqa: F401
from nexus_ai_agent.storage.models import Referral, Reminder

# ── fakes ──────────────────────────────────────────────────────────────


class FakeBot:
    """Minimal stand-in for a Telegram bot."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str) -> None:
        self.sent.append((chat_id, text))

    async def get_chat_member(self, chat_id: Any, user_id: int) -> Any:
        status = getattr(self, "member_status", "member")
        return SimpleNamespace(status=status)


def make_update(user_id: int = 1, chat_id: int = 10, text: str | None = None) -> SimpleNamespace:
    message = SimpleNamespace(
        text=text,
        reply_text=AsyncMock(),
        reply_photo=AsyncMock(),
        reply_document=AsyncMock(),
        from_user=SimpleNamespace(id=user_id),
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


def make_callback(user_id: int = 1, data: str = "") -> SimpleNamespace:
    query = SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=user_id),
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
        message=SimpleNamespace(),
    )
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=10),
        message=None,
        edited_message=None,
        callback_query=query,
    )


@pytest.fixture()
def env(tmp_path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Engines bound to a temp DB plus the command-handler map."""
    db_file = str(tmp_path / "feature.sqlite")
    engine = create_engine(f"sqlite:///{db_file}")
    SQLModel.metadata.create_all(engine)
    engine.dispose()
    # Engines that read settings internally (force-join, anon chat) must
    # hit the same isolated DB file.
    monkeypatch.setenv("NEXUS_DB_PATH", db_file)
    settings_module.get_settings.cache_clear()
    try:
        settings = Settings().model_copy(update={"db_path": db_file})
        engines = build_feature_engines(settings)
        cmds = build_feature_command_handlers(engines, settings)
        yield SimpleNamespace(db=db_file, settings=settings, engines=engines, cmds=cmds)
    finally:
        settings_module.get_settings.cache_clear()


def reply_text(update: SimpleNamespace) -> str:
    return update.message.reply_text.call_args.args[0]


# ── tools: /calc /remind /convert /tr ─────────────────────────────────


async def test_calc_actually_computes(env: SimpleNamespace) -> None:
    update = make_update()
    await env.cmds["calc"](update, make_context(["2", "+", "2*3"]))
    assert reply_text(update) == "🧮 2 + 2*3 = 8"


async def test_calc_rejects_code_execution(env: SimpleNamespace) -> None:
    update = make_update()
    await env.cmds["calc"](update, make_context(["().__class__.__bases__"]))
    assert "نامعتبر" in reply_text(update)


async def test_calc_usage_without_args(env: SimpleNamespace) -> None:
    update = make_update()
    await env.cmds["calc"](update, make_context([]))
    assert "/calc" in reply_text(update)


async def test_remind_persists_row_in_originating_chat(env: SimpleNamespace) -> None:
    bot = FakeBot()
    update = make_update(user_id=1, chat_id=-1001)
    await env.cmds["remind"](update, make_context(["30m", "نماز"], bot=bot))
    assert "یادآوری #" in reply_text(update)
    engine = create_engine(f"sqlite:///{env.db}")
    with Session(engine) as session:
        rows = session.query(Reminder).all()
    engine.dispose()
    assert len(rows) == 1
    assert rows[0].user_id == 1
    assert rows[0].chat_id == -1001  # originating chat, not user id
    assert rows[0].status == "pending"


async def test_cancel_remind_command(env: SimpleNamespace) -> None:
    bot = FakeBot()
    update = make_update(user_id=1, chat_id=10)
    await env.cmds["remind"](update, make_context(["1h", "جلسه"], bot=bot))
    engine = create_engine(f"sqlite:///{env.db}")
    with Session(engine) as session:
        rid = session.query(Reminder).first().id
    engine.dispose()

    cancel_update = make_update(user_id=1, chat_id=10)
    await env.cmds["cancel_remind"](cancel_update, make_context([str(rid)]))
    assert "لغو شد" in reply_text(cancel_update)

    # Someone else cannot cancel it (already gone now, but ownership path
    # is covered by the reminder system tests; here: bad input handling).
    bad_update = make_update(user_id=1, chat_id=10)
    await env.cmds["cancel_remind"](bad_update, make_context(["abc"]))
    assert "عدد" in reply_text(bad_update)


async def test_convert_uses_real_converter(env: SimpleNamespace) -> None:
    update = make_update()
    await env.cmds["convert"](update, make_context(["100", "usd", "irt"]))
    assert reply_text(update).startswith("💰")

    update2 = make_update()
    await env.cmds["convert"](update2, make_context(["5", "c", "f"]))
    assert reply_text(update2).startswith("🌡️")


async def test_convert_rejects_non_numeric(env: SimpleNamespace) -> None:
    update = make_update()
    await env.cmds["convert"](update, make_context(["abc", "usd", "irt"]))
    assert "❌" in reply_text(update)


async def test_tr_delegates_to_translator(env: SimpleNamespace) -> None:
    env.engines.translator.translate = AsyncMock(return_value="Hello world")
    update = make_update()
    await env.cmds["tr"](update, make_context(["سلام", "دنیا"]))
    assert reply_text(update) == "🌐 سلام دنیا → Hello world"
    env.engines.translator.translate.assert_awaited_once_with("سلام دنیا", source="fa", target="en")


# ── games: shared state across calls ──────────────────────────────────


async def test_number_guess_keeps_state(env: SimpleNamespace) -> None:
    start_update = make_update(user_id=7)
    await env.cmds["guess_start"](start_update, make_context())
    assert "بازی حدس عدد شروع شد" in reply_text(start_update)

    guess_update = make_update(user_id=7)
    await env.cmds["guess"](guess_update, make_context(["50"]))
    assert reply_text(guess_update) in ("⬆️ بزرگ‌تره! (حدس 1)", "⬇️ کوچک‌تره! (حدس 1)")

    # A different user has no game.
    other_update = make_update(user_id=8)
    await env.cmds["guess"](other_update, make_context(["50"]))
    assert "بازی فعلی ندارید" in reply_text(other_update)


async def test_guess_persian_digits(env: SimpleNamespace) -> None:
    start_update = make_update(user_id=9)
    await env.cmds["guess_start"](start_update, make_context())
    guess_update = make_update(user_id=9)
    await env.cmds["guess"](guess_update, make_context(["۴۲"]))
    assert "حدس" in reply_text(guess_update)


async def test_wordle_start_and_guess(env: SimpleNamespace) -> None:
    start_update = make_update(user_id=5)
    await env.cmds["wordle"](start_update, make_context())
    assert "وردل" in reply_text(start_update)

    # Find the target word from the engine and guess it: must win.
    target = env.engines.wordle._games[5]["target"]  # type: ignore[index]
    guess_update = make_update(user_id=5)
    await env.cmds["wordle"](guess_update, make_context([target]))
    assert "آفرین" in reply_text(guess_update)


async def test_poll_create_vote_results(env: SimpleNamespace) -> None:
    create_update = make_update(user_id=1)
    # Real Telegram splits "/poll q | a | b" into tokens; the pipes survive.
    await env.cmds["poll"](create_update, make_context(["دوست‌داشتنی‌ترین؟", "|", "کد", "|", "قهوه"]))
    markup = create_update.message.reply_text.call_args.kwargs["reply_markup"]
    data0 = markup.inline_keyboard[0][0].callback_data
    data1 = markup.inline_keyboard[1][0].callback_data
    assert data0.startswith("pollvote_")

    # Two users vote different options.
    cb1 = make_callback(user_id=1, data=data0)
    await env.cmds["poll_callback"](cb1, make_context())
    cb1.callback_query.edit_message_text.assert_awaited_once()
    results_text = cb1.callback_query.edit_message_text.call_args.args[0]
    assert "کد" in results_text

    cb2 = make_callback(user_id=2, data=data1)
    await env.cmds["poll_callback"](cb2, make_context())
    results2 = cb2.callback_query.edit_message_text.call_args.args[0]
    assert "مجموع آرا: 2" in results2

    # Double vote rejected (answer text), no new result edit needed.
    cb3 = make_callback(user_id=1, data=data0)
    await env.cmds["poll_callback"](cb3, make_context())
    cb3.callback_query.answer.assert_awaited()


async def test_quiz_roundtrip(env: SimpleNamespace) -> None:
    env.engines.quiz.update_score = lambda user_id, chat_id, correct: 0  # no DB in test
    ask_update = make_update(user_id=3)
    await env.cmds["quiz"](ask_update, make_context())
    markup = ask_update.message.reply_text.call_args.kwargs["reply_markup"]
    correct_idx = env.engines.quiz._active[3]["answer"]  # type: ignore[index]
    data = markup.inline_keyboard[correct_idx][0].callback_data
    assert data.startswith("quiz_")

    cb = make_callback(user_id=3, data=data)
    await env.cmds["quiz_callback"](cb, make_context())
    edited = cb.callback_query.edit_message_text.call_args.args[0]
    assert "درست" in edited


# ── referral: /start deep-link actually records referrals ─────────────


async def test_start_deep_link_records_referral(env: SimpleNamespace) -> None:
    code = env.engines.referral.get_or_create_code(user_id=100)
    start_update = make_update(user_id=200, chat_id=200)
    await env.cmds["start"](start_update, make_context([f"ref_{code}"]))
    texts = [c.args[0] for c in start_update.message.reply_text.call_args_list]
    assert any("🎁" in t for t in texts)

    engine = create_engine(f"sqlite:///{env.db}")
    with Session(engine) as session:
        ref = session.query(Referral).filter_by(referee_id=200).first()
    engine.dispose()
    assert ref is not None
    assert ref.referrer_id == 100
    assert ref.status == "completed"


async def test_start_without_ref_param_is_plain_welcome(env: SimpleNamespace) -> None:
    update = make_update(user_id=201, chat_id=201)
    await env.cmds["start"](update, make_context([]))
    assert any("Welcome" in c.args[0] for c in update.message.reply_text.call_args_list)


async def test_start_self_referral_stays_silent(env: SimpleNamespace) -> None:
    code = env.engines.referral.get_or_create_code(user_id=300)
    update = make_update(user_id=300, chat_id=300)
    await env.cmds["start"](update, make_context([f"ref_{code}"]))
    texts = [c.args[0] for c in update.message.reply_text.call_args_list]
    assert not any("🎁" in t for t in texts)


# ── force join: real membership check (P0-3) ──────────────────────────


async def test_forcejoin_verify_uses_real_bot_check(env: SimpleNamespace) -> None:
    bot = FakeBot()  # member_status="member" by default
    cb = make_callback(user_id=42, data="forcejoin_verify")
    await env.cmds["forcejoin_verify"](cb, make_context(bot=bot))
    edited = cb.callback_query.edit_message_text.call_args.args[0]
    assert "تأیید شد" in edited
    # The bot was actually bound to the shared manager.
    assert env.engines.force_join.is_bound
    assert env.engines.force_join.bot is bot


async def test_forcejoin_verify_non_member_denied(env: SimpleNamespace) -> None:
    bot = FakeBot()
    bot.member_status = "left_chat"
    cb = make_callback(user_id=43, data="forcejoin_verify")
    await env.cmds["forcejoin_verify"](cb, make_context(bot=bot))
    edited = cb.callback_query.edit_message_text.call_args.args[0]
    assert "❌" in edited


# ── anonymous chat: bot injection + delivery path (P0-10) ─────────────


async def test_anon_message_delivered_to_partner(env: SimpleNamespace) -> None:
    bot = FakeBot()
    env.engines.anon.bind(bot)
    env.engines.anon._active[1] = 2  # type: ignore[attr-defined]
    env.engines.anon._active[2] = 1  # type: ignore[attr-defined]

    update = make_update(user_id=1, chat_id=1, text="سلام ناشناس")
    # The wiring exposes a MessageHandler; invoke its callback directly.
    await env.cmds["anon_message_handler"].callback(update, make_context(bot=bot))
    assert bot.sent == [(2, "📩 پیام ناشناس:\nسلام ناشناس")]
    assert "✅" in reply_text(update)


async def test_anon_filter_matches_only_paired_users(env: SimpleNamespace) -> None:
    env.engines.anon._active[1] = 2  # type: ignore[attr-defined]
    env.engines.anon._active[2] = 1  # type: ignore[attr-defined]
    filt = ActiveAnonSessionFilter(env.engines.anon.has_active_session)
    assert filt.filter(make_update(user_id=1, text="hi").message) is True
    assert filt.filter(make_update(user_id=3, text="hi").message) is False
    assert filt.filter(make_update(user_id=1, text=None).message) is False


async def test_anon_start_uses_shared_manager(env: SimpleNamespace) -> None:
    bot = FakeBot()
    update = make_update(user_id=50, chat_id=50)
    await env.cmds["anon_start"](update, make_context(bot=bot))
    assert "چت ناشناس" in reply_text(update) or "در صف" in reply_text(update)
    assert env.engines.anon.is_bound
