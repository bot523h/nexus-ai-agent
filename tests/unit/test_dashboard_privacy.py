"""Privacy tests for the public dashboard API (P0-5).

``GET /api/dashboard/recent_users`` used to answer an unauthenticated request
on a published port with real ``telegram_id`` and ``username`` values — a
direct handle for contacting or tracking a user.  These tests pin the new
contract: no identifiers, masked labels, and an optional bearer-token lock.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlmodel import Session, SQLModel

from nexus_ai_agent.config import settings as settings_module
from nexus_ai_agent.storage.models import Chat, CloudFile, User, UserActiveAgent

TOKEN = "s3cr3t-dashboard-token"


@pytest.fixture()
def _db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    db_path = tmp_path / "app.sqlite"
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("NEXUS_DB_PATH", str(db_path))
    monkeypatch.delenv("NEXUS_API_DASHBOARD_TOKEN", raising=False)
    settings_module.get_settings.cache_clear()

    # Sync engine on purpose: the dashboard reads through the async engine
    # inside TestClient's loop, and sharing one engine across two loops is a
    # recipe for "attached to a different loop".
    engine = create_engine(f"sqlite:///{db_path}")
    SQLModel.metadata.create_all(
        engine,
        tables=[User.__table__, Chat.__table__, CloudFile.__table__, UserActiveAgent.__table__],
    )
    with Session(engine) as session:
        session.add(User(telegram_id=424242, username="alice", is_allowed=True))
        session.add(User(telegram_id=424243, username="bob", is_allowed=True))
        session.commit()
    engine.dispose()
    yield db_path
    settings_module.get_settings.cache_clear()


@pytest.fixture()
def client(_db: Path) -> TestClient:
    from nexus_ai_agent.api.app import app

    return TestClient(app)


def test_recent_users_never_exposes_telegram_id_or_username(client: TestClient) -> None:
    response = client.get("/api/dashboard/recent_users")
    assert response.status_code == 200
    rows = response.json()
    assert rows, "fixture inserts two users"
    for row in rows:
        assert set(row) == {"id", "display"}
        assert "telegram_id" not in row
        assert "username" not in row
    # The username is masked, not echoed.
    assert all("alice" not in row["display"] and "bob" not in row["display"] for row in rows)
    assert not any("424242" in str(row) or "424243" in str(row) for row in rows)


def test_recent_users_limit_is_clamped(client: TestClient) -> None:
    # A negative LIMIT means "unbounded" in SQLite; it must not become a dump.
    assert len(client.get("/api/dashboard/recent_users?limit=-1").json()) >= 1
    assert len(client.get("/api/dashboard/recent_users?limit=1").json()) == 1
    assert len(client.get("/api/dashboard/recent_users?limit=99999").json()) <= 50


def test_stats_still_reports_aggregates_only(client: TestClient) -> None:
    response = client.get("/api/dashboard/stats")
    assert response.status_code == 200
    body = response.json()
    assert body["total_users"] == 2
    assert set(body) == {
        "total_users",
        "total_chats",
        "total_files",
        "active_specialized_agents",
    }


def test_dashboard_token_locks_the_router_when_configured(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NEXUS_API_DASHBOARD_TOKEN", TOKEN)
    settings_module.get_settings.cache_clear()

    assert client.get("/api/dashboard/stats").status_code == 401
    assert client.get("/api/dashboard/recent_users").status_code == 401
    assert (
        client.get(
            "/api/dashboard/stats", headers={"Authorization": "Bearer not-the-token"}
        ).status_code
        == 401
    )
    authorized = client.get("/api/dashboard/stats", headers={"Authorization": f"Bearer {TOKEN}"})
    assert authorized.status_code == 200
    assert authorized.json()["total_users"] == 2


def test_health_probe_is_never_gated(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXUS_API_DASHBOARD_TOKEN", TOKEN)
    settings_module.get_settings.cache_clear()
    assert client.get("/healthz").status_code == 200


def test_served_page_never_renders_a_telegram_id(client: TestClient) -> None:
    html = client.get("/").text
    assert "telegram_id" not in html
    assert "u.display" in html
