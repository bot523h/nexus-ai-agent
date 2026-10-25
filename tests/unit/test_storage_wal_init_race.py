"""Regression: SQLite first-use WAL init must absorb a concurrent writer.

``create_all_tables`` switches a fresh database into WAL with a bare
``PRAGMA journal_mode=WAL``.  That transition needs a brief exclusive lock, so
when a second caller is already holding a write transaction at first-use time
the loser gets ``sqlite3.OperationalError: database is locked`` -- exactly the
intermittent failure seen on CI (``test_r_f28_concurrent_identical_learns_collapse_into_one``).

These tests pin the failure deterministically: a raw sqlite3 connection holds a
``BEGIN IMMEDIATE`` write lock on the target path, and first-use initialization
is asserted to succeed anyway (the contention is retried) and to leave the
database in WAL mode.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from pathlib import Path

import pytest
from sqlalchemy import text

from nexus_ai_agent.storage import db as db_module
from nexus_ai_agent.storage.db import create_all_tables, get_session


def _journal_mode(db_path: Path) -> str:
    conn = sqlite3.connect(db_path)
    try:
        return str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
    finally:
        conn.close()


class _WriteLockHolder:
    """Hold a write transaction on ``db_path`` for ``hold_seconds``, then release."""

    def __init__(self, db_path: Path, hold_seconds: float) -> None:
        self._db_path = db_path
        self._hold_seconds = hold_seconds
        self._ready = threading.Event()
        self._released = threading.Event()

    def __enter__(self) -> _WriteLockHolder:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        assert self._ready.wait(timeout=5), "lock holder never acquired the write lock"
        return self

    def _run(self) -> None:
        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("CREATE TABLE IF NOT EXISTS _lock_probe (x INTEGER)")
            conn.execute("INSERT INTO _lock_probe VALUES (1)")
            self._ready.set()
            time.sleep(self._hold_seconds)
            conn.commit()
        finally:
            self._released.set()
            conn.close()

    def __exit__(self, *exc: object) -> None:
        self._released.wait(timeout=5)
        self._thread.join(timeout=5)


@pytest.mark.asyncio
async def test_first_use_wal_survives_a_concurrent_writer(tmp_path: Path) -> None:
    """Deterministic RED before the fix: the bare pragma raises 'database is locked'."""
    db_path = tmp_path / "contended.sqlite"
    db_module._initialized_paths.discard(str(db_path))
    # Materialise the file so the holder's BEGIN IMMEDIATE takes a real lock.
    sqlite3.connect(db_path).close()

    with _WriteLockHolder(db_path, hold_seconds=0.4):
        await create_all_tables(str(db_path))

    assert _journal_mode(db_path) == "wal"
    async with get_session(str(db_path)) as session:
        await session.exec(text("SELECT 1"))  # type: ignore[call-overload]


@pytest.mark.asyncio
async def test_concurrent_first_use_sets_wal_without_error(tmp_path: Path) -> None:
    """Several callers booting at once on one fresh path must all succeed."""
    db_path = tmp_path / "stampede.sqlite"
    db_module._initialized_paths.discard(str(db_path))
    sqlite3.connect(db_path).close()

    with _WriteLockHolder(db_path, hold_seconds=0.4):
        results = await asyncio.gather(
            *[create_all_tables(str(db_path)) for _ in range(6)],
            return_exceptions=True,
        )

    errors = [repr(r) for r in results if isinstance(r, BaseException)]
    assert errors == [], f"concurrent first use failed: {errors}"
    assert _journal_mode(db_path) == "wal"


@pytest.mark.asyncio
async def test_wal_retry_absorbs_contention_longer_than_the_busy_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bounded retry -- not the busy timeout -- carries this case.

    With the busy timeout shrunk below the hold, only the retry loop can absorb
    the lock; removing the retry turns this red.
    """
    monkeypatch.setattr(db_module, "_SQLITE_BUSY_TIMEOUT_SECONDS", 0.1)
    db_path = tmp_path / "slow.sqlite"
    db_module._initialized_paths.discard(str(db_path))
    sqlite3.connect(db_path).close()

    with _WriteLockHolder(db_path, hold_seconds=0.8):
        await create_all_tables(str(db_path))

    assert _journal_mode(db_path) == "wal"


@pytest.mark.asyncio
async def test_stampede_of_first_use_never_surfaces_schema_changed(tmp_path: Path) -> None:
    """Behavioural contract: N coroutines booting one fresh path all succeed.

    Regression for ``sqlite3.OperationalError: database schema has changed``
    (SQLITE_SCHEMA) seen on CI ``python-parity``: unserialised coroutines ran
    ``create_all`` DDL concurrently, so a pooled connection's prepared
    statement went stale.  Repeated over many fresh paths because the race is
    probabilistic; no internal function is mocked.
    """
    failures: list[str] = []
    for i in range(25):
        db_path = tmp_path / f"stampede-{i}.sqlite"
        db_module._initialized_paths.discard(str(db_path))
        sqlite3.connect(db_path).close()
        results = await asyncio.gather(
            *[create_all_tables(str(db_path)) for _ in range(8)],
            *[_use_session(str(db_path)) for _ in range(4)],
            return_exceptions=True,
        )
        failures += [repr(r) for r in results if isinstance(r, BaseException)]
        assert _journal_mode(db_path) == "wal"
    assert failures == [], f"first-use stampede failed: {failures[:3]}"


async def _use_session(db_path: str) -> None:
    async with get_session(db_path) as session:
        await session.execute(text("SELECT count(*) FROM user"))


@pytest.mark.asyncio
async def test_coroutine_stampede_runs_schema_ddl_once(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Serialisation, not retry, resolves in-loop races: no lost-race retries."""
    db_path = tmp_path / "serial.sqlite"
    db_module._initialized_paths.discard(str(db_path))
    sqlite3.connect(db_path).close()
    with caplog.at_level("WARNING", logger=db_module.log.name):
        await asyncio.gather(*[create_all_tables(str(db_path)) for _ in range(8)])
    retries = [r for r in caplog.records if "concurrent schema creation" in r.getMessage()]
    assert retries == [], f"in-loop race should not need retries: {len(retries)}"


def test_separate_processes_initialising_one_path_all_succeed(tmp_path: Path) -> None:
    """Cross-process contract: an in-process lock cannot cover this case.

    Independent interpreters boot the same fresh database at once; the
    idempotent ``create_all`` plus conflict retry must absorb the race.
    """
    import subprocess
    import sys

    db_path = tmp_path / "multiproc.sqlite"
    sqlite3.connect(db_path).close()
    code = (
        "import asyncio,sys;"
        "from nexus_ai_agent.storage.db import create_all_tables;"
        "asyncio.run(create_all_tables(sys.argv[1]))"
    )
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", code, str(db_path)],
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(4)
    ]
    outputs = [(p.wait(timeout=60), p.stderr.read() if p.stderr else "") for p in procs]
    bad = [(rc, err[-400:]) for rc, err in outputs if rc != 0]
    assert bad == [], f"cross-process first use failed: {bad}"
    assert _journal_mode(db_path) == "wal"


@pytest.mark.parametrize(
    ("message", "is_conflict"),
    [
        ("table user already exists", True),
        ("database schema has changed", True),
        ("no such table: user", False),
        ("database disk image is malformed", False),
        ('near "X": syntax error', False),
        ("database is locked", False),
    ],
)
def test_only_benign_race_signatures_count_as_create_conflicts(
    message: str, is_conflict: bool
) -> None:
    """The retry classifier must stay narrow: real schema/DDL errors are not races."""
    from sqlalchemy.exc import OperationalError

    exc = OperationalError("stmt", {}, Exception(message))
    assert db_module._is_concurrent_create_conflict(exc) is is_conflict


@pytest.mark.asyncio
async def test_persistent_schema_changed_is_bounded_then_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A conflict that never resolves is not a race: it raises after the bound."""
    from sqlalchemy import MetaData
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.ext.asyncio import create_async_engine

    monkeypatch.setattr(db_module, "_CREATE_ALL_ATTEMPTS", 3)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'p.sqlite'}")
    attempts = 0

    class _AlwaysStale(MetaData):
        def create_all(self, *args: object, **kwargs: object) -> None:  # type: ignore[override]
            nonlocal attempts
            attempts += 1
            raise OperationalError("stmt", {}, Exception("database schema has changed"))

    try:
        with pytest.raises(OperationalError):
            await db_module.create_all_metadata(engine, _AlwaysStale())
    finally:
        await engine.dispose()
    assert attempts == 3
