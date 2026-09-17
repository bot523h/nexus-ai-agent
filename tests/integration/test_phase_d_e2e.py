"""Phase-D exit integrity tests: fresh/legacy SQLite + Postgres adoption (mock).

Covers the full bootstrap matrix of Phase D so the merge to main is gated on
end-to-end behaviour, not unit tests alone:

* fresh SQLite install → `nexus migrate` → tables + alembic_version
* legacy SQLite (no stamp) → adoption preserves data + stamps correctly
* Postgres adoption (mocked connection) → zero-drift adopt, drift fail-fast
* `nexus migrate` idempotency (second run is a clean no-op)
* concurrent `nexus migrate` — one wins, both exit deterministically
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest


def _nexus(*args: str, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "nexus_ai_agent.cli", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


@pytest.fixture()
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    import os as _os

    env = dict(_os.environ)
    env["NEXUS_DB_PATH"] = str(tmp_path / "data" / "app.sqlite")
    env["NEXUS_CHECKPOINT_PATH"] = str(tmp_path / "data" / "checkpoint.sqlite")
    env["NEXUS_VECTOR_PATH"] = str(tmp_path / "data" / "vector")
    env.pop("NEXUS_DATABASE_URL", None)
    return env


class TestSqliteFreshInstall:
    def test_fresh_migrate_creates_schema_and_stamp(self, cli_env: dict[str, str]) -> None:
        result = _nexus("migrate", cwd=Path.cwd(), env=cli_env)
        assert result.returncode == 0, result.stderr
        db = Path(cli_env["NEXUS_DB_PATH"])
        assert db.exists()
        con = sqlite3.connect(db)
        try:
            tables = {
                r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            assert "alembic_version" in tables
            assert "user" in tables
            assert "chat" in tables
        finally:
            con.close()


class TestSqliteLegacy:
    def test_legacy_db_adopted_preserves_data(self, cli_env: dict[str, str]) -> None:
        db_path = Path(cli_env["NEXUS_DB_PATH"])
        db_path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(db_path)
        con.execute("CREATE TABLE chat (id INTEGER PRIMARY KEY, user_id INTEGER)")
        con.execute("INSERT INTO chat (id, user_id) VALUES (42, 7)")
        con.commit()
        con.close()

        result = _nexus("migrate", cwd=Path.cwd(), env=cli_env)
        assert result.returncode == 0, result.stderr
        con = sqlite3.connect(db_path)
        try:
            row = con.execute("SELECT id, user_id FROM chat WHERE id = 42").fetchone()
            assert row == (42, 7)  # data preserved through adoption
            version = con.execute("SELECT version_num FROM alembic_version").fetchone()
            assert version is not None
        finally:
            con.close()


class TestPostgresAdoptionMock:
    def test_zero_drift_reports_adopt(self) -> None:
        from nexus_ai_agent.storage.adopt_pg import ACTION_ADOPT, PostgresAdoptionReport, decide

        report = PostgresAdoptionReport(
            database="postgresql://example",
            action="inspect_only",
            alembic_stamped=False,
            table_count=29,
            missing_tables=[],
            extra_tables=[],
        )
        assert decide(report) == ACTION_ADOPT

    def test_drift_reports_fail_fast(self) -> None:
        from nexus_ai_agent.storage.adopt_pg import ACTION_FAIL, PostgresAdoptionReport, decide

        report = PostgresAdoptionReport(
            database="postgresql://example",
            action="inspect_only",
            alembic_stamped=False,
            table_count=10,
            missing_tables=["user"],
            extra_tables=[],
        )
        assert decide(report) == ACTION_FAIL


class TestMigrateIdempotency:
    def test_second_migrate_is_clean_noop(self, cli_env: dict[str, str]) -> None:
        first = _nexus("migrate", cwd=Path.cwd(), env=cli_env)
        assert first.returncode == 0, first.stderr
        second = _nexus("migrate", cwd=Path.cwd(), env=cli_env)
        assert second.returncode == 0, second.stderr


class TestMigrateRace:
    def test_concurrent_migrate_terminates_deterministically(self, cli_env: dict[str, str]) -> None:
        # Two migrators on one database must serialize: the lock holder runs
        # migrate to completion; any true overlap fails fast with a clear
        # message (never a raw DBAPI crash).  A serial fallback is fine only
        # because migrate is idempotent.
        results: list[tuple[int, str]] = []
        procs = [
            subprocess.Popen(
                [sys.executable, "-m", "nexus_ai_agent.cli", "migrate"],
                cwd=Path.cwd(),
                env=cli_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(2)
        ]
        for p in procs:
            _, err = p.communicate(timeout=120)
            results.append((p.returncode, err))

        codes = sorted(rc for rc, _ in results)
        assert codes[0] == 0, f"at least one migrator must win: {codes}"
        assert codes[-1] in (0, 1), f"loser must fail fast (1) or run serially (0): {codes}"
        # The losing process must surface the actionable message, not a raw
        # DBAPI raise.
        losers = [err for rc, err in results if rc != 0]
        assert all("already in progress" in err for err in losers), losers
