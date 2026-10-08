"""Regression: `await AsyncSession.exec()` on a *plain* SQLAlchemy session.

`storage.db.get_session` yields a ``sqlalchemy.ext.asyncio.AsyncSession`` from
``async_sessionmaker`` — not ``sqlmodel.ext.asyncio.session.AsyncSession`` — and
only the SQLModel subclass carries ``exec()``.  Every ``await session.exec(stmt)``
on the real factory raises ``AttributeError`` at runtime.  Measured live on
``main`` @ ``440d290`` (task-123 forensic dedupe of PR#33):

* ``bot/handlers.py``: ``_upsert_user`` / ``_upsert_chat`` (every incoming
  message), ``/myfiles``, ``/download``, and both ``/language`` branches;
* ``features/onboarding.py::is_first_time_user`` — its broad ``except`` swallowed
  the ``AttributeError`` and reported *every* user as first-time.

Three proofs below: the session contract itself, the real runtime paths against
a real SQLite async session, and a source ratchet over ``src/`` so the defect
class cannot return.  Mutation check: revert any call site to ``.exec`` and the
ratchet goes red; make ``get_session`` yield SQLModel's subclass and the
contract test goes red.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from nexus_ai_agent import storage  # noqa: F401  (registers every table)
from nexus_ai_agent.bot.handlers import _upsert_chat, _upsert_user
from nexus_ai_agent.features.onboarding import is_first_time_user

SRC_ROOT = Path(__file__).resolve().parents[2] / "src"
_AWAITED_EXEC = re.compile(r"await\s+[A-Za-z_][A-Za-z0-9_.]*\.exec\s*\(")


async def _factory(tmp_path: Path) -> async_sessionmaker[Any]:
    """A real AsyncSession factory over SQLite — exactly the shape of ``get_session``."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'app.sqlite'}")
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


async def test_get_session_factory_yields_no_sqlmodel_exec(tmp_path: Path) -> None:
    """The contract: the app's session has ``execute`` and must never have ``exec``."""
    factory = await _factory(tmp_path)
    async with factory() as session:
        assert hasattr(session, "execute")
        assert not hasattr(session, "exec"), (
            "the session from get_session grew SQLModel's exec(); if that is now "
            "true, this defect class changed shape — revisit the call sites"
        )


async def test_upsert_user_and_chat_run_on_a_real_async_session(tmp_path: Path) -> None:
    """RED on main@440d290: AttributeError from ``_upsert_user`` / ``_upsert_chat``."""
    factory = await _factory(tmp_path)
    tg_user = SimpleNamespace(id=4242, username="forensic")

    created = await _upsert_user(factory, tg_user)
    assert created.telegram_id == 4242
    assert created.username == "forensic"

    updated = await _upsert_user(factory, SimpleNamespace(id=4242, username="renamed"))
    assert updated.username == "renamed"

    chat = await _upsert_chat(factory, chat_id=99, thread_id="t-99")
    assert chat.chat_id == 99
    assert chat.thread_id == "t-99"


async def test_is_first_time_user_reports_a_stored_language_row(tmp_path: Path) -> None:
    """RED on main@440d290: the swallowed AttributeError reports everyone as new."""
    factory = await _factory(tmp_path)

    assert await is_first_time_user(777, factory) is True

    from nexus_ai_agent.storage.models import UserLanguage

    async with factory() as session:
        session.add(UserLanguage(user_id=777, language="fa"))
        await session.commit()

    assert await is_first_time_user(777, factory) is False, (
        "a user with a stored language row is not first-time; the broad except "
        "must not mask a broken query again"
    )


@pytest.mark.parametrize("path", sorted(SRC_ROOT.rglob("*.py")), ids=lambda p: p.name)
def test_no_awaited_sqlmodel_exec_survives_in_source(path: Path) -> None:
    """Ratchet: ``await <session>.exec(...)`` may not come back anywhere in ``src/``."""
    offender = _AWAITED_EXEC.search(path.read_text(encoding="utf-8"))
    assert offender is None, (
        f"{path.relative_to(SRC_ROOT)}: async sessions have no SQLModel exec() — "
        f"use `(await session.execute(stmt)).scalars()` (found: {offender.group(0)!r})"
    )
