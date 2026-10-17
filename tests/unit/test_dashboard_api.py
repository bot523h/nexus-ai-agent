"""Tests for the dashboard API PII removal + bearer-token gate (P0-5)."""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlmodel import Session, SQLModel

from nexus_ai_agent.api.app import app
from nexus_ai_agent.api.dashboard import serialize_joined_at
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


# ── joined_at is an unambiguous UTC instant (review finding, 2026-10-17) ────


def _insert_user(*, telegram_id: int, created_at: datetime) -> None:
    engine = create_engine(f"sqlite:///{settings_module.get_settings().db_path}")
    try:
        with Session(engine) as session:
            session.add(
                User(telegram_id=telegram_id, username=f"u{telegram_id}", created_at=created_at)
            )
            session.commit()
    finally:
        engine.dispose()


def test_a_naive_join_time_is_published_as_utc_not_local_time(
    client_and_db: TestClient, with_token: None
) -> None:
    """``User.created_at`` is naive UTC; JS reads a naive ISO string as *local* time."""
    _insert_user(telegram_id=333, created_at=datetime(2026, 10, 17, 12, 34, 56))
    resp = client_and_db.get("/api/dashboard/recent_users?limit=3", headers=TOKEN)
    assert resp.status_code == 200
    joined = {entry["id"]: entry["joined_at"] for entry in resp.json()}
    assert "2026-10-17T12:34:56+00:00" in joined.values(), joined


def test_serialize_joined_at_reads_a_naive_value_as_utc() -> None:
    assert serialize_joined_at(datetime(2026, 10, 17, 12, 34, 56)) == "2026-10-17T12:34:56+00:00"


def test_serialize_joined_at_preserves_the_instant_of_an_offset_aware_value() -> None:
    tehran = timezone(timedelta(hours=3, minutes=30))
    new_york = timezone(timedelta(hours=-5))
    # 16:04:56 +03:30 == 12:34:56 UTC, and 08:04:56 -05:00 == 13:04:56 UTC:
    # the instant is preserved, the local wall-clock reading is not.
    assert serialize_joined_at(datetime(2026, 10, 17, 16, 4, 56, tzinfo=tehran)) == (
        "2026-10-17T12:34:56+00:00"
    )
    assert serialize_joined_at(datetime(2026, 10, 17, 8, 4, 56, tzinfo=new_york)) == (
        "2026-10-17T13:04:56+00:00"
    )


def test_serialize_joined_at_keeps_a_missing_timestamp_missing() -> None:
    assert serialize_joined_at(None) is None


def test_the_dashboard_client_reads_an_offset_less_timestamp_as_utc(
    client_and_db: TestClient,
) -> None:
    """The served script must not rely on `new Date()`'s local-time default.

    The API now always publishes an offset; this guard keeps the client
    correct for an older payload too, and pins the regex that does it.
    """
    html = client_and_db.get("/").text
    match = re.search(r"/\(\?:Z\|\[\+-]\\d\{2\}:\?\\d\{2\}\)\$/", html)
    assert match is not None, "the dashboard no longer normalises naive timestamps"
    served = re.compile(r"(?:Z|[+-]\d{2}:?\d{2})$")
    assert served.search("2026-10-17T12:34:56+00:00") is not None
    assert served.search("2026-10-17T12:34:56Z") is not None
    assert served.search("2026-10-17T12:34:56") is None, "a naive value must be normalised"


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
