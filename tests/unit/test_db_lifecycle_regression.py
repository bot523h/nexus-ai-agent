"""W1 recovery — database lifecycle regression contracts.

Root cause (CI run 36336910453, 12/16): ``_initialized_paths`` was keyed by a
*relative* path string, so ``data/app.sqlite`` under an ``ai_memory`` test's
``tmp_path`` and under the repository root collided on one cache entry.  The
second file was treated as "already initialized" and its schema was never
created — ``sqlite3.OperationalError: no such table: pendingapproval`` on a
fresh checkout (locally masked by a stale ``data/app.sqlite``).

These tests pin the corrected invariant: engine lifetime and schema lifetime
are separate (LAW 11), file identity is absolute
(``_normalize_sqlite_path``), and every lifecycle in §6 of the mission
(multiple paths, switching, concurrency, disposal, recreation, idempotency)
holds.  Each test is hermetic (own ``tmp_path``, own state reset) and must
pass in any execution order.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

import pytest
from sqlmodel import select

from nexus_ai_agent.config import settings as settings_module
from nexus_ai_agent.storage import db as db_module
from nexus_ai_agent.storage.db import (
    _normalize_sqlite_path,
    create_all_tables,
    get_session,
)
from nexus_ai_agent.storage.models import PendingApproval


@pytest.fixture(autouse=True)
async def _hermetic_db_state():
    """Reset all DB/request global state so order never matters."""
    settings_module.get_settings.cache_clear()
    db_module._initialized_paths.clear()
    if db_module._engine is not None:
        with contextlib.suppress(Exception):
            await db_module._engine.dispose()
    db_module._engine = None
    db_module._engine_path = None
    db_module._session_factory = None
    while db_module._replaced_engines:
        with contextlib.suppress(Exception):
            await db_module._replaced_engines.pop().dispose()
    yield
    settings_module.get_settings.cache_clear()
    db_module._initialized_paths.clear()
    if db_module._engine is not None:
        with contextlib.suppress(Exception):
            await db_module._engine.dispose()
    db_module._engine = None
    db_module._engine_path = None
    db_module._session_factory = None
    while db_module._replaced_engines:
        with contextlib.suppress(Exception):
            await db_module._replaced_engines.pop().dispose()


async def _row_count(db_path: str) -> int:
    async with get_session(db_path) as session:
        rows = (await session.execute(select(PendingApproval))).scalars().all()
        return len(rows)


# ── A. fresh path → first session ──────────────────────────────────────


@pytest.mark.asyncio
async def test_a_fresh_path_creates_schema_on_first_session(tmp_path: Path) -> None:
    db_path = str(tmp_path / "a.sqlite")
    assert not Path(db_path).exists()
    async with get_session(db_path) as session:
        session.add(PendingApproval(change_type="a", description="fresh"))
        await session.commit()
    assert Path(db_path).exists()
    assert await _row_count(db_path) == 1


# ── B. same path → second session (reuse, data persists) ───────────────


@pytest.mark.asyncio
async def test_b_same_path_second_session_reuses_schema(tmp_path: Path) -> None:
    db_path = str(tmp_path / "b.sqlite")
    async with get_session(db_path) as session:
        session.add(PendingApproval(change_type="b", description="first"))
        await session.commit()
    async with get_session(db_path) as session:
        session.add(PendingApproval(change_type="b", description="second"))
        await session.commit()
    assert await _row_count(db_path) == 2


# ── C. dispose engine → reuse same path ────────────────────────────────


@pytest.mark.asyncio
async def test_c_engine_disposal_preserves_schema_for_same_file(tmp_path: Path) -> None:
    """Simulates conftest's per-test engine disposal: the file keeps its
    tables and the absolute cache key keeps its fast path."""
    db_path = str(tmp_path / "c.sqlite")
    async with get_session(db_path) as session:
        session.add(PendingApproval(change_type="c", description="before-dispose"))
        await session.commit()

    # conftest._dispose_db_engine equivalent (schema cache survives by design).
    assert db_module._engine is not None
    await db_module._engine.dispose()
    db_module._engine = None
    db_module._engine_path = None
    db_module._session_factory = None

    async with get_session(db_path) as session:
        rows = (await session.execute(select(PendingApproval))).scalars().all()
        assert len(rows) == 1
        session.add(PendingApproval(change_type="c", description="after-dispose"))
        await session.commit()
    assert await _row_count(db_path) == 2


# ── D. path A → path B → path A (switching + isolation) ────────────────


@pytest.mark.asyncio
async def test_d_path_switching_isolates_data_and_returns_cleanly(tmp_path: Path) -> None:
    path_a = str(tmp_path / "a.sqlite")
    path_b = str(tmp_path / "b.sqlite")

    async with get_session(path_a) as session:
        session.add(PendingApproval(change_type="a", description="in-a"))
        await session.commit()
    async with get_session(path_b) as session:
        assert (await session.execute(select(PendingApproval))).scalars().all() == []
        session.add(PendingApproval(change_type="b", description="in-b"))
        await session.commit()
    async with get_session(path_a) as session:
        rows = (await session.execute(select(PendingApproval))).scalars().all()
        assert [r.description for r in rows] == ["in-a"]
    async with get_session(path_b) as session:
        rows = (await session.execute(select(PendingApproval))).scalars().all()
        assert [r.description for r in rows] == ["in-b"]


# ── E. schema exists → repeated initialization is idempotent ───────────


@pytest.mark.asyncio
async def test_e_repeated_initialization_is_idempotent(tmp_path: Path) -> None:
    db_path = str(tmp_path / "e.sqlite")
    await create_all_tables(db_path)
    await create_all_tables(db_path)
    await create_all_tables(db_path)
    async with get_session(db_path) as session:
        session.add(PendingApproval(change_type="e", description="after-repeats"))
        await session.commit()
    assert await _row_count(db_path) == 1


# ── F. concurrent initialization ───────────────────────────────────────


@pytest.mark.asyncio
async def test_f_concurrent_initialization_on_fresh_path_is_safe(tmp_path: Path) -> None:
    """Eight coroutines racing ``create_all_tables`` on one fresh file must
    all succeed (the ``already exists`` retry path stays intact)."""
    db_path = str(tmp_path / "f.sqlite")
    await asyncio.gather(*[create_all_tables(db_path) for _ in range(8)])
    async with get_session(db_path) as session:
        session.add(PendingApproval(change_type="f", description="after-race"))
        await session.commit()
    assert await _row_count(db_path) == 1


# ── G. approval model exists after lifecycle reset (the CI failure) ────


@pytest.mark.asyncio
async def test_g_approval_roundtrip_after_full_lifecycle_reset(tmp_path: Path) -> None:
    """Hermetic version of the CI failure: dispose everything, then prove the
    approval table is usable on a fresh path (no reliance on suite order)."""
    db_path = str(tmp_path / "g.sqlite")
    # Full reset mid-test (engine + factory + schema cache + settings).
    if db_module._engine is not None:
        await db_module._engine.dispose()
    db_module._engine = None
    db_module._engine_path = None
    db_module._session_factory = None
    db_module._initialized_paths.clear()
    settings_module.get_settings.cache_clear()

    async with get_session(db_path) as session:
        session.add(PendingApproval(change_type="g", description="post-reset"))
        await session.commit()
        await session.refresh((await session.execute(select(PendingApproval))).scalars().one())
    assert await _row_count(db_path) == 1


# ── H. relative-path CWD isolation (the exact defect) ──────────────────


@pytest.mark.asyncio
async def test_h_same_relative_path_under_different_cwds_gets_two_schemas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The core regression: ``data/app.sqlite`` relative to dir-A and dir-B
    must be treated as two different databases, each with its own schema."""
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()

    monkeypatch.chdir(dir_a)
    async with get_session("data/app.sqlite") as session:
        session.add(PendingApproval(change_type="h", description="in-a"))
        await session.commit()

    # Simulate conftest disposal between the two "tests".
    await db_module._engine.dispose()
    db_module._engine = None
    db_module._engine_path = None
    db_module._session_factory = None

    monkeypatch.chdir(dir_b)
    # Must NOT skip creation due to dir-A's cache entry.
    async with get_session("data/app.sqlite") as session:
        assert (await session.execute(select(PendingApproval))).scalars().all() == []
        session.add(PendingApproval(change_type="h", description="in-b"))
        await session.commit()

    assert (dir_a / "data" / "app.sqlite").exists()
    assert (dir_b / "data" / "app.sqlite").exists()

    # Each file holds only its own row.
    monkeypatch.chdir(dir_a)
    await db_module._engine.dispose()
    db_module._engine = None
    db_module._engine_path = None
    db_module._session_factory = None
    async with get_session("data/app.sqlite") as session:
        rows = (await session.execute(select(PendingApproval))).scalars().all()
        assert [r.description for r in rows] == ["in-a"]


# ── I. normalization unit contract ─────────────────────────────────────


def test_i_normalize_returns_absolute_canonical_identity(tmp_path: Path) -> None:
    rel = "data/app.sqlite"
    abs_a = _normalize_sqlite_path(rel)
    assert Path(abs_a).is_absolute(), f"must be absolute: {abs_a}"
    # Different spellings of the same file collapse to one key.
    assert _normalize_sqlite_path("./data/app.sqlite") == abs_a
    assert _normalize_sqlite_path("data//app.sqlite") == abs_a
    # Tilde expands.
    assert "~" not in _normalize_sqlite_path("~/x.sqlite")
    # Absolute input is stable.
    target = str(tmp_path / "i.sqlite")
    assert _normalize_sqlite_path(target) == str(Path(target).resolve())
