"""Concurrent ``nexus migrate`` on one database (Phase D exit criteria).

Two processes can reach the same database at once — a bot booting while an
operator runs ``nexus migrate`` by hand, or two replicas of a stateless
deployment starting together.  These tests drive real subprocesses (not
threads) so the processes have separate connections, separate Alembic runs and
separate event loops, exactly like production.

What is asserted, and what deliberately is not
----------------------------------------------
The invariant that matters is **the database ends up valid**, not "exactly one
process fails".  Which process loses a race is timing-dependent, and on SQLite
the loser may simply find the revision already applied and exit 0.  Asserting a
specific loser would be a flaky test that tells nobody anything.  So:

* at least one process must succeed;
* afterwards there must be exactly **one** ``alembic_version`` row, at head;
* the schema must be complete (no half-created table set);
* any process that did fail must have said why.
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

#: Chain head — migrations/versions/2a1c4b6d8e9f_pgvector.py.
_HEAD = "2a1c4b6d8e9f"

_WORKERS = 4
_TIMEOUT_SECONDS = 180


def _run_migrates(db_file: Path, count: int) -> list[subprocess.CompletedProcess[str]]:
    """Launch ``count`` concurrent ``nexus migrate`` processes and wait for all."""
    env = dict(os.environ)
    env["NEXUS_DB_PATH"] = str(db_file)
    env.pop("NEXUS_DATABASE_URL", None)
    env.pop("DATABASE_URL", None)
    # Keep each child out of the parent's bytecode cache races.
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
        results.append(
            subprocess.CompletedProcess(
                args=process.args,
                returncode=process.returncode,
                stdout=stdout,
                stderr=None,
            )
        )
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


class TestFreshDatabaseRace:
    def test_concurrent_migrates_leave_a_valid_database(self, tmp_path: Path) -> None:
        db_file = tmp_path / "race.sqlite"

        results = _run_migrates(db_file, _WORKERS)
        outcomes = [result.returncode for result in results]

        assert 0 in outcomes, f"every concurrent migrate failed: {[r.stdout for r in results]}"

        tables, versions = _final_state(db_file)
        assert versions == [_HEAD], f"expected exactly one stamp at head, got {versions}"
        assert set(SQLModel.metadata.tables) <= tables

    def test_a_loser_explains_itself(self, tmp_path: Path) -> None:
        """A failing process must report a concurrency reason, not a silent crash."""
        db_file = tmp_path / "race.sqlite"
        results = _run_migrates(db_file, _WORKERS)

        for result in results:
            if result.returncode != 0:
                assert result.stdout.strip(), "a failing migrate must print something"


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
        assert 0 in codes, f"every concurrent migrate failed: {[r.stdout[-800:] for r in results]}"

        _, versions = _final_state(legacy_db)
        assert len(versions) == 1, (
            f"the stamp must be single-valued, got {versions}; "
            f"exit codes {codes}; output {[r.stdout[-800:] for r in results]}"
        )
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
        assert 0 in [result.returncode for result in results], [r.stdout for r in results]

        tables, versions = _final_state(db_file)
        assert versions == [_HEAD]
        assert set(SQLModel.metadata.tables) <= tables
