"""A-06 deeper validation: every HTTP route is fail-closed, by inventory.

``test_dashboard_api.py`` proves the two dashboard routes close without a
token.  This test refuses to trust a hand-written list: it takes the route
inventory from the application's own OpenAPI document (which includes the
routers FastAPI ≥ 0.115 wraps lazily) and probes *each* operation three
ways — unconfigured, configured without credentials, configured with wrong
credentials — through the real ASGI app.  Anything outside the explicit public
allowlist must answer 401/403/503 and never leak a 200.  A new route added
without a gate turns this test red on the same day.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from nexus_ai_agent.config import settings as settings_module
from nexus_ai_agent.storage import db as db_module

#: Operations that are public by design (no user data; health + landing page).
PUBLIC = {("GET", "/"), ("GET", "/healthz")}

#: The Telegram webhook has its own shared-secret check (403 without it).
WEBHOOK = ("POST", "/webhook/telegram")

PARAMS = {"{job_id}": "job-1", "{project_id}": "proj1", "{asset_id}": "a1", "{filename}": "f.txt"}


def _fill(path: str) -> str:
    for key, value in PARAMS.items():
        path = path.replace(key, value)
    assert "{" not in path, f"unfilled path parameter in {path}"
    return path


@pytest.fixture()
def api(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:gate-inventory")
    monkeypatch.setenv("NEXUS_DB_PATH", str(tmp_path / "app.sqlite"))
    for var in ("NEXUS_DASHBOARD_TOKEN", "NEXUS_API_HMAC_KEY", "NEXUS_DATABASE_URL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)
    settings_module.get_settings.cache_clear()
    db_module._engine = None  # type: ignore[attr-defined]
    db_module._session_factory = None  # type: ignore[attr-defined]
    from nexus_ai_agent.api.app import app

    try:
        yield app
    finally:
        settings_module.get_settings.cache_clear()
        db_module._engine = None  # type: ignore[attr-defined]
        db_module._session_factory = None  # type: ignore[attr-defined]


def _operations(app: Any) -> list[tuple[str, str]]:
    ops = [
        (method.upper(), path)
        for path, methods in app.openapi()["paths"].items()
        for method in methods
    ]
    assert len(ops) >= 8, ops
    return ops


def _probe(app: Any, headers: dict[str, str] | None = None) -> dict[tuple[str, str], int]:
    client = TestClient(app)
    return {
        (method, path): client.request(method, _fill(path), headers=headers).status_code
        for method, path in _operations(app)
    }


def test_inventory_contains_the_audited_surfaces(api: Any) -> None:
    ops = set(_operations(api))
    assert PUBLIC <= ops and WEBHOOK in ops
    assert ("GET", "/api/dashboard/stats") in ops
    assert ("GET", "/api/dashboard/recent_users") in ops
    assert any(path.startswith("/api/studio/") for _, path in ops)
    assert any(path.startswith("/creative/") for _, path in ops)


def test_unconfigured_deployment_closes_every_gated_route(api: Any) -> None:
    statuses = _probe(api)
    leaks = {
        op: code
        for op, code in statuses.items()
        if op not in PUBLIC and op != WEBHOOK and code != 503
    }
    assert leaks == {}, f"routes open without security configuration: {leaks}"
    assert all(statuses[op] == 200 for op in PUBLIC)
    assert statuses[WEBHOOK] == 403


def test_configured_deployment_rejects_missing_and_wrong_credentials(
    api: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NEXUS_DASHBOARD_TOKEN", "correct-horse-battery-staple")
    monkeypatch.setenv("NEXUS_API_HMAC_KEY", "k" * 32)
    settings_module.get_settings.cache_clear()

    for headers in (
        None,
        {"Authorization": "Bearer wrong-token"},
        {"Authorization": "Basic d3Jvbmc6d3Jvbmc="},
        {"X-NEXUS-Timestamp": "0", "X-NEXUS-Signature": "00" * 32},
    ):
        statuses = _probe(api, headers)
        leaks = {
            op: code
            for op, code in statuses.items()
            if op not in PUBLIC and op != WEBHOOK and code not in (401, 403)
        }
        assert leaks == {}, f"headers={headers}: {leaks}"
        assert statuses[WEBHOOK] == 403


def test_correct_dashboard_token_opens_only_the_dashboard(
    api: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NEXUS_DASHBOARD_TOKEN", "correct-horse-battery-staple")
    settings_module.get_settings.cache_clear()
    statuses = _probe(api, {"Authorization": "Bearer correct-horse-battery-staple"})
    assert statuses[("GET", "/api/dashboard/stats")] == 200
    assert statuses[("GET", "/api/dashboard/recent_users")] == 200
    # The HMAC-signed and studio surfaces still need their own configuration.
    still_closed = {
        op: code
        for op, code in statuses.items()
        if op not in PUBLIC and op != WEBHOOK and not op[1].startswith("/api/dashboard/")
    }
    assert still_closed and all(code in (401, 403, 404, 503) for code in still_closed.values()), (
        still_closed
    )
