"""Tests for the dashboard API PII removal + bearer-token gate (P0-5)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlmodel import Session, SQLModel

from nexus_ai_agent.api.app import app
from nexus_ai_agent.config import settings as settings_module
from nexus_ai_agent.storage import db as db_module
from nexus_ai_agent.storage.models import User


def _reset_db_engine_cache() -> None:
    """The sqlite engine is cached by (relative) path string; reset per test.

    The autouse ``_dispose_db_engine`` fixture (tests/conftest.py) has already
    disposed the previous test's engine on its own loop; this only clears a
    stale cached path so the next request opens the fixture's database.
    """
    db_module._engine = None
    db_module._engine_path = None
    db_module._session_factory = None


@pytest.fixture()
def client_and_db(tmp_path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Test client against a fresh sqlite DB; settings isolated per test.

    The dashboard API resolves its SQLite file as ``data/app.sqlite``
    relative to the process CWD (see ``storage.db.get_session``), so the
    fixture chdirs into a temp dir.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_file = data_dir / "app.sqlite"
    engine = create_engine(f"sqlite:///{db_file}")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(User(telegram_id=111, username="alice"))
        session.add(User(telegram_id=222, username="bob"))
        session.commit()
    engine.dispose()

    _reset_db_engine_cache()
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("NEXUS_DATABASE_URL", raising=False)
    settings_module.get_settings.cache_clear()
    yield TestClient(app)
    settings_module.get_settings.cache_clear()


TOKEN = {"Authorization": "Bearer s3cret-token"}


@pytest.fixture()
def with_token(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("NEXUS_DASHBOARD_TOKEN", "s3cret-token")
    settings_module.get_settings.cache_clear()
    yield
    monkeypatch.delenv("NEXUS_DASHBOARD_TOKEN", raising=False)
    settings_module.get_settings.cache_clear()


def test_recent_users_is_pii_free(client_and_db: TestClient, with_token: None) -> None:
    resp = client_and_db.get("/api/dashboard/recent_users", headers=TOKEN)
    assert resp.status_code == 200
    users = resp.json()
    assert len(users) == 2
    for entry in users:
        assert "telegram_id" not in entry
        assert "username" not in entry
        assert set(entry) == {"id", "joined_at"}


def test_stats_count_real_rows(client_and_db: TestClient, with_token: None) -> None:
    resp = client_and_db.get("/api/dashboard/stats", headers=TOKEN)
    assert resp.status_code == 200
    assert resp.json() == {
        "total_users": 2,
        "total_chats": 0,
        "total_files": 0,
        "active_specialized_agents": 0,
    }


def test_dashboard_is_closed_when_no_token_is_configured(client_and_db: TestClient) -> None:
    """Audit A-06 (2026-10-15): an unset token used to mean *open*; it now
    means *disabled*, with a response that says which variable to set."""
    for path in ("/api/dashboard/stats", "/api/dashboard/recent_users"):
        resp = client_and_db.get(path)
        assert resp.status_code == 503, path
        assert "NEXUS_DASHBOARD_TOKEN" in resp.json()["detail"]
        # Guessing a header does not open it either.
        assert client_and_db.get(path, headers=TOKEN).status_code == 503


@pytest.mark.parametrize("limit", [0, -1, 101, "abc"])
def test_recent_users_limit_is_bounded(client_and_db: TestClient, with_token: None, limit) -> None:
    resp = client_and_db.get(f"/api/dashboard/recent_users?limit={limit}", headers=TOKEN)
    assert resp.status_code == 422


def test_recent_users_limit_within_range_is_honoured(
    client_and_db: TestClient, with_token: None
) -> None:
    resp = client_and_db.get("/api/dashboard/recent_users?limit=1", headers=TOKEN)
    assert resp.status_code == 200
    assert len(resp.json()) == 1


def test_token_required_when_configured(client_and_db: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("NEXUS_DASHBOARD_TOKEN", "s3cret-token")
    settings_module.get_settings.cache_clear()
    try:
        # Missing header → 401
        assert client_and_db.get("/api/dashboard/recent_users").status_code == 401
        # Wrong token → 401
        bad = client_and_db.get(
            "/api/dashboard/recent_users", headers={"Authorization": "Bearer nope"}
        )
        assert bad.status_code == 401
        # Correct bearer token → 200
        ok = client_and_db.get(
            "/api/dashboard/recent_users", headers={"Authorization": "Bearer s3cret-token"}
        )
        assert ok.status_code == 200
        # Stats is gated too
        assert client_and_db.get("/api/dashboard/stats").status_code == 401
        ok_stats = client_and_db.get(
            "/api/dashboard/stats", headers={"Authorization": "Bearer s3cret-token"}
        )
        assert ok_stats.status_code == 200
    finally:
        monkeypatch.delenv("NEXUS_DASHBOARD_TOKEN", raising=False)
        settings_module.get_settings.cache_clear()
