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
        await session.execute(text("SELECT 1"))


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
