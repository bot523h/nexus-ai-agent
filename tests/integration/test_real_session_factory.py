"""The production session factory must satisfy the contract its consumers use.

Audit finding A-01 (2026-10-15): ``storage.db`` built its session factories
with SQLAlchemy's ``AsyncSession`` while ``bot/handlers.py`` (every free-text
message via ``_upsert_user``), ``/myfiles``, ``/download``, ``/language`` and
``features/onboarding.py`` call ``session.exec(...)`` — a SQLModel-only
method. Every one of those paths raised ``AttributeError`` on ``main`` and no
test noticed, because none of them drove a handler through the *real*
factory. These tests do exactly that, with no mocks between the handler
helpers and the database file.

Audit finding A-02b: ``get_session()`` with no argument hard-coded
``data/app.sqlite`` and ignored ``NEXUS_DB_PATH``; the feature engines honour
the setting, so one deployment silently used two SQLite files.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import OperationalError
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession as SQLModelAsyncSession

from nexus_ai_agent.bot.handlers import _upsert_chat, _upsert_user
from nexus_ai_agent.features.onboarding import is_first_time_user
from nexus_ai_agent.storage.db import get_session
from nexus_ai_agent.storage.models import Chat, User, UserLanguage

pytestmark = pytest.mark.asyncio


async def test_get_session_yields_a_sqlmodel_session_with_exec(settings_override) -> None:
    async with get_session() as session:
        assert isinstance(session, SQLModelAsyncSession)
        assert callable(getattr(session, "exec", None))
        rows = (await session.exec(select(User))).all()
    assert rows == []


async def test_upsert_user_and_chat_run_through_the_real_factory(settings_override) -> None:
    tg_user = SimpleNamespace(id=424242, username="first")

    created = await _upsert_user(get_session, tg_user)
    assert created.telegram_id == 424242
    assert created.username == "first"

    # Second message from the same user: the existing row is updated, not duplicated.
    updated = await _upsert_user(get_session, SimpleNamespace(id=424242, username="renamed"))
    assert updated.id == created.id
    assert updated.username == "renamed"

    chat = await _upsert_chat(get_session, -1001, "tg:-1001")
    assert chat.thread_id == "tg:-1001"
    rebound = await _upsert_chat(get_session, -1001, "tg:-1001:v2")
    assert rebound.id == chat.id
    assert rebound.thread_id == "tg:-1001:v2"

    async with get_session() as session:
        assert len((await session.exec(select(User))).all()) == 1
        assert len((await session.exec(select(Chat))).all()) == 1


async def test_get_session_default_path_honours_nexus_db_path(
    settings_override, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Run from an empty cwd so a regression to the literal ``data/app.sqlite``
    # would be visible as a stray file here instead of silently reusing the
    # repository's own data directory.
    workdir = tmp_path / "cwd"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    configured = Path(settings_override.db_path)

    async with get_session() as session:
        await session.exec(select(User))

    assert configured.exists(), "the configured NEXUS_DB_PATH file must be the one in use"
    assert not (workdir / "data" / "app.sqlite").exists(), (
        "get_session() fell back to the literal data/app.sqlite (audit finding A-02b)"
    )


async def test_onboarding_first_time_check_uses_the_real_factory(settings_override) -> None:
    assert await is_first_time_user(777, get_session) is True
    async with get_session() as session:
        session.add(UserLanguage(user_id=777, language="fa"))
        await session.commit()
    assert await is_first_time_user(777, get_session) is False


async def test_onboarding_degrades_only_on_database_errors() -> None:
    class _Broken:
        async def __aenter__(self):
            raise OperationalError("SELECT 1", {}, Exception("database is locked"))

        async def __aexit__(self, *exc: object) -> None:
            return None

    # A database failure degrades to "first time" (the documented behaviour) …
    assert await is_first_time_user(1, _Broken) is True

    class _Programming:
        async def __aenter__(self):
            raise AttributeError("'AsyncSession' object has no attribute 'exec'")

        async def __aexit__(self, *exc: object) -> None:
            return None

    # … but a programming error must surface instead of being swallowed.
    with pytest.raises(AttributeError):
        await is_first_time_user(1, _Programming)
