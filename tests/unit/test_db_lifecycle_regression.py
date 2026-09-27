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
import os
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


# ── J. E2/E2b: file replaced after engine disposal → re-initialised ────


@pytest.mark.asyncio
async def test_j_file_replaced_under_a_disposed_engine_is_reinitialised(
    tmp_path: Path,
) -> None:
    """E2 close-out: the schema claim is owned by the engine, not by the path
    string.  An externally replaced file (delete + empty recreate) after an
    engine disposal must get its schema back automatically — previously the
    absolute path stayed in ``_initialized_paths`` for the rest of the
    process and every session failed with ``no such table`` until restart.
    Mutation killer: gating on ``_initialized_paths`` instead of the engine
    makes this test RED (``OperationalError: no such table``)."""
    db_path = str(tmp_path / "j.sqlite")
    async with get_session(db_path) as session:
        session.add(PendingApproval(change_type="j", description="original"))
        await session.commit()

    # conftest._dispose_db_engine equivalent: engine dies, process record survives.
    assert db_module._engine is not None
    await db_module._engine.dispose()
    db_module._engine = None
    db_module._engine_path = None
    db_module._session_factory = None
    assert _normalize_sqlite_path(db_path) in db_module._initialized_paths

    # External replacement while the path is still cached.
    Path(db_path).unlink()
    Path(db_path).touch()

    # No restart: the next session must heal (schema recreated, file usable).
    async with get_session(db_path) as session:
        assert (await session.execute(select(PendingApproval))).scalars().all() == []
        session.add(PendingApproval(change_type="j", description="healed"))
        await session.commit()
    assert await _row_count(db_path) == 1


# ── K. E3b: atomic restore of a schema-bearing backup preserves rows ────


@pytest.mark.asyncio
async def test_k_atomic_schema_bearing_restore_preserves_rows(tmp_path: Path) -> None:
    """Anti-overcorrection guard: re-initialisation is checkfirst-idempotent,
    so healing after an atomic restore (``os.replace`` / ``mv``) must never
    clobber the data that arrived inside the backup file.

    The restore protocol this pins: every engine touching the file is
    disposed first (graceful close checkpoints WAL and removes the
    ``-wal``/``-shm`` sidecars), THEN the file is swapped.  Swapping over
    live WAL sidecars replays foreign frames against the new file — silent
    data mixing that NO schema gate can see; that belongs to the operational
    boundary documented on ``create_all_tables``."""
    live = str(tmp_path / "live.sqlite")
    backup = str(tmp_path / "backup.sqlite")

    # A live, cached DB existed earlier in this process.
    async with get_session(live) as session:
        session.add(PendingApproval(change_type="k", description="stale-live-row"))
        await session.commit()
    # The backup was built off-line in the same process (live engine retired).
    async with get_session(backup) as session:
        session.add(PendingApproval(change_type="k", description="from-backup"))
        await session.commit()

    # Quiesce: dispose the current (backup) engine AND the retired (live)
    # engine so WAL checkpoints flush and sidecars disappear on both files.
    assert db_module._engine is not None
    await db_module._engine.dispose()
    db_module._engine = None
    db_module._engine_path = None
    db_module._session_factory = None
    while db_module._replaced_engines:
        await db_module._replaced_engines.pop().dispose()
    assert not Path(live + "-wal").exists()
    assert not Path(backup + "-wal").exists()

    os.replace(backup, live)

    async with get_session(live) as session:
        rows = (await session.execute(select(PendingApproval))).scalars().all()
        assert [r.description for r in rows] == ["from-backup"]


# ── M. engine identity: one engine = one absolute file (no disposal) ────


@pytest.mark.asyncio
async def test_m_engine_stays_bound_to_absolute_file_across_cwd_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pins ``resolve()`` inside ``_get_engine`` itself, WITHOUT any disposal
    between CWD changes: the engine-path key is the absolute file identity,
    so a CWD change RETIRES the engine instead of silently reusing a relative
    URL against a different working directory.  Mutation killer: expanding
    the key with ``expanduser`` only (no ``resolve()``) reuses dir-A's engine
    in dir-B and this test reads dir-A's row where an empty database is
    expected → RED."""
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()

    monkeypatch.chdir(dir_a)
    async with get_session("data/app.sqlite") as session:
        session.add(PendingApproval(change_type="m", description="in-a"))
        await session.commit()

    # NO disposal: only the absolute binding may separate the two files.
    monkeypatch.chdir(dir_b)
    async with get_session("data/app.sqlite") as session:
        assert (await session.execute(select(PendingApproval))).scalars().all() == []
        session.add(PendingApproval(change_type="m", description="in-b"))
        await session.commit()

    monkeypatch.chdir(dir_a)
    async with get_session("data/app.sqlite") as session:
        rows = (await session.execute(select(PendingApproval))).scalars().all()
        assert [r.description for r in rows] == ["in-a"]


# ── L. E3a: atomic replace with an EMPTY file → re-initialised ──────────


@pytest.mark.asyncio
async def test_l_atomic_replace_with_empty_file_is_reinitialised(tmp_path: Path) -> None:
    """E3a: swap-through-rename of an empty file (operator restoring a blank
    DB) after engine disposal must re-initialise exactly like delete+recreate
    (test J) — the evidence entry is the file replacement, not its mechanism."""
    live = str(tmp_path / "live.sqlite")
    blank = tmp_path / "blank.sqlite"
    blank.touch()

    async with get_session(live) as session:
        session.add(PendingApproval(change_type="l", description="before"))
        await session.commit()
    await db_module._engine.dispose()
    db_module._engine = None
    db_module._engine_path = None
    db_module._session_factory = None

    os.replace(blank, live)

    async with get_session(live) as session:
        assert (await session.execute(select(PendingApproval))).scalars().all() == []
        session.add(PendingApproval(change_type="l", description="healed"))
        await session.commit()
    assert await _row_count(live) == 1
