"""Wave 3D Patch A — the async session API boundary.

``nexus_ai_agent.storage.db.get_session`` yields a **plain SQLAlchemy
``AsyncSession``** (``async_sessionmaker(create_async_engine(...))``). SQLAlchemy's
``AsyncSession`` exposes ``execute()``; ``exec()`` exists only on SQLModel's own
``Session``/``AsyncSession`` subclasses, which this storage layer never creates.

Seven call sites in ``bot/handlers.py`` (×6) and ``features/onboarding.py`` (×1)
called ``await session.exec(...)``. Because ``bot/app.py`` passes ``get_session``
straight into ``build_handlers`` as ``db_session_factory``, **every incoming free
text message** died in ``_upsert_user`` with
``AttributeError: 'AsyncSession' object has no attribute 'exec'`` before reaching
the graph — the primary conversational path.

The repair is deliberately minimal and mechanical: ``execute()` + ``scalars()``
instead of ``exec()``. The sync ``Session`` call sites elsewhere in ``features/``
are *correct* and are asserted to stay untouched.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from nexus_ai_agent.bot.handlers import _upsert_chat, _upsert_user
from nexus_ai_agent.features.onboarding import is_first_time_user
from nexus_ai_agent.storage import models as _models  # noqa: F401  (registers tables)
from nexus_ai_agent.storage.db import SQLModel

SRC = Path(__file__).parents[2] / "src"

# The sync SQLModel `Session` modules where `.exec()` is the *correct* API. A
# repair that rewrote these would be a regression, so they are pinned explicitly.
SYNC_SESSION_MODULES = (
    "nexus_ai_agent/features/ads.py",
    "nexus_ai_agent/features/analytics.py",
    "nexus_ai_agent/features/anonymous_chat.py",
    "nexus_ai_agent/features/channel_manager.py",
    "nexus_ai_agent/features/engagement.py",
    "nexus_ai_agent/features/force_join.py",
    "nexus_ai_agent/features/games.py",
    "nexus_ai_agent/features/gamification.py",
    "nexus_ai_agent/features/owner_control.py",
    "nexus_ai_agent/features/viral_engine.py",
)


@pytest.fixture()
async def session_factory(tmp_path: Path) -> Any:
    """The production factory shape: a plain SQLAlchemy async sessionmaker."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'app.sqlite'}")
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def test_upsert_user_accepts_a_plain_async_session(session_factory: Any) -> None:
    """The first message of a brand-new user must not raise ``AttributeError``."""
    user = SimpleNamespace(id=111, username="alice")
    row = await asyncio.wait_for(_upsert_user(session_factory, user), timeout=15)

    assert row.telegram_id == 111
    assert row.username == "alice"

    # The second call takes the *existing* branch — the other repaired line.
    again = await asyncio.wait_for(_upsert_user(session_factory, user), timeout=15)
    assert again.telegram_id == 111


async def test_upsert_chat_accepts_a_plain_async_session(session_factory: Any) -> None:
    row = await asyncio.wait_for(_upsert_chat(session_factory, 111, "tg:111"), timeout=15)
    assert row.chat_id == 111
    assert row.thread_id == "tg:111"
    again = await asyncio.wait_for(_upsert_chat(session_factory, 111, "tg:111"), timeout=15)
    assert again.chat_id == 111


async def test_first_time_user_check_accepts_a_plain_async_session(session_factory: Any) -> None:
    """``features/onboarding`` shares the same factory and the same defect class."""
    first = await asyncio.wait_for(is_first_time_user(111, session_factory), timeout=15)
    assert first is True


def test_no_async_session_call_site_uses_the_sqlmodel_only_exec_api() -> None:
    """A static guard: the whole ``src`` tree must be free of this defect class.

    ``await <session>.exec(...)`` is only ever valid on a SQLModel session, and
    this project's async factories never produce one, so the pattern is banned
    outright. A future call site fails here rather than in production.
    """
    offenders: list[str] = []
    pattern = re.compile(r"\bawait\s+\w*\.?session\w*\.exec\s*\(")
    for path in sorted(SRC.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line):
                offenders.append(f"{path.relative_to(SRC)}:{lineno}: {line.strip()}")
    assert offenders == [], "await <session>.exec(...) on a plain AsyncSession:\n" + "\n".join(
        offenders
    )


def test_the_sync_sqlmodel_session_modules_still_use_exec() -> None:
    """The repair must not have spread into the sync ``Session`` modules.

    Those modules import SQLModel's ``Session``, for which ``.exec()`` *is* the
    documented API; rewriting them to ``execute()`` would be an unrelated change.
    """
    for relative in SYNC_SESSION_MODULES:
        text = (SRC / relative).read_text(encoding="utf-8")
        assert "from sqlmodel import" in text and "Session" in text, relative
        assert ".exec(" in text, f"{relative} lost its (correct) SQLModel .exec() usage"
