"""Production backend policy: NEXUS_REQUIRE_POSTGRES fails closed (mission P0).

The dangerous state this guards: a production deployment that never received
``NEXUS_DATABASE_URL`` silently degrading to a local SQLite file it cannot
persist. Development, tests and CI must keep working on SQLite — the policy is
opt-in, so a missing flag never changes existing behaviour.

Contracts under test
--------------------
* ``enforce_postgres_policy`` returns the URL when set, ``None`` when unset and
  not required, and raises ``BackendPolicyError`` when unset *and* required.
* ``selected_backend`` reports the intended backend: ``postgresql`` whenever a
  URL is set *or* PostgreSQL is required (never a healthy "sqlite" for a broken
  production deployment).
* ``get_session`` and ``resolve_migration_url`` fail closed under the policy.
* ``/readyz`` reports ``db.backend == "postgresql"`` and 503 when the policy is
  in force but no URL is configured — never a green SQLite payload.
* The default (flag absent) is unchanged: SQLite everywhere.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from nexus_ai_agent.api import app as app_module
from nexus_ai_agent.config import settings as settings_module
from nexus_ai_agent.storage import db as db_module
from nexus_ai_agent.storage.db import (
    BackendPolicyError,
    enforce_postgres_policy,
    get_session,
    resolve_migration_url,
    selected_backend,
)

VALID_URL = "postgresql://nexus:secret@db.example.com:5432/nexusdb"


@pytest.fixture(autouse=True)
def _clean_db_env(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Isolate from inherited env values and cached settings."""
    monkeypatch.delenv("NEXUS_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("NEXUS_REQUIRE_POSTGRES", raising=False)
    monkeypatch.delenv("REQUIRE_POSTGRES", raising=False)
    settings_module.get_settings.cache_clear()
    yield
    settings_module.get_settings.cache_clear()


class TestEnforcePostgresPolicy:
    def test_url_set_returns_url_regardless_of_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NEXUS_DATABASE_URL", VALID_URL)
        assert enforce_postgres_policy() == VALID_URL
        monkeypatch.setenv("NEXUS_REQUIRE_POSTGRES", "true")
        assert enforce_postgres_policy() == VALID_URL

    def test_unset_without_flag_returns_none(self) -> None:
        # Development / CI default: no flag → SQLite fallback is authorized.
        assert enforce_postgres_policy() is None

    def test_unset_with_flag_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NEXUS_REQUIRE_POSTGRES", "true")
        with pytest.raises(BackendPolicyError, match="refusing the SQLite fallback"):
            enforce_postgres_policy()

    def test_blank_url_with_flag_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The untouched .env.example has NEXUS_DATABASE_URL= (blank). Under the
        # policy a blank value is "not configured" and must fail closed.
        monkeypatch.setenv("NEXUS_DATABASE_URL", "")
        monkeypatch.setenv("NEXUS_REQUIRE_POSTGRES", "true")
        with pytest.raises(BackendPolicyError):
            enforce_postgres_policy()

    @pytest.mark.parametrize("flag", ["true", "True", "1", "yes", "on"])
    def test_flag_truthy_forms(self, monkeypatch: pytest.MonkeyPatch, flag: str) -> None:
        monkeypatch.setenv("NEXUS_REQUIRE_POSTGRES", flag)
        with pytest.raises(BackendPolicyError):
            enforce_postgres_policy()

    def test_legacy_alias_require_postgres(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("REQUIRE_POSTGRES", "true")
        with pytest.raises(BackendPolicyError):
            enforce_postgres_policy()


class TestSelectedBackend:
    def test_sqlite_when_nothing_configured(self) -> None:
        assert selected_backend() == "sqlite"

    def test_postgresql_when_url_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NEXUS_DATABASE_URL", VALID_URL)
        assert selected_backend() == "postgresql"

    def test_postgresql_when_required_but_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The intended backend is postgresql even though it cannot connect —
        # readiness must never present this as a healthy SQLite deployment.
        monkeypatch.setenv("NEXUS_REQUIRE_POSTGRES", "true")
        assert selected_backend() == "postgresql"


class TestGetSessionFailsClosed:
    @pytest.mark.asyncio
    async def test_required_but_unset_raises_before_sqlite(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NEXUS_REQUIRE_POSTGRES", "true")
        created: list[str] = []

        async def _fail_create_all(db_path: str = "data/app.sqlite") -> None:
            created.append(db_path)

        monkeypatch.setattr(db_module, "create_all_tables", _fail_create_all)
        with pytest.raises(BackendPolicyError):
            async with get_session():
                pass
        assert created == [], "the SQLite path must never be touched under the policy"

    @pytest.mark.asyncio
    async def test_explicit_db_path_still_allowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # An explicit path is a deliberate local target (tests/CLI), not a
        # silent production fallback — the policy must not block it.
        monkeypatch.setenv("NEXUS_REQUIRE_POSTGRES", "true")
        monkeypatch.setattr(db_module, "create_all_tables", _noop_create_all)
        monkeypatch.setattr(db_module, "_session_factory", _FakeFactory())

        async with get_session("/tmp/explicit.sqlite") as session:
            assert session == "explicit-session"


class TestMigrationUrlFailsClosed:
    def test_required_but_unset_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NEXUS_REQUIRE_POSTGRES", "true")
        with pytest.raises(BackendPolicyError):
            resolve_migration_url()

    def test_default_sqlite_still_works(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NEXUS_DB_PATH", str(tmp_path / "app.sqlite"))
        settings_module.get_settings.cache_clear()
        assert resolve_migration_url().startswith("sqlite+aiosqlite:///")


class _FakeSession:
    async def __aenter__(self) -> str:
        return "explicit-session"

    async def __aexit__(self, *exc: object) -> None:
        return None


class _FakeFactory:
    def __call__(self) -> _FakeSession:
        return _FakeSession()


async def _noop_create_all(db_path: str = "data/app.sqlite") -> None:
    return None


class TestReadyzReportsIntendedBackend:
    def test_readyz_503_and_postgresql_backend_when_required_but_unset(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("NEXUS_REQUIRE_POSTGRES", "true")
        monkeypatch.setenv("NEXUS_DB_PATH", str(tmp_path / "app.sqlite"))
        settings_module.get_settings.cache_clear()
        saved = getattr(app_module.app.state, "runtime_application", None)
        app_module.app.state.runtime_application = _ReadyApp()
        try:
            response = TestClient(app_module.app).get("/readyz")
        finally:
            if saved is None:
                try:
                    del app_module.app.state.runtime_application
                except AttributeError:
                    pass
            else:
                app_module.app.state.runtime_application = saved

        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "not_ready"
        assert body["db"]["ok"] is False
        assert body["db"]["backend"] == "postgresql"
        assert "required" in (body["db"]["detail"] or "")


class _ReadyApp:
    bot_data = {"startup_status": {"state": "ready", "degraded": False}}
