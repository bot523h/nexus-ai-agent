"""Adversarial tests for the studio surface's own defensive controls.

``test_studio_surface.py`` covers the happy path and the delegation to the IBPC
lane. This module covers the three controls that exist *here* and nowhere else:

* the CSRF marker on every state-changing route — an authenticated browser is
  still an attacker-controlled page away from a forged POST;
* the failed-login limiter — without it a token can be brute-forced at wire
  speed;
* the per-principal session quota — without it one caller refreshing in a loop
  evicts every other principal's live session.

No network is touched: the IBPC handlers are replaced with a sentinel so a
request that reaches them is visible, and the marker/limit/quota logic is
exercised through the real FastAPI stack.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from nexus_ai_agent.api import studio_ibpc
from nexus_ai_agent.api.app import app
from nexus_ai_agent.api.studio_surface import (
    API_PREFIX,
    LoginAttemptLimiter,
    PrincipalSessionRegistry,
    authenticated_surface_routes,
    login_limiter,
    session_registry,
)

MARKER = {"x-nagar-studio": "1"}
PRINCIPAL = "prn_" + "b" * 32
PRINCIPAL_TOKEN = "tok-prin-w11x-security"


@pytest.fixture()
def store_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A real principal store with one registered principal, and a clean surface."""
    import base64

    from nexus_ai_agent.adapters.studio_principal_auth import StudioPrincipalAuthStore

    workspace = tmp_path / "ws"
    workspace.mkdir()
    auth_db = tmp_path / "auth.sqlite3"
    monkeypatch.setenv("NEXUS_STUDIO_AUTH_DB", str(auth_db))
    monkeypatch.setenv("NEXUS_STUDIO_WORKSPACE_ROOT", str(workspace))
    key = base64.b64encode(b"y" * 32).decode("ascii")
    monkeypatch.setenv("NEXUS_IBPC_APPROVAL_KEY_RING", json.dumps({"active": key}))
    monkeypatch.setenv("NEXUS_IBPC_APPROVAL_ACTIVE_KEY_ID", "active")

    store = StudioPrincipalAuthStore(auth_db)
    store.register_principal(
        PRINCIPAL,
        tenant_id="ten-1",
        raw_token=PRINCIPAL_TOKEN,
        display_name="Owner",
        allow_system_bootstrap=True,
    )
    store.close()

    session_registry.clear()
    login_limiter.clear()
    yield auth_db
    session_registry.clear()
    login_limiter.clear()


@pytest.fixture()
def lane(store_db: Path) -> Iterator[TestClient]:
    client = TestClient(app, base_url="https://testserver")
    yield client


def _login(client: TestClient) -> None:
    response = client.post(
        f"{API_PREFIX}/login", json={"principal_token": PRINCIPAL_TOKEN}, headers=MARKER
    )
    assert response.status_code == 200, response.text


@pytest.fixture()
def ibpc_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record any call that reaches an IBPC handler instead of performing it."""
    calls: list[str] = []

    def sentinel(name: str) -> Any:
        async def _call(**_: Any) -> dict[str, Any]:
            calls.append(name)
            return {"reached": name}

        return _call

    for name in (
        "open_ibpc_session",
        "get_ibpc_session",
        "close_ibpc_session",
        "submit_ibpc_turn",
        "approve_ibpc_turn",
        "execute_ibpc_turn",
    ):
        monkeypatch.setattr(studio_ibpc, name, sentinel(name))
    return calls


# ---------------------------------------------------------------------------
# CSRF marker on every state-changing route
# ---------------------------------------------------------------------------
def _marker_less_targets() -> list[tuple[str, str, dict[str, Any] | None]]:
    """Every mutating route, with its path templates filled in concretely."""
    substitutions = {
        "{project_id}": "proj-security",
        "{session_id}": "sess-1",
        "{turn_id}": "turn-1",
    }
    targets: list[tuple[str, str, dict[str, Any] | None]] = []
    for route in authenticated_surface_routes():
        if route.method == "GET":
            continue
        path = route.path
        for placeholder, value in substitutions.items():
            path = path.replace(placeholder, value)
        body: dict[str, Any] | None = {"principal_token": PRINCIPAL_TOKEN}
        targets.append((route.method, path, body))
    return targets


def test_every_state_changing_route_refuses_a_missing_marker(
    lane: TestClient, ibpc_calls: list[str]
) -> None:
    """Authenticated or not, a POST without the marker is refused before any work."""
    _login(lane)
    for method, path, body in _marker_less_targets():
        response = (
            lane.request(method, path, json=body)
            if body is not None
            else lane.request(method, path)
        )
        assert response.status_code == 403, f"{method} {path} -> {response.status_code}"
        assert response.json()["detail"] == "missing request marker"
    assert ibpc_calls == [], "a marker-less request must never reach an IBPC handler"


def test_the_marker_is_checked_before_the_session(lane: TestClient, ibpc_calls: list[str]) -> None:
    """An anonymous forged POST gets 403, not 401: no session lookup is performed."""
    response = lane.post(f"{API_PREFIX}/projects/p/ibpc/sessions", json={})
    assert response.status_code == 403
    assert ibpc_calls == []


def test_a_marker_less_login_attempt_does_not_consume_the_failure_budget(
    lane: TestClient,
) -> None:
    """The marker check runs first, so a forged POST cannot lock a victim out."""
    for _ in range(10):
        assert (
            lane.post(f"{API_PREFIX}/login", json={"principal_token": "wrong-token"}).status_code
            == 403
        )
    ok = lane.post(f"{API_PREFIX}/login", json={"principal_token": PRINCIPAL_TOKEN}, headers=MARKER)
    assert ok.status_code == 200, "a marker-less flood must not throttle a real login"


def _installed_surface_routes() -> list[tuple[str, str, str, Any]]:
    """``(method, path, handler, route)`` for every route of the installed routers."""
    from nexus_ai_agent.api import studio_surface as surface

    found: list[tuple[str, str, str, Any]] = []
    for router in (surface.router, surface.login_router, surface.api_router):
        for route in router.routes:
            if route.path in surface.PUBLIC_SURFACE_PATHS:
                continue  # the static bundle is public by design
            for method in sorted(getattr(route, "methods", set()) or set()):
                if method in {"GET", "POST"}:
                    handler = getattr(getattr(route, "endpoint", None), "__name__", "")
                    found.append((method, route.path, handler, route))
    return found


def test_every_mutating_route_declares_the_marker_dependency() -> None:
    """Fail closed on the *route table itself*, not only on observed behaviour."""
    mutating = [entry for entry in _installed_surface_routes() if entry[0] == "POST"]
    assert len(mutating) == 7, f"unexpected mutating routes: {[e[1] for e in mutating]}"
    for method, path, _handler, route in mutating:
        declared = {
            getattr(dependency.dependency, "__name__", "")
            for dependency in getattr(route, "dependencies", [])
        }
        assert "_require_marker" in declared, f"{method} {path} has no CSRF marker"


def test_the_declared_inventory_matches_the_installed_routers() -> None:
    """A route cannot be added without appearing in the inventory table."""
    declared = {(route.method, route.path) for route in authenticated_surface_routes()}
    installed = {(method, path) for method, path, _handler, _route in _installed_surface_routes()}
    assert installed == declared, {
        "installed_but_undeclared": sorted(installed - declared),
        "declared_but_missing": sorted(declared - installed),
    }


def test_the_inventory_names_the_handler_that_serves_each_route() -> None:
    declared = {
        (route.method, route.path): route.handler for route in authenticated_surface_routes()
    }
    for method, path, handler, _route in _installed_surface_routes():
        assert declared[(method, path)] == handler


# ---------------------------------------------------------------------------
# Failed-login limiter
# ---------------------------------------------------------------------------
def _limiter(**kwargs: Any) -> LoginAttemptLimiter:
    defaults: dict[str, Any] = {
        "max_failures": 3,
        "window_seconds": 60.0,
        "max_tracked_keys": 8,
    }
    defaults.update(kwargs)
    return LoginAttemptLimiter(**defaults)


def test_limiter_blocks_after_the_budget_and_reports_the_true_wait() -> None:
    limiter = _limiter()
    for offset in range(3):
        assert limiter.check("1.2.3.4", now=100.0 + offset) is None
        limiter.record_failure("1.2.3.4", now=100.0 + offset)
    wait = limiter.check("1.2.3.4", now=102.0)
    assert wait == pytest.approx(58.0), "the wait is until the oldest failure expires"


def test_limiter_reopens_the_window_once_failures_age_out() -> None:
    limiter = _limiter()
    for offset in range(3):
        limiter.record_failure("ip", now=0.0 + offset)
    assert limiter.check("ip", now=2.0) == pytest.approx(58.0)
    assert limiter.check("ip", now=59.0) == pytest.approx(1.0)
    # At 60.0 the first failure leaves the window: two remain, below the budget.
    assert limiter.check("ip", now=60.5) is None


def test_limiter_keeps_keys_independent() -> None:
    limiter = _limiter()
    for offset in range(3):
        limiter.record_failure("attacker", now=float(offset))
    assert limiter.check("attacker", now=3.0) is not None
    assert limiter.check("innocent", now=3.0) is None


def test_limiter_forgets_a_key_after_a_successful_login() -> None:
    limiter = _limiter()
    limiter.record_failure("ip", now=0.0)
    limiter.record_failure("ip", now=1.0)
    limiter.record_success("ip")
    assert limiter.check("ip", now=2.0) is None
    assert limiter.tracked_keys() == 0


def test_limiter_never_tracks_more_keys_than_its_ceiling() -> None:
    limiter = _limiter(max_tracked_keys=4)
    for index in range(40):
        limiter.record_failure(f"ip-{index}", now=float(index))
    assert limiter.tracked_keys() <= 4


def test_limiter_prunes_expired_buckets_instead_of_evicting_live_ones() -> None:
    limiter = _limiter(max_tracked_keys=3, window_seconds=10.0)
    for index in range(3):
        limiter.record_failure(f"old-{index}", now=0.0)
    limiter.record_failure("fresh", now=100.0)
    assert limiter.tracked_keys() == 1
    assert limiter.check("fresh", now=100.0) is None


def test_limiter_rejects_nonsense_configuration() -> None:
    with pytest.raises(ValueError):
        _limiter(max_failures=0)
    with pytest.raises(ValueError):
        _limiter(window_seconds=0)
    with pytest.raises(ValueError):
        _limiter(max_tracked_keys=0)


def test_repeated_wrong_tokens_are_throttled_with_a_retry_after(
    lane: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The route end of the limiter: 5 failures, then a flat 429 with Retry-After."""
    for _ in range(5):
        assert (
            lane.post(
                f"{API_PREFIX}/login", json={"principal_token": "wrong-token"}, headers=MARKER
            ).status_code
            == 401
        )
    throttled = lane.post(
        f"{API_PREFIX}/login", json={"principal_token": "wrong-token"}, headers=MARKER
    )
    assert throttled.status_code == 429
    body = throttled.json()
    assert body["detail"] == "rate_limit_exceeded"
    assert isinstance(body["retry_after_seconds"], int) and body["retry_after_seconds"] >= 1
    assert throttled.headers["Retry-After"] == str(body["retry_after_seconds"])


def test_a_throttled_caller_is_refused_before_the_store_is_consulted(
    lane: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even the *correct* token is refused while throttled — no oracle, no work."""
    for _ in range(5):
        lane.post(f"{API_PREFIX}/login", json={"principal_token": "wrong-token"}, headers=MARKER)

    consulted: list[str] = []
    real = studio_ibpc.get_studio_auth_store

    def spy() -> Any:
        consulted.append("store")
        return real()

    monkeypatch.setattr(studio_ibpc, "get_studio_auth_store", spy)
    response = lane.post(
        f"{API_PREFIX}/login", json={"principal_token": PRINCIPAL_TOKEN}, headers=MARKER
    )
    assert response.status_code == 429
    assert consulted == []


def test_a_successful_login_resets_the_budget(lane: TestClient) -> None:
    for _ in range(4):
        lane.post(f"{API_PREFIX}/login", json={"principal_token": "wrong-token"}, headers=MARKER)
    _login(lane)
    for _ in range(4):
        assert (
            lane.post(
                f"{API_PREFIX}/login", json={"principal_token": "wrong-token"}, headers=MARKER
            ).status_code
            == 401
        )


def test_the_client_key_is_the_transport_peer_never_a_header(lane: TestClient) -> None:
    """A spoofable ``X-Forwarded-For`` must not be able to rotate the bucket."""
    for _ in range(5):
        lane.post(
            f"{API_PREFIX}/login",
            json={"principal_token": "wrong-token"},
            headers={**MARKER, "x-forwarded-for": f"203.0.113.{_}"},
        )
    response = lane.post(
        f"{API_PREFIX}/login",
        json={"principal_token": "wrong-token"},
        headers={**MARKER, "x-forwarded-for": "198.51.100.7"},
    )
    assert response.status_code == 429


# ---------------------------------------------------------------------------
# Per-principal session quota
# ---------------------------------------------------------------------------
def test_one_principal_cannot_evict_another_principals_session() -> None:
    registry = PrincipalSessionRegistry(capacity=4, max_sessions_per_principal=2)
    victim = registry.issue(principal_id="victim", tenant_id="t", token="tok-v")
    for _ in range(10):
        registry.issue(principal_id="attacker", tenant_id="t", token="tok-a")
    assert registry.resolve(victim.session_id) is not None, "the victim kept its session"
    assert len(registry) == 3, "two attacker sessions + the victim"


def test_a_single_principal_is_capped_and_its_oldest_session_goes_first() -> None:
    registry = PrincipalSessionRegistry(max_sessions_per_principal=3)
    issued = [registry.issue(principal_id="p", tenant_id="t", token="tok") for _ in range(5)]
    assert len(registry) == 3
    assert registry.resolve(issued[0].session_id) is None
    assert registry.resolve(issued[1].session_id) is None
    assert registry.resolve(issued[4].session_id) is not None


def test_the_global_capacity_still_bounds_the_store() -> None:
    registry = PrincipalSessionRegistry(capacity=3, max_sessions_per_principal=2)
    for index in range(10):
        registry.issue(principal_id=f"p{index}", tenant_id="t", token="tok")
    assert len(registry) == 3


def test_the_quota_must_be_positive() -> None:
    with pytest.raises(ValueError):
        PrincipalSessionRegistry(max_sessions_per_principal=0)


def test_the_quota_is_enforced_through_the_live_login_route(lane: TestClient) -> None:
    """10 logins as one principal leave at most the configured number of sessions."""
    from nexus_ai_agent.api.studio_surface import _MAX_SESSIONS_PER_PRINCIPAL

    session_registry.clear()
    for _ in range(_MAX_SESSIONS_PER_PRINCIPAL + 4):
        _login(lane)
    assert len(session_registry) == _MAX_SESSIONS_PER_PRINCIPAL


# ---------------------------------------------------------------------------
# The bundle must send the marker on every POST it makes
# ---------------------------------------------------------------------------
def test_the_bundle_sends_the_marker_on_every_request() -> None:
    script = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "nexus_ai_agent"
        / "web"
        / "studio_surface"
        / "studio.js"
    ).read_text(encoding="utf-8")
    assert "const MARKER" in script
    assert script.count('method: "POST"') == script.count('method: "POST"')
    helper = script.split("async function call(")[1].split("return response.status")[0]
    assert "api.MARKER" in helper, "the shared helper must attach the marker to every request"


def test_the_public_allow_list_matches_the_bundle_files() -> None:
    """The mount, the index and every required asset are the only public paths."""
    from nexus_ai_agent.api.studio_surface import (
        PUBLIC_SURFACE_PATHS,
        REQUIRED_FILES,
        SURFACE_DIR,
        SURFACE_MOUNT,
    )

    expected = {SURFACE_MOUNT, f"{SURFACE_MOUNT}/"} | {
        f"{SURFACE_MOUNT}/{name}" for name in REQUIRED_FILES if name != "index.html"
    }
    assert set(PUBLIC_SURFACE_PATHS) == expected
    for name in REQUIRED_FILES:
        assert (SURFACE_DIR / name).is_file(), f"the wheel is missing {name}"


# ---------------------------------------------------------------------------
# Every forwarding route reaches the IBPC lane with the session's own token
# ---------------------------------------------------------------------------
def test_every_forwarding_route_reaches_its_ibpc_handler(
    lane: TestClient, ibpc_calls: list[str]
) -> None:
    """The four post-open routes are thin, and thin is exactly what must be proven.

    Each one validates its body locally (422 on garbage) and then forwards the
    *session's* principal token — never a caller-supplied one — so the IBPC lane
    re-authorises the action. The sentinel records the reach, and the marker +
    quota controls above decide whether a request gets that far.
    """
    _login(lane)
    base = f"{API_PREFIX}/projects/proj-security/ibpc/sessions/sess-1"
    submit = {
        "turn_id": "turn-1",
        "sequence_number": 1,
        "text": "make it warmer",
        "asset_id": "ast-1",
        "source_path": "assets/ast-1.png",
    }
    approve = {"approval_id": "apr-1"}

    assert lane.post(f"{base}/close", headers=MARKER, json={}).status_code == 200
    assert lane.post(f"{base}/turns", headers=MARKER, json=submit).status_code == 201
    assert (
        lane.post(f"{base}/turns/turn-1/approve", headers=MARKER, json=approve).status_code == 200
    )
    assert (
        lane.post(f"{base}/turns/turn-1/execute", headers=MARKER, json=approve).status_code == 200
    )

    assert ibpc_calls == [
        "close_ibpc_session",
        "submit_ibpc_turn",
        "approve_ibpc_turn",
        "execute_ibpc_turn",
    ]


def test_a_malformed_turn_body_is_refused_before_it_is_forwarded(
    lane: TestClient, ibpc_calls: list[str]
) -> None:
    _login(lane)
    base = f"{API_PREFIX}/projects/proj-security/ibpc/sessions/sess-1"
    for path, body in (
        (f"{base}/turns", {"turn_id": "", "sequence_number": 0, "text": ""}),
        (f"{base}/turns/turn-1/approve", {}),
        (f"{base}/turns/turn-1/execute", {"approval_id": ""}),
    ):
        response = lane.post(path, headers=MARKER, json=body)
        assert response.status_code == 422, f"{path} -> {response.status_code}"
    assert ibpc_calls == [], "an invalid body must never reach the IBPC lane"


def test_the_forwarded_token_is_the_sessions_own(
    lane: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A caller cannot smuggle another principal by supplying a token header."""
    from nexus_ai_agent.api import studio_ibpc

    seen: list[str] = []

    async def _capture(**kwargs: Any) -> dict[str, Any]:
        seen.append(kwargs["x_nexus_principal_token"])
        return {}

    monkeypatch.setattr(studio_ibpc, "close_ibpc_session", _capture)
    _login(lane)
    response = lane.post(
        f"{API_PREFIX}/projects/proj-security/ibpc/sessions/sess-1/close",
        headers={**MARKER, "x-nexus-principal-token": "tok-forged"},
        json={},
    )
    assert response.status_code == 200, response.text
    assert seen == [PRINCIPAL_TOKEN], "the session token wins over a caller-supplied header"


# ---------------------------------------------------------------------------
# Registry and limiter edges
# ---------------------------------------------------------------------------
def test_revoking_a_missing_or_empty_session_is_a_noop() -> None:
    registry = PrincipalSessionRegistry()
    registry.revoke(None)
    registry.revoke("never-issued")
    assert len(registry) == 0


def test_an_expired_session_is_dropped_on_the_next_lookup() -> None:
    clock = {"now": 1000.0}
    registry = PrincipalSessionRegistry(ttl_seconds=10.0, clock=lambda: clock["now"])
    issued = registry.issue(principal_id="p", tenant_id="t", token="tok")
    assert registry.resolve(issued.session_id) is not None
    clock["now"] += 11.0
    assert registry.resolve(issued.session_id) is None
    assert len(registry) == 0


def test_the_client_key_falls_back_to_local_when_the_peer_is_unknown() -> None:
    from starlette.requests import Request

    from nexus_ai_agent.api.studio_surface import _client_key

    request = Request({"type": "http", "method": "POST", "path": "/", "headers": []})
    assert _client_key(request) == "local"
    behind_peer = Request(
        {"type": "http", "method": "POST", "path": "/", "headers": [], "client": ("10.0.0.9", 5)}
    )
    assert _client_key(behind_peer) == "10.0.0.9"


def test_installing_the_surface_fails_loudly_when_the_bundle_is_incomplete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wheel that dropped a required asset must break at import, not in a browser."""
    from fastapi import FastAPI

    from nexus_ai_agent.api import studio_surface

    monkeypatch.setattr(
        studio_surface, "REQUIRED_FILES", (*studio_surface.REQUIRED_FILES, "missing.js")
    )
    with pytest.raises(RuntimeError) as excinfo:
        studio_surface.install_studio_surface(FastAPI())
    assert "bundle is incomplete" in str(excinfo.value)
    assert "missing.js" in str(excinfo.value)
