"""PostgreSQL persistence, migration idempotency and durability (mission P0).

Runs only against a live PostgreSQL (``NEXUS_DATABASE_URL``) with the explicit
destructive opt-in (``NEXUS_PG_TEST_DESTRUCTIVE=1``) — the same contract every
module in ``PG_WRITING_MODULES`` obeys
(``tests/unit/test_pg_integration_safety.py`` enforces that).

What this proves that no unit test can:

* ``run_migrations`` reaches Alembic head on a real cluster and is
  *idempotent*: a second run applies nothing and leaves ``alembic_version``
  unchanged, so the Neon lifecycle re-run is a no-op rather than a re-DDL.
* a row written through the async application path (``get_session``) is still
  there after this process's engine pool is disposed and rebuilt from scratch:
  the data lives on the server, not in this process's memory — the durability
  property a scale-to-zero host depends on.
* the backend the runtime reports is really PostgreSQL.

Data-safety: every write is scoped to a per-run nonce (a chat id and a
telegram id this run generated); the cleanup deletes only those rows.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import Iterator
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlmodel import select

from nexus_ai_agent.config import settings as settings_module
from nexus_ai_agent.storage import db as db_module
from nexus_ai_agent.storage.db import get_session, resolve_database_url, selected_backend
from nexus_ai_agent.storage.models import Chat, User

DESTRUCTIVE_OPT_IN = "NEXUS_PG_TEST_DESTRUCTIVE"

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not os.getenv("NEXUS_DATABASE_URL"),
        reason="requires PostgreSQL (NEXUS_DATABASE_URL)",
    ),
    pytest.mark.skipif(
        os.getenv(DESTRUCTIVE_OPT_IN) != "1",
        reason=(
            f"refuses to write to NEXUS_DATABASE_URL without {DESTRUCTIVE_OPT_IN}=1 "
            "(the CI migrate-postgres job sets it against its disposable service "
            "container)"
        ),
    ),
]

if os.getenv(DESTRUCTIVE_OPT_IN) == "1" and not os.getenv("NEXUS_DATABASE_URL"):
    pytest.fail(
        f"{DESTRUCTIVE_OPT_IN}=1 is set but NEXUS_DATABASE_URL is not: this module "
        "would collect as skipped, so the PostgreSQL persistence proof it exists "
        "to verify would go unverified while the build stayed green. Set both "
        "variables, or neither.",
        pytrace=False,
    )


def _delete_run_rows(chat_id: int, telegram_id: int) -> None:
    """Remove exactly the rows this run created (nonce-scoped)."""
    with db_module.sync_engine().begin() as conn:
        conn.execute(text("DELETE FROM chat WHERE chat_id = :id"), {"id": chat_id})
        conn.execute(text('DELETE FROM "user" WHERE telegram_id = :id'), {"id": telegram_id})


@pytest.fixture()
def pg(monkeypatch: pytest.MonkeyPatch) -> Iterator[SimpleNamespace]:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.delenv("NEXUS_DB_PATH", raising=False)
    settings_module.get_settings.cache_clear()
    db_module._engine = None  # type: ignore[attr-defined]
    db_module._session_factory = None  # type: ignore[attr-defined]
    db_module._sync_engine_url = None  # type: ignore[attr-defined]
    db_module._sync_engine_instance = None  # type: ignore[attr-defined]
    # pytest-asyncio gives each test its own event loop, so a PG engine cached
    # by a previous test is bound to that test's closed loop ("attached to a
    # different loop"). Every test must start from an empty PG cache.
    db_module._pg_engines.clear()
    db_module._pg_session_factories.clear()
    db_module._pg_prepared_urls.clear()

    nonce = 2**40 + secrets.randbelow(2**30)
    chat_id = -(10**12 + nonce)
    try:
        yield SimpleNamespace(nonce=nonce, chat_id=chat_id)
    finally:
        _delete_run_rows(chat_id, nonce)
        settings_module.get_settings.cache_clear()
        db_module._sync_engine_url = None  # type: ignore[attr-defined]
        db_module._sync_engine_instance = None  # type: ignore[attr-defined]


def test_migrations_are_idempotent_on_a_live_cluster(pg: SimpleNamespace) -> None:
    """A second ``run_migrations`` is a no-op; the stamp does not move."""
    from nexus_ai_agent.storage.migrations import run_migrations

    engine = db_module.sync_engine()
    with engine.connect() as conn:
        before = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()

    # Re-running must apply nothing — no error, no re-DDL.
    run_migrations()

    with engine.connect() as conn:
        after = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    assert after == before, "a re-run of run_migrations moved the Alembic stamp"


async def test_row_survives_a_full_engine_rebuild(pg: SimpleNamespace) -> None:
    """Write via the app path, dispose every engine, re-read from a fresh one.

    This is the durability proof: the row is committed on the server, so
    rebuilding the pool from scratch still sees it. If the data lived in
    process memory (or an ephemeral local file) it would be gone.
    """
    async with get_session() as session:
        session.add(User(telegram_id=pg.nonce, username=f"pg-persist-{pg.nonce}"))
        session.add(Chat(chat_id=pg.chat_id, thread_id=f"tg:{pg.chat_id}"))
        await session.commit()

    # Tear the whole process-side pool down — nothing may be cached.
    await db_module._dispose_replaced_engines()
    for engine in list(db_module._pg_engines.values()):
        await engine.dispose()
    db_module._pg_engines.clear()
    db_module._pg_session_factories.clear()
    db_module._pg_prepared_urls.clear()
    db_module._sync_engine_instance = None  # type: ignore[attr-defined]

    # Read back through the async application path, brand-new pool.
    async with get_session() as session:
        user = (await session.exec(select(User).where(User.telegram_id == pg.nonce))).one()
        chat = (await session.exec(select(Chat).where(Chat.chat_id == pg.chat_id))).one()
    assert user.username == f"pg-persist-{pg.nonce}"
    assert chat.thread_id == f"tg:{pg.chat_id}"


async def test_backend_is_really_postgresql(pg: SimpleNamespace) -> None:
    assert resolve_database_url() is not None
    assert selected_backend() == "postgresql"
    async with get_session() as session:
        current = (await session.execute(text("SELECT current_database()"))).scalar_one()
    assert current == db_module.sync_engine().url.database
