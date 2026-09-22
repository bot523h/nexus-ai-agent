"""Contract tests for first-run onboarding (task-161).

``/start`` must keep its welcome and referral behaviour. Onboarding is additive:
a keyboard is sent only on the first successful start, and the detected language
is persisted only after that send.
"""

from __future__ import annotations

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
from nexus_ai_agent.features.onboarding import is_first_time_user
from nexus_ai_agent.storage import models as models_module  # noqa: F401
from nexus_ai_agent.storage.models import Referral, UserLanguage


class _Result:
    def __init__(self, row: Any) -> None:
        self._row = row

    def scalars(self) -> _Result:
        return self

    def first(self) -> Any:
        return self._row


class _ExecuteSession:
    def __init__(self, row: Any) -> None:
        self.row = row
        self.executed = False

    async def execute(self, stmt: Any) -> _Result:
        del stmt
        self.executed = True
        return _Result(self.row)

    async def __aenter__(self) -> _ExecuteSession:
        return self

    async def __aexit__(self, *args: object) -> bool:
        del args
        return False


class _Boom:
    async def __aenter__(self) -> Any:
        raise RuntimeError("db down")

    async def __aexit__(self, *args: object) -> bool:
        del args
        return False


def make_update(
    user_id: int = 7,
    *,
    language_code: str | None = "en",
    chat_id: int = 70,
) -> SimpleNamespace:
    message = SimpleNamespace(reply_text=AsyncMock(), text=None)
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id, language_code=language_code),
        effective_chat=SimpleNamespace(id=chat_id),
        effective_message=message,
        message=message,
        edited_message=None,
        callback_query=None,
    )


def make_context(args: list[str] | None = None) -> SimpleNamespace:
    return SimpleNamespace(args=args or [], bot=None)


def make_callback(user_id: int, data: str) -> SimpleNamespace:
    query = SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=user_id, language_code="en"),
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
        message=SimpleNamespace(),
    )
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id, language_code="en"),
        effective_chat=SimpleNamespace(id=70),
        message=None,
        edited_message=None,
        callback_query=query,
    )


def _markups(update: SimpleNamespace) -> list[Any]:
    return [call.kwargs.get("reply_markup") for call in update.message.reply_text.call_args_list]


@pytest.fixture()
def env(tmp_path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    db_file = str(tmp_path / "onboarding.sqlite")
    engine = create_engine(f"sqlite:///{db_file}")
    SQLModel.metadata.create_all(engine)
    engine.dispose()
    monkeypatch.setenv("NEXUS_DB_PATH", db_file)
    settings_module.get_settings.cache_clear()
    settings = Settings().model_copy(update={"db_path": db_file})
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
        settings_module.get_settings.cache_clear()


def _languages(db: str) -> list[UserLanguage]:
    engine = create_engine(f"sqlite:///{db}")
    with Session(engine) as session:
        rows = list(session.query(UserLanguage).all())
    engine.dispose()
    return rows


async def test_first_start_persists_language_and_second_does_not_repeat(
    env: SimpleNamespace,
) -> None:
    update = make_update(user_id=7, language_code="fa-IR")
    await env.cmds["start"](update, make_context())
    texts = [call.args[0] for call in update.message.reply_text.call_args_list]
    assert any("Welcome" in text for text in texts)
    assert any(markup is not None for markup in _markups(update))
    rows = _languages(env.db)
    assert len(rows) == 1
    assert rows[0].user_id == 7
    assert rows[0].language == "fa"

    again = make_update(user_id=7, language_code="en")
    await env.cmds["start"](again, make_context())
    assert all(markup is None for markup in _markups(again))
    assert len(_languages(env.db)) == 1
    assert _languages(env.db)[0].language == "fa"


async def test_missing_language_code_stores_en(env: SimpleNamespace) -> None:
    update = make_update(user_id=8, language_code=None)
    await env.cmds["start"](update, make_context())
    rows = _languages(env.db)
    assert len(rows) == 1
    assert rows[0].language == "en"


async def test_existing_language_row_skips_keyboard(env: SimpleNamespace) -> None:
    env.engines.onboarding.remember(9, "de")
    update = make_update(user_id=9, language_code="en")
    await env.cmds["start"](update, make_context())
    assert all(markup is None for markup in _markups(update))
    assert _languages(env.db)[0].language == "de"


async def test_failed_send_does_not_persist(env: SimpleNamespace) -> None:
    update = make_update(user_id=10, language_code="fr")

    async def reply(*args: Any, **kwargs: Any) -> None:
        del args
        if kwargs.get("reply_markup") is not None:
            raise RuntimeError("cannot send")

    update.message.reply_text = reply
    update.effective_message.reply_text = reply
    await env.cmds["start"](update, make_context())
    assert _languages(env.db) == []
    assert env.engines.onboarding.is_first_time(10) is True


async def test_markdown_failure_falls_back_to_plain_text(env: SimpleNamespace) -> None:
    update = make_update(user_id=11, language_code="en")
    modes: list[str | None] = []

    async def reply(*args: Any, **kwargs: Any) -> None:
        del args
        modes.append(kwargs.get("parse_mode"))
        if kwargs.get("parse_mode") == "Markdown":
            raise RuntimeError("bad markdown")

    update.message.reply_text = reply
    update.effective_message.reply_text = reply
    await env.cmds["start"](update, make_context())
    assert "Markdown" in modes
    assert None in modes
    assert _languages(env.db)[0].language == "en"


async def test_callback_edits_hint_and_unknown_data_does_not(env: SimpleNamespace) -> None:
    known = make_callback(11, "onboarding_ai")
    await env.cmds["onboarding_callback"](known, make_context())
    known.callback_query.answer.assert_awaited()
    edited = known.callback_query.edit_message_text.call_args.args[0]
    assert "/ask" in edited

    unknown = make_callback(11, "onboarding_nope")
    await env.cmds["onboarding_callback"](unknown, make_context())
    unknown.callback_query.answer.assert_awaited()
    unknown.callback_query.edit_message_text.assert_not_awaited()


async def test_start_referral_still_records(env: SimpleNamespace) -> None:
    code = env.engines.referral.get_or_create_code(user_id=100)
    update = make_update(user_id=200, language_code="en", chat_id=200)
    await env.cmds["start"](update, make_context([f"ref_{code}"]))
    texts = [call.args[0] for call in update.message.reply_text.call_args_list]
    assert any("🎁" in text for text in texts)
    assert any("Welcome" in text for text in texts)
    engine = create_engine(f"sqlite:///{env.db}")
    with Session(engine) as session:
        ref = session.query(Referral).filter_by(referee_id=200).first()
    engine.dispose()
    assert ref is not None
    assert ref.referrer_id == 100


async def test_self_referral_stays_silent(env: SimpleNamespace) -> None:
    code = env.engines.referral.get_or_create_code(user_id=300)
    update = make_update(user_id=300, language_code="en", chat_id=300)
    await env.cmds["start"](update, make_context([f"ref_{code}"]))
    texts = [call.args[0] for call in update.message.reply_text.call_args_list]
    assert not any("🎁" in text for text in texts)


async def test_sqlalchemy_execute_path_and_operational_failure() -> None:
    empty = _ExecuteSession(None)
    assert await is_first_time_user(1, lambda: empty) is True
    assert empty.executed is True
    present = _ExecuteSession(SimpleNamespace(language="fa"))
    assert await is_first_time_user(1, lambda: present) is False
    assert await is_first_time_user(1, lambda: _Boom()) is False
