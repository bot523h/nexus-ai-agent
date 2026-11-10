"""NAGAR Studio browser session (web/README.md): fail-closed, tamper-evident, same authority."""

from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from nexus_ai_agent.api import studio_session as ss

TOKEN = "dashboard-secret-0123456789"
JOB = "/api/studio/projects/project-a/jobs/job-1/state"
MARK = {ss.CSRF_HEADER: ss.CSRF_VALUE}


class _Reader:
    async def get_job(self, job_id: str) -> dict[str, Any] | None:
        return {
            "id": job_id,
            "status": "verifying",
            "input_data": {"project_id": "project-a", "intent": {"intent_id": "i-a"}},
            "result": {},
        }


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("NEXUS_DASHBOARD_TOKEN", TOKEN)
    monkeypatch.setenv("NEXUS_STUDIO_PROJECT_IDS", "project-a")
    from nexus_ai_agent.config import settings as settings_module

    settings_module.get_settings.cache_clear()
    from nexus_ai_agent.api import app as app_module

    monkeypatch.setattr(app_module, "get_creative_registry", lambda: _Reader())
    ss.reset_login_throttle()
    # https base URL: the __Host- cookie is Secure and must round-trip like in production.
    yield TestClient(app_module.app, base_url="https://testserver")
    ss.reset_login_throttle()
    settings_module.get_settings.cache_clear()


def _login(client: TestClient, token: str = TOKEN) -> Any:
    return client.post("/studio/session", json={"token": token}, headers=MARK)


def test_login_sets_a_hardened_cookie_and_it_authorizes_the_read_route(client: TestClient) -> None:
    assert client.get(JOB).status_code == 401
    response = _login(client)
    assert response.status_code == 200
    cookie = response.headers["set-cookie"].lower()
    assert ss.SESSION_COOKIE.lower() in cookie
    assert "httponly" in cookie and "secure" in cookie and "samesite=strict" in cookie
    assert "path=/" in cookie and "domain=" not in cookie
    assert TOKEN not in response.text and TOKEN not in response.headers["set-cookie"]
    assert client.get(JOB).status_code == 200
    state = client.get("/studio/session").json()
    assert state["authenticated"] is True and state["expires_at"] > time.time()


def test_wrong_token_is_rejected_and_sets_no_cookie(client: TestClient) -> None:
    response = _login(client, "nope")
    assert response.status_code == 401
    assert "set-cookie" not in response.headers
    assert client.get(JOB).status_code == 401


def test_login_without_the_request_marker_is_refused(client: TestClient) -> None:
    response = client.post("/studio/session", json={"token": TOKEN})
    assert response.status_code == 403
    assert "set-cookie" not in response.headers
    assert client.delete("/studio/session").status_code == 403


def test_fails_closed_without_a_configured_dashboard_token(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("NEXUS_DASHBOARD_TOKEN")
    from nexus_ai_agent.config import settings as settings_module

    settings_module.get_settings.cache_clear()
    assert _login(client).status_code == 503
    assert client.get(JOB).status_code == 503
    assert client.get("/api/studio/overview").status_code == 503
    assert client.get("/studio/session").json()["configured"] is False


def test_tampered_forged_and_malformed_cookies_are_treated_as_absent(client: TestClient) -> None:
    value, exp = ss.issue_session_value(TOKEN)
    body, mac = value.rsplit(".", 1)
    forged = [
        body + "." + ("0" * 64),
        body.replace(str(exp), str(exp + 3600)) + "." + mac,  # extended lifetime, old MAC
        "v2." + value.split(".", 1)[1],
        "garbage",
        "v1.x.y.z",
        "",
        "A" * 400,
    ]
    for cookie in forged:
        client.cookies.clear()
        client.cookies.set(ss.SESSION_COOKIE, cookie, domain="testserver.local")
        assert ss.verify_session_value(cookie, TOKEN) is None
        assert client.get(JOB).status_code == 401


def test_expired_and_far_future_sessions_are_rejected() -> None:
    now = 1_000_000.0
    value, exp = ss.issue_session_value(TOKEN, now=now)
    assert ss.verify_session_value(value, TOKEN, now=now + 10) == exp
    assert ss.verify_session_value(value, TOKEN, now=exp) is None
    assert ss.verify_session_value(value, TOKEN, now=exp + 1) is None
    far, _ = ss.issue_session_value(TOKEN, now=now + 10 * ss.SESSION_TTL_SECONDS)
    assert ss.verify_session_value(far, TOKEN, now=now) is None


def test_rotating_the_dashboard_token_revokes_existing_sessions() -> None:
    value, _ = ss.issue_session_value(TOKEN)
    assert ss.verify_session_value(value, TOKEN) is not None
    assert ss.verify_session_value(value, TOKEN + "-rotated") is None


def test_session_grants_exactly_the_bearer_scope_not_more(client: TestClient) -> None:
    _login(client)
    assert client.get(JOB).status_code == 200
    denied = client.get("/api/studio/projects/project-b/jobs/job-1/state")
    assert denied.status_code == 404
    assert "project-a" not in denied.text
    assert client.get("/api/studio/projects/bad id/jobs/job-1/state").status_code in (400, 404)


def test_an_authorization_header_is_authoritative_over_the_cookie(client: TestClient) -> None:
    _login(client)
    assert client.get(JOB, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get(JOB, headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200


def test_bearer_access_is_unchanged_without_any_cookie(client: TestClient) -> None:
    assert client.get(JOB, headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200
    assert client.get(JOB, headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_logout_clears_the_cookie(client: TestClient) -> None:
    _login(client)
    response = client.delete("/studio/session", headers=MARK)
    assert response.status_code == 200
    assert "max-age=0" in response.headers["set-cookie"].lower()
    assert client.get(JOB).status_code == 401


def test_failed_login_throttle_blocks_guessing_but_not_existing_sessions(
    client: TestClient,
) -> None:
    _login(client)  # an established session
    other = TestClient(client.app, base_url="https://testserver")
    for _ in range(ss.FAILED_LOGIN_LIMIT):
        assert _login(other, "guess").status_code == 401
    blocked = _login(other, TOKEN)  # even the right token waits out the window
    assert blocked.status_code == 429 and int(blocked.headers["retry-after"]) >= 1
    assert client.get(JOB).status_code == 200  # existing session unaffected
    assert client.get(JOB, headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200


def test_overview_requires_auth_and_is_scoped_to_deployment_policy(client: TestClient) -> None:
    assert client.get("/api/studio/overview").status_code == 401
    _login(client)
    body = client.get("/api/studio/overview").json()
    assert body["projects"] == ["project-a"]
    by_id = {c["id"]: c for c in body["capabilities"]}
    assert by_id["user_identity"]["status"] == "BLOCKED"
    assert by_id["intent_submit"]["status"] == "DEFERRED"
    assert {c["status"] for c in body["capabilities"]} <= {
        "VERIFIED",
        "VERIFIED_WITH_LIMITATIONS",
        "HARDENED_BUT_NOT_COMPLETE",
        "BLOCKED",
        "DEFERRED",
    }


def test_studio_api_responses_are_never_cacheable(client: TestClient) -> None:
    _login(client)
    for path in (JOB, "/api/studio/overview", "/studio/session"):
        response = client.get(path)
        assert response.headers["cache-control"] == "no-store", path
        assert response.headers["x-content-type-options"] == "nosniff"
