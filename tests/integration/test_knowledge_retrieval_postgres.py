"""PostgreSQL leg: the knowledge registry on the real backend (task-273).

Runs only where ``NEXUS_DATABASE_URL`` is set **and** the second explicit
opt-in ``NEXUS_PG_TEST_DESTRUCTIVE=1`` is present — the same two-gate
discipline as ``test_postgres_single_engine.py`` (D-0028): the URL alone may
point at an operator's real database, so it must not by itself arm a module
that writes rows.

What this proves (the review finding on ``TIMESTAMP WITHOUT TIME ZONE``):

* ``KnowledgeSource`` registry writes round-trip on PostgreSQL with **naive
  UTC** timestamps exactly as migration ``d4e1c0a79b21`` declares the
  columns — aware caller values are normalised, naive values preserved;
* the async session path used by the service reads them back as naive values
  (no driver-dependent tz re-interpretation);
* every write is scoped to a per-run nonce and removed afterwards, with a
  survival assertion over the rows that existed before the module ran.

The *index store* is deliberately out of scope: it is file-backed derived
state and behaves identically regardless of the database backend.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio

from nexus_ai_agent.knowledge_retrieval.repository import KnowledgeSourceRepository
from nexus_ai_agent.storage.db import (
    normalize_database_url,
    sync_engine,
    to_asyncpg_url,
)
from nexus_ai_agent.storage.models import KnowledgeSource

#: Second explicit opt-in (D-0028): ``NEXUS_DATABASE_URL`` alone never arms a
#: writing module.
DESTRUCTIVE_OPT_IN = "NEXUS_PG_TEST_DESTRUCTIVE"

pytestmark = [
    pytest.mark.skipif(not os.getenv("NEXUS_DATABASE_URL"), reason="requires PostgreSQL"),
    pytest.mark.skipif(not os.getenv(DESTRUCTIVE_OPT_IN), reason="requires destructive opt-in"),
    # The cached async engine/pool is bound to the loop it was created on;
    # pin this module's tests to ONE loop so both tests share it (the sync
    # baseline fixture above never touches that pool).
    pytest.mark.asyncio(loop_scope="module"),
]


@pytest.fixture(scope="module")
def nonce() -> str:
    return uuid.uuid4().hex[:12]


@pytest.fixture(scope="module", autouse=True)
def _survival_baseline():
    """Snapshot the registry on the *sync* engine, prove foreign rows survived.

    Deliberately synchronous (the house pattern of
    ``test_postgres_single_engine.py``): sync-engine code is loop-agnostic.
    """
    from sqlmodel import Session, select

    with Session(sync_engine()) as session:
        before = {row.source_id for row in session.execute(select(KnowledgeSource)).scalars()}
    yield before
    with Session(sync_engine()) as session:
        after = {row.source_id for row in session.execute(select(KnowledgeSource)).scalars()}
    # Every row this module created is gone; nothing pre-existing disappeared.
    assert not any(sid.startswith("pgtest-") for sid in after), "nonce rows not cleaned"
    assert before <= after, f"foreign registry rows disappeared: {sorted(before - after)}"


@pytest_asyncio.fixture(scope="module", autouse=True, loop_scope="module")
async def registry_factory():
    """Module-local async engine + session factory on this module's loop.

    The process-wide ``_pg_engines`` cache binds its pool to whichever event
    loop touched it first; a *local* engine keeps this module from poisoning
    (or being poisoned by) that shared cache, and it is disposed at teardown.
    Everything runs on the single module-scoped loop (pytestmark above), so
    both tests and the teardown share one loop.
    """
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    url = normalize_database_url(os.environ["NEXUS_DATABASE_URL"])
    engine = create_async_engine(to_asyncpg_url(url), echo=False, pool_pre_ping=True)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def test_registry_timestamps_are_naive_utc_on_postgres(nonce: str, registry_factory) -> None:
    repository = KnowledgeSourceRepository(registry_factory)
    source_id = f"pgtest-{nonce}-a"
    aware = datetime(2026, 10, 24, 4, 30, 0, tzinfo=timezone.utc)
    await repository.upsert_ingested(
        source_id=source_id,
        root=f"pgroot-{nonce}",
        path=f"pgtest-{nonce}-a.md",
        sha256="a" * 64,
        size_bytes=11,
        chunks=1,
        indexed_at=aware,
    )
    rows = [row for row in await repository.list_all() if row.source_id == source_id]
    assert len(rows) == 1
    assert rows[0].indexed_at is not None
    indexed = datetime.fromisoformat(rows[0].indexed_at)
    updated = datetime.fromisoformat(rows[0].updated_at or "")
    assert indexed.tzinfo is None, "indexed_at must be naive UTC on PG"
    assert updated.tzinfo is None, "updated_at must be naive UTC on PG"
    assert indexed == aware.astimezone(timezone.utc).replace(tzinfo=None)
    await repository.delete(source_id)


async def test_registry_preserves_naive_caller_timestamps(nonce: str, registry_factory) -> None:
    repository = KnowledgeSourceRepository(registry_factory)
    source_id = f"pgtest-{nonce}-b"
    naive = datetime(2026, 3, 1, 9, 30, 0)  # contract: naive IS UTC
    await repository.upsert_ingested(
        source_id=source_id,
        root=f"pgroot-{nonce}",
        path=f"pgtest-{nonce}-b.md",
        sha256="b" * 64,
        size_bytes=22,
        chunks=2,
        indexed_at=naive,
    )
    rows = [row for row in await repository.list_all() if row.source_id == source_id]
    assert len(rows) == 1
    assert rows[0].indexed_at is not None
    parsed = datetime.fromisoformat(rows[0].indexed_at)
    assert parsed == naive and parsed.tzinfo is None
    # Batch lookup (the search-path query) also works on the real backend.
    fetched = await repository.get_many([source_id])
    assert source_id in fetched and fetched[source_id].sha256 == "b" * 64
    await repository.delete(source_id)
