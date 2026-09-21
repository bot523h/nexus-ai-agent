"""Tests for the dashboard API PII removal + bearer-token gate (P0-5)."""

from __future__ import annotations

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

    Retired engines are disposed lazily by the db module on the next
    async operation, so dropping the reference here is sufficient.
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


def test_recent_users_is_pii_free(client_and_db: TestClient) -> None:
    resp = client_and_db.get("/api/dashboard/recent_users")
    assert resp.status_code == 200
    users = resp.json()
    assert len(users) == 2
    for entry in users:
        assert "telegram_id" not in entry
        assert "username" not in entry
        assert set(entry) == {"id", "joined_at"}


def test_stats_works_without_token(client_and_db: TestClient) -> None:
    resp = client_and_db.get("/api/dashboard/stats")
    assert resp.status_code == 200
    assert resp.json()["total_users"] == 2


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
