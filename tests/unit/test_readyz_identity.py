"""Readiness endpoint + deploy-identity truth (mission: production/DR).

Contracts under test
--------------------
* ``GET /readyz`` must answer 200 only when the database is actually usable
  (a real ``SELECT 1`` through a fresh short-lived connection — never the
  app's engine/pool, so the probe cannot mutate the schema).
* The payload must always carry deploy identity (``version``,
  ``deploy_git_sha``) so deploy smoke can detect stale / wrong deployments
  (kill-mutation: serving 200 without identity, or reporting ready while the
  DB is unreachable, must fail tests in this file).
* Failure must be actionable: 503 + a ``db.detail`` reason, never a bare
  ``not_ready``.
* Chaos legs: missing database file, non-database file at the path,
  a probe that hangs (bounded by timeout), identity env var present/absent.
* ``/healthz`` must stay byte-identical static (``{"status": "ok"}``) — the
  liveness contract is pinned by deploy_smoke live checks.
"""

from __future__ import annotations

import time
from importlib.metadata import version as distribution_version

import pytest
from fastapi.testclient import TestClient

from nexus_ai_agent.api import app as app_module
from nexus_ai_agent.config import settings as settings_module


@pytest.fixture()
def ready_client(settings_override) -> TestClient:
    """TestClient backed by a REAL database file with the app schema.

    Readiness semantics: 200 only when the database is usable — the fixture
    creates it the same way the app's first boot does.
    """
    import asyncio

    from nexus_ai_agent.storage.db import create_all_tables

    asyncio.run(create_all_tables(settings_override.db_path))
    return TestClient(app_module.app)


class TestReadyzContract:
    def test_readyz_200_with_real_identity_block(self, ready_client: TestClient):
        response = ready_client.get("/readyz")

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ready"
        assert isinstance(body["version"], str) and body["version"] not in ("", "unknown")
        assert "deploy_git_sha" in body
        assert body["db"] == {"ok": True, "backend": "sqlite", "detail": None}

    def test_readyz_reports_version_from_distribution(self, ready_client: TestClient):
        # The runtime must surface the actual installed version — a captured
        # constant or a hardcoded "ok" would let stale deployments lie.
        assert ready_client.get("/readyz").json()["version"] == distribution_version(
            "nexus-ai-agent"
        )

    def test_healthz_stays_byte_identical_liveness(self, ready_client: TestClient):
        response = ready_client.get("/healthz")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    def test_readyz_echoes_baked_git_sha_when_present(
        self, ready_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("NEXUS_DEPLOY_GIT_SHA", "abc123def456")
        assert ready_client.get("/readyz").json()["deploy_git_sha"] == "abc123def456"

    def test_readyz_git_sha_is_null_not_faked_when_absent(
        self, ready_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.delenv("NEXUS_DEPLOY_GIT_SHA", raising=False)
        assert ready_client.get("/readyz").json()["deploy_git_sha"] is None


class TestReadyzChaos:
    def test_readyz_503_with_reason_when_db_file_missing(
        self, ready_client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
    ):
        # DB file absent: readiness MUST fail loudly with a reason (liveness
        # stays green — the process is alive, it just cannot serve data).
        monkeypatch.setenv("NEXUS_DB_PATH", str(tmp_path / "gone" / "never.sqlite3"))
        settings_module.get_settings.cache_clear()

        response = ready_client.get("/readyz")

        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "not_ready"
        assert body["db"]["ok"] is False
        assert "missing" in (body["db"]["detail"] or "")
        # ...and the broken DB must NOT be masked by the liveness probe:
        assert ready_client.get("/healthz").json() == {"status": "ok"}

    def test_readyz_503_when_db_file_is_not_a_database(
        self, ready_client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
    ):
        garbage = tmp_path / "app.sqlite"
        garbage.write_bytes(b"\x00garbage-not-sqlite" * 8)
        monkeypatch.setenv("NEXUS_DB_PATH", str(garbage))
        settings_module.get_settings.cache_clear()

        response = ready_client.get("/readyz")

        assert response.status_code == 503
        body = response.json()
        assert body["db"]["ok"] is False
        assert body["db"]["detail"] or ""  # actionable reason, never bare

    def test_readyz_probe_creates_no_database_file(
        self, ready_client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
    ):
        # Readiness must never mutate: probing a missing DB must not create
        # it (an r/w sqlite3.open would). kill-mutation target.
        missing = tmp_path / "absent.sqlite3"
        monkeypatch.setenv("NEXUS_DB_PATH", str(missing))
        settings_module.get_settings.cache_clear()

        assert ready_client.get("/readyz").status_code == 503
        assert not missing.exists()

    def test_readiness_is_bounded_when_probe_hangs(
        self, ready_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ):
        # A hanging probe must not hang the event loop: the helper returns a
        # not-ready verdict after its timeout instead of blocking forever.
        def _hanging_connect(*_args, **_kwargs):
            time.sleep(2)
            raise AssertionError("probe should have been outlived by the timeout")

        import asyncio
        import sqlite3 as sqlite3_module

        monkeypatch.setattr(sqlite3_module, "connect", _hanging_connect)
        ok, detail = asyncio.run(app_module._database_readiness(timeout=0.2))
        assert ok is False
        assert "TimeoutError" in detail


def test_readyz_route_declared_in_app_source():
    """Offline contract: deploy_smoke validates the route string exists."""
    import inspect

    source = inspect.getsource(app_module)
    assert "/readyz" in source
    assert "/healthz" in source
