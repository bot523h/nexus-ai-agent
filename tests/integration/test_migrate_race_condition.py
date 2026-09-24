"""Concurrent ``nexus migrate`` on one database.

Two processes can reach the same database at once — a bot booting while an
operator runs ``nexus migrate`` by hand, or two replicas starting together.
These tests drive real subprocesses (not threads), so the processes have
separate connections, separate Alembic runs and separate event loops, exactly
like production.

What is asserted, and what deliberately is not
----------------------------------------------
The invariant that matters is **the database ends up valid** and **any loser
says why**.  Which process loses is timing-dependent, and ``migration_lock()``
is non-blocking, so a racer may simply arrive after the winner finished and
exit 0.  Asserting a specific loser would be a flaky test that tells nobody
anything.  So:

* at least one process must succeed;
* afterwards there must be exactly **one** ``alembic_version`` row, at head;
* the schema must be complete (no half-created table set);
* any process that did fail must fail with the lock's actionable message, never
  with a raw ``table … already exists`` from inside Alembic.

That last point is the regression guard: before ``migration_lock`` and the
``create_all`` retry, all four racers died on ``table message already exists``.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlmodel import SQLModel

import nexus_ai_agent.storage.db  # noqa: F401  (registers every SQLModel table)

#: Chain head — migrations/versions/a41c9e2b7f63_stateful_scale_zero.py.
_HEAD = "a41c9e2b7f63"

_WORKERS = 4
_TIMEOUT_SECONDS = 180

#: The message ``migration_lock`` raises when another process holds the lock.
_LOCK_MESSAGE = "already in progress"


def _run_migrates(db_file: Path, count: int) -> list[subprocess.CompletedProcess[str]]:
    """Launch ``count`` concurrent ``nexus migrate`` processes and wait for all."""
    env = dict(os.environ)
    env["NEXUS_DB_PATH"] = str(db_file)
    env.pop("NEXUS_DATABASE_URL", None)
    env.pop("DATABASE_URL", None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"

    processes = [
        subprocess.Popen(  # noqa: S603 — fixed argv, no shell
            [sys.executable, "-m", "nexus_ai_agent.cli", "migrate"],
            env=env,
            cwd=str(db_file.parent),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        for _ in range(count)
    ]
    results: list[subprocess.CompletedProcess[str]] = []
    for process in processes:
        stdout, _ = process.communicate(timeout=_TIMEOUT_SECONDS)
        results.append(subprocess.CompletedProcess(process.args, process.returncode, stdout, None))
    return results


def _final_state(db_file: Path) -> tuple[set[str], list[str]]:
    conn = sqlite3.connect(db_file)
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "alembic_version" not in tables:
            return tables, []
        return tables, [r[0] for r in conn.execute("SELECT version_num FROM alembic_version")]
    finally:
        conn.close()


def _assert_losers_explained(results: list[subprocess.CompletedProcess[str]]) -> None:
    for result in results:
        if result.returncode != 0:
            assert _LOCK_MESSAGE in result.stdout, (
                "a failing migrate must report the lock, not a raw DDL error; got:\n"
                + result.stdout[-1200:]
            )


class TestFreshDatabaseRace:
    def test_concurrent_migrates_leave_a_valid_database(self, tmp_path: Path) -> None:
        db_file = tmp_path / "race.sqlite"

        results = _run_migrates(db_file, _WORKERS)
        codes = [result.returncode for result in results]
        assert 0 in codes, f"every concurrent migrate failed: {[r.stdout[-600:] for r in results]}"

        tables, versions = _final_state(db_file)
        assert versions == [_HEAD], f"expected exactly one stamp at head, got {versions}"
        assert set(SQLModel.metadata.tables) <= tables

    def test_a_loser_reports_the_lock_not_a_ddl_error(self, tmp_path: Path) -> None:
        db_file = tmp_path / "race.sqlite"
        _assert_losers_explained(_run_migrates(db_file, _WORKERS))


class TestLegacyAdoptionRace:
    """The D6 adoption path is the dangerous one: create_all *then* stamp."""

    @pytest.fixture()
    def legacy_db(self, tmp_path: Path) -> Path:
        db_file = tmp_path / "legacy_race.sqlite"
        conn = sqlite3.connect(db_file)
        conn.execute(
            "CREATE TABLE pendingapproval ("
            "id INTEGER PRIMARY KEY, change_type VARCHAR NOT NULL,"
            "description VARCHAR NOT NULL, created_at DATETIME NOT NULL,"
            "status VARCHAR NOT NULL, auto_apply_at DATETIME)"
        )
        for index in range(3):
            conn.execute(
                "INSERT INTO pendingapproval (change_type, description, created_at, status)"
                " VALUES ('legacy', ?, '2025-01-01 00:00:00', 'pending')",
                (f"row {index}",),
            )
        conn.commit()
        conn.close()
        return db_file

    def test_concurrent_adoption_never_duplicates_the_stamp(self, legacy_db: Path) -> None:
        results = _run_migrates(legacy_db, _WORKERS)
        codes = [result.returncode for result in results]
        assert 0 in codes, f"every concurrent migrate failed: {[r.stdout[-600:] for r in results]}"

        _, versions = _final_state(legacy_db)
        assert len(versions) == 1, f"the stamp must be single-valued, got {versions}"
        assert versions == [_HEAD]

    def test_concurrent_adoption_preserves_every_row(self, legacy_db: Path) -> None:
        _run_migrates(legacy_db, _WORKERS)

        conn = sqlite3.connect(legacy_db)
        count = conn.execute("SELECT COUNT(*) FROM pendingapproval").fetchone()[0]
        conn.close()
        assert count == 3, "concurrent adoption must not lose or duplicate user rows"

    def test_a_partially_created_database_is_not_left_behind(self, tmp_path: Path) -> None:
        """A file with *some* tables must still converge to the full schema."""
        db_file = tmp_path / "partial_race.sqlite"
        engine = create_engine(f"sqlite:///{db_file}")
        SQLModel.metadata.create_all(engine, tables=[SQLModel.metadata.tables["chat"]])
        engine.dispose()

        results = _run_migrates(db_file, _WORKERS)
        codes = [result.returncode for result in results]
        assert 0 in codes, f"every concurrent migrate failed: {[r.stdout[-600:] for r in results]}"

        tables, versions = _final_state(db_file)
        assert versions == [_HEAD]
        assert set(SQLModel.metadata.tables) <= tables
