"""Unit tests for the Postgres adoption / fail-fast layer (D10)."""

from __future__ import annotations

import pytest

from nexus_ai_agent.storage.adopt_pg import (
    ACTION_ADOPT,
    ACTION_FAIL,
    ACTION_MANAGED,
    ACTION_MIGRATE,
    PostgresAdoptionReport,
    _redacted_url,
    decide,
)


def _report(
    stamped: bool = False,
    tables: int = 0,
    missing: list[str] | None = None,
    extra: list[str] | None = None,
) -> PostgresAdoptionReport:
    return PostgresAdoptionReport(
        database="postgresql://example",
        action="inspect_only",
        alembic_stamped=stamped,
        table_count=tables,
        missing_tables=missing or [],
        extra_tables=extra or [],
    )


class TestDecideMatrix:
    def test_stamped_database_is_managed(self) -> None:
        assert decide(_report(stamped=True, tables=29)) == ACTION_MANAGED

    def test_empty_database_should_migrate_not_fail(self) -> None:
        # A fresh (0-table) database builds fine via `upgrade head`; failing
        # here would punish new installs.  design decision documented in D10.
        assert decide(_report(stamped=False, tables=0)) == ACTION_MIGRATE

    def test_zero_drift_legacy_is_adopted(self) -> None:
        assert decide(_report(stamped=False, tables=29)) == ACTION_ADOPT

    def test_missing_tables_drift_is_fail_fast(self) -> None:
        assert decide(_report(stamped=False, tables=5, missing=["user", "chat"])) == ACTION_FAIL

    def test_extra_tables_drift_is_fail_fast(self) -> None:
        assert decide(_report(stamped=False, tables=30, extra=["rogue_table"])) == ACTION_FAIL


class TestHeadStateIncludesLifecycleTable:
    def test_expected_set_includes_lifecycle_table(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A head-state database must contain nexus_checkpoint_lifecycle (A)."""
        import asyncio

        import nexus_ai_agent.storage.adopt_pg as mod

        captured: dict[str, object] = {}

        def fake_snapshot(sync_conn, expected, redacted):
            captured["expected"] = set(expected)
            return mod.PostgresAdoptionReport(
                database=redacted,
                action="inspect_only",
                alembic_stamped=False,
                table_count=0,
            )

        class _FakeConn:
            async def run_sync(self, fn):
                return fn(None)

        class _FakeCtx:
            async def __aenter__(self):
                return _FakeConn()

            async def __aexit__(self, *args):
                return False

        class _FakeEngine:
            def connect(self):
                return _FakeCtx()

            async def dispose(self):
                return None

        monkeypatch.setattr(mod, "_snapshot", fake_snapshot)
        monkeypatch.setattr(mod, "create_async_engine", lambda *a, **k: _FakeEngine())

        report = asyncio.run(mod._inspect("postgresql://u:p@h/d"))
        assert report.alembic_stamped is False
        expected = captured["expected"]
        assert "nexus_checkpoint_lifecycle" in expected
        assert "chat" in expected  # ORM tables still expected


class TestRedaction:
    def test_credentials_never_leak(self) -> None:
        leaked = "postgresql://secretuser:secretpass@neon-host:5432/mydb"
        assert "secretuser" not in _redacted_url(leaked)
        assert "secretpass" not in _redacted_url(leaked)
        assert _redacted_url(leaked) == "postgresql://neon-host:5432"


class TestFailFastError:
    def test_drift_error_message_is_actionable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import nexus_ai_agent.storage.adopt_pg as mod

        err = mod.drift_error(_report(stamped=False, tables=3, missing=["chat"], extra=[]))
        assert "nexus adopt-pg --dry-run" in str(err)
        assert "chat" in str(err)
