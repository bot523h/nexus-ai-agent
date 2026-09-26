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


# ── query-parameter bounds ──────────────────────────────────────────────────


def test_recent_users_limit_is_bounded(client_and_db: TestClient) -> None:
    """`limit` was an unvalidated int straight off the query string.

    A single authenticated `?limit=100000000` materialised the whole user
    table into memory and serialised it — a denial of service behind an
    endpoint whose only other protection is a shared bearer token.
    """
    from nexus_ai_agent.api.dashboard import MAX_RECENT_USERS

    assert client_and_db.get("/api/dashboard/recent_users?limit=100000000").status_code == 422
    assert (
        client_and_db.get(f"/api/dashboard/recent_users?limit={MAX_RECENT_USERS}").status_code
        == 200
    )


def test_recent_users_rejects_a_negative_limit(client_and_db: TestClient) -> None:
    """SQLite reads `LIMIT -1` as "no limit" — the opposite of the intent."""
    assert client_and_db.get("/api/dashboard/recent_users?limit=-1").status_code == 422
    assert client_and_db.get("/api/dashboard/recent_users?limit=0").status_code == 422


def test_recent_users_honours_a_valid_limit(client_and_db: TestClient) -> None:
    resp = client_and_db.get("/api/dashboard/recent_users?limit=1")
    assert resp.status_code == 200
    assert len(resp.json()) == 1


# ── CORS policy ─────────────────────────────────────────────────────────────


def test_a_wildcard_origin_never_carries_credentials() -> None:
    """`allow_origins=["*"]` + `allow_credentials=True` is the CORS anti-pattern.

    Starlette responds to that combination by reflecting the caller's Origin
    and setting `Access-Control-Allow-Credentials: true`, which hands every
    site on the internet an authenticated channel to this API. The empty
    default avoided it; an operator typing `NEXUS_API_CORS_ORIGINS=*` while
    debugging reintroduced it, because `bool(["*"])` is `True`.
    """
    from nexus_ai_agent.api.app import cors_policy

    assert cors_policy(["*"]) == (["*"], False)
    assert cors_policy(["https://a.example", "*"]) == (["*"], False)


def test_an_explicit_allowlist_keeps_credentials() -> None:
    from nexus_ai_agent.api.app import cors_policy

    assert cors_policy(["https://a.example"]) == (["https://a.example"], True)
    assert cors_policy(["https://a.example", "https://b.example"]) == (
        ["https://a.example", "https://b.example"],
        True,
    )


def test_no_allowlist_means_no_cross_origin_access() -> None:
    from nexus_ai_agent.api.app import cors_policy

    assert cors_policy([]) == ([], False)
