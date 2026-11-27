"""The Studio product surface must exist, be served, and be honest about state.

Product frame: a capability has to be reachable without Telegram. The Studio API
existed but had no human surface on this path, so this bundle is the reference
client. These tests pin the three panels, the graceful failure states and the
presentation logic (run under Node, no browser needed).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nexus_ai_agent.api.app import app
from nexus_ai_agent.api.studio_surface import (
    API_PREFIX,
    REQUIRED_FILES,
    SURFACE_DIR,
    SURFACE_MOUNT,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(scope="module")
def html() -> str:
    return (SURFACE_DIR / "index.html").read_text(encoding="utf-8")


def test_bundle_files_are_present() -> None:
    for name in REQUIRED_FILES:
        assert (SURFACE_DIR / name).stat().st_size > 0, name


def test_index_is_served_at_the_mount(client: TestClient) -> None:
    response = client.get(f"{SURFACE_MOUNT}/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "NAGAR Studio" in response.text


def test_bare_mount_redirects_to_the_trailing_slash(client: TestClient) -> None:
    response = client.get(SURFACE_MOUNT, follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == f"{SURFACE_MOUNT}/"


def test_responses_carry_a_strict_content_security_policy(client: TestClient) -> None:
    for path in (f"{SURFACE_MOUNT}/", f"{SURFACE_MOUNT}/studio.js"):
        headers = client.get(path).headers
        assert headers["x-content-type-options"] == "nosniff"
        policy = headers["content-security-policy"]
        assert "script-src 'self'" in policy
        assert "frame-ancestors 'none'" in policy
        assert "unsafe-inline" not in policy


def test_assets_are_served(client: TestClient) -> None:
    js = client.get(f"{SURFACE_MOUNT}/studio.js")
    css = client.get(f"{SURFACE_MOUNT}/studio.css")
    assert js.status_code == 200 and "StudioSurface" in js.text
    assert css.status_code == 200 and "--accent" in css.text
    assert client.get(f"{SURFACE_MOUNT}/nope.js").status_code == 404


def test_surface_has_the_three_panels(html: str) -> None:
    for region in ('id="sessions"', 'id="composer"', 'id="turns"'):
        assert region in html, region
    for field in ('name="project_id"', 'name="text"', 'name="asset_id"', 'name="source_path"'):
        assert field in html, field


def test_surface_declares_rtl_persian_and_no_inline_script(html: str) -> None:
    assert 'lang="fa"' in html and 'dir="rtl"' in html
    assert "<script src=" in html
    assert "<script>" not in html, "an inline script would need a CSP exception"


def test_every_element_the_script_reaches_exists_in_the_markup(html: str) -> None:
    """A renamed id is a silent `TypeError` in the browser, not a test failure."""
    import re

    script = (SURFACE_DIR / "studio.js").read_text(encoding="utf-8")
    referenced = set(re.findall(r'\$\("([a-z0-9-]+)"\)', script))
    declared = set(re.findall(r'id="([a-z0-9-]+)"', html))
    assert referenced, "the script wires nothing"
    assert referenced <= declared, f"script reaches missing ids: {sorted(referenced - declared)}"


def test_surface_has_a_live_status_region(html: str) -> None:
    assert 'id="status"' in html and 'aria-live="polite"' in html


def test_accessible_labels_and_skip_link(html: str) -> None:
    assert 'class="skip"' in html
    assert html.count("<label>") >= 4


def test_package_data_ships_the_bundle() -> None:
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert '"nexus_ai_agent.web" = ["static/*", "studio_surface/*"]' in text


NODE_PROGRAM = r"""
const surface = require(process.argv[2]);
const out = {
  pass: surface.gateBadge("PASS"),
  abstain: surface.gateBadge("abstain_insufficient_evidence"),
  fail: surface.gateBadge("FAIL"),
  unknownGate: surface.gateBadge("something-else"),
  granted: surface.permissionBadge("GRANTED"),
  approval: surface.permissionBadge("requires_approval"),
  denied: surface.permissionBadge("denied"),
  uncertaintyNumber: surface.uncertaintyLabel(0.25),
  uncertaintyExpanded: surface.uncertaintyLabel({ expanded: 0.12, coverage: 0.95 }),
  uncertaintyMissing: surface.uncertaintyLabel(null),
  unauthorized: surface.statusFromError({ status: 401 }),
  unavailable: surface.statusFromError({ status: 503 }),
  offline: surface.statusFromError({ name: "TypeError" }),
  other: surface.statusFromError({ status: 500 }),
  turn: surface.turnView({
    text: "x".repeat(120),
    state: "completed",
    gate: "PASS",
    permission: "granted",
    uncertainty: 0.5,
    sequence_number: 2,
  }),
  sessions: surface.sessionItems({
    sessions: [
      { session_id: "s-1", status: "ACTIVE", max_sequence: 2, session_epoch: 3 },
      { status: "CLOSED" },
    ],
  }),
  emptySessions: surface.sessionItems({}),
  nextFromTurns: surface.nextSequence({ turns: [{ sequence_number: 1 }, { sequence_number: 4 }] }),
  nextFromMax: surface.nextSequence({ max_sequence: 7 }),
  nextEmpty: surface.nextSequence({}),
  idempotency: surface.idempotencyKey("p1", "s1", "t1"),
  message: surface.statusMessage("unavailable"),
};
process.stdout.write(JSON.stringify(out));
"""


def _node() -> str:
    node = shutil.which("node")
    if node is None:  # pragma: no cover - CI installs Node; local shells may not
        pytest.skip("node is not installed")
    return node


def test_presentation_logic_under_node(tmp_path: Path) -> None:
    program = tmp_path / "check.cjs"
    program.write_text(NODE_PROGRAM, encoding="utf-8")
    result = subprocess.run(
        [_node(), str(program), str(SURFACE_DIR / "studio.js")],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    out = json.loads(result.stdout)
    assert out["pass"] == {"label": "PASS", "tone": "ok"}
    assert out["abstain"] == {"label": "ABSTAIN", "tone": "warn"}
    assert out["fail"] == {"label": "FAIL", "tone": "bad"}
    assert out["unknownGate"]["label"] == "نامشخص"
    assert out["granted"]["tone"] == "ok"
    assert out["approval"]["tone"] == "warn"
    assert out["denied"]["tone"] == "bad"
    assert out["uncertaintyNumber"] == "±0.250"
    assert out["uncertaintyExpanded"] == "±0.120 (95%)"
    assert out["uncertaintyMissing"] == "—"
    assert out["unauthorized"] == "unauthorized"
    assert out["unavailable"] == "unavailable"
    assert out["offline"] == "unavailable"
    assert out["other"] == "error"
    turn = out["turn"]
    assert turn["subtitle"] == "نوبت 2"
    assert turn["title"].endswith("…") and len(turn["title"]) <= 80
    assert [b["label"] for b in turn["badges"]] == ["completed", "PASS", "مجاز", "u ±0.500"]
    assert out["sessions"][0]["id"] == "s-1"
    assert out["sessions"][0]["detail"] == "ACTIVE · 2 نوبت · epoch 3"
    assert out["sessions"][1]["title"] == "بدون شناسه"
    assert out["emptySessions"] == []
    # A hardcoded sequence number would collide on the second turn of a session.
    assert out["nextFromTurns"] == 5
    assert out["nextFromMax"] == 8
    assert out["nextEmpty"] == 1
    assert out["idempotency"] == "studio:p1:s1:t1"
    assert "در دسترس نیست" in out["message"]


# ---------------------------------------------------------------------------
# The authenticated lane: a public bundle, private data.
# ---------------------------------------------------------------------------

PRINCIPAL = "prn_" + "a" * 32
PRINCIPAL_TOKEN = "tok-prin-w11x-secret"
PROJECT = "proj-w11x"


@pytest.fixture()
def lane(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """A real principal store, a real workspace, and a cookie-carrying client."""
    import base64

    from nexus_ai_agent.adapters.studio_principal_auth import StudioPrincipalAuthStore
    from nexus_ai_agent.api.studio_surface import session_registry
    from nexus_ai_agent.nagar.ibpc.authority import StudioRole

    workspace = tmp_path / "ws"
    workspace.mkdir()
    auth_db = tmp_path / "auth.sqlite3"
    monkeypatch.setenv("NEXUS_STUDIO_AUTH_DB", str(auth_db))
    monkeypatch.setenv("NEXUS_STUDIO_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("NEXUS_STUDIO_ASSET_ROOT", str(workspace))
    monkeypatch.setenv("NEXUS_STUDIO_ACTIVE_REVISION", "0")
    key = base64.b64encode(b"z" * 32).decode("ascii")
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
    store.bind_channel(
        PRINCIPAL,
        channel="studio",
        external_subject="sub-w11x",
        user_id=1,
        chat_id=1,
        allow_system_bootstrap=True,
    )
    store.ensure_project(PROJECT, tenant_id="ten-1", active_revision_id="0")
    store.grant_membership(
        PROJECT, PRINCIPAL, role=StudioRole.OWNER, granted_by="system", allow_system_bootstrap=True
    )
    store.close()

    session_registry.clear()
    # https base_url: the cookie is Secure, and a test client on http would
    # never send it back — the same rule a browser enforces.
    client = TestClient(app, base_url="https://testserver")
    yield client
    session_registry.clear()


def _login(client: TestClient) -> None:
    response = client.post(
        f"{API_PREFIX}/login",
        json={"principal_token": PRINCIPAL_TOKEN},
        headers={"x-nagar-studio": "1"},
    )
    assert response.status_code == 200, response.text


def test_lane_requires_a_session(lane: TestClient) -> None:
    assert lane.get(f"{API_PREFIX}/me").status_code == 401
    assert lane.get(f"{API_PREFIX}/sessions", params={"project_id": PROJECT}).status_code == 401
    assert lane.post(f"{API_PREFIX}/projects/{PROJECT}/ibpc/sessions", json={}).status_code == 401


def test_login_requires_the_csrf_marker(lane: TestClient) -> None:
    response = lane.post(f"{API_PREFIX}/login", json={"principal_token": PRINCIPAL_TOKEN})
    assert response.status_code == 403


def test_login_rejects_a_wrong_token(lane: TestClient) -> None:
    response = lane.post(
        f"{API_PREFIX}/login",
        json={"principal_token": "not-the-token"},
        headers={"x-nagar-studio": "1"},
    )
    assert response.status_code == 401


def test_login_sets_an_opaque_httponly_cookie_and_never_echoes_the_token(
    lane: TestClient,
) -> None:
    response = lane.post(
        f"{API_PREFIX}/login",
        json={"principal_token": PRINCIPAL_TOKEN},
        headers={"x-nagar-studio": "1"},
    )
    assert response.status_code == 200, response.text
    assert PRINCIPAL_TOKEN not in response.text
    assert response.json()["principal_id"] == PRINCIPAL
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "secure" in cookie and "samesite=strict" in cookie
    assert PRINCIPAL_TOKEN.lower() not in cookie
    me = lane.get(f"{API_PREFIX}/me")
    assert me.status_code == 200
    assert me.json()["principal_id"] == PRINCIPAL


def test_logout_revokes_the_server_side_session(lane: TestClient) -> None:
    _login(lane)
    assert lane.get(f"{API_PREFIX}/me").status_code == 200
    out = lane.post(f"{API_PREFIX}/logout", json={}, headers={"x-nagar-studio": "1"})
    assert out.status_code == 200
    assert lane.get(f"{API_PREFIX}/me").status_code == 401


def test_sessions_panel_lists_a_real_durable_session(lane: TestClient) -> None:
    _login(lane)
    empty = lane.get(f"{API_PREFIX}/sessions", params={"project_id": PROJECT})
    assert empty.status_code == 200 and empty.json()["sessions"] == []

    opened = lane.post(f"{API_PREFIX}/projects/{PROJECT}/ibpc/sessions", json={})
    assert opened.status_code == 201, opened.text
    session_id = opened.json()["session_id"]

    listed = lane.get(f"{API_PREFIX}/sessions", params={"project_id": PROJECT})
    assert listed.status_code == 200, listed.text
    ids = [row["session_id"] for row in listed.json()["sessions"]]
    assert session_id in ids, "the listing must read the durable store, not a cache"

    detail = lane.get(f"{API_PREFIX}/projects/{PROJECT}/ibpc/sessions/{session_id}")
    assert detail.status_code == 200
    assert detail.json()["session_id"] == session_id


def test_sessions_panel_refuses_a_project_the_caller_cannot_read(lane: TestClient) -> None:
    _login(lane)
    response = lane.get(f"{API_PREFIX}/sessions", params={"project_id": "proj-someone-else"})
    assert response.status_code == 403, response.text


def test_lane_rejects_an_invalid_body_with_422(lane: TestClient) -> None:
    _login(lane)
    response = lane.post(f"{API_PREFIX}/projects/{PROJECT}/ibpc/sessions", json={"nope": 1})
    assert response.status_code == 422, response.text


def test_the_bundle_never_touches_persistent_storage() -> None:
    """A token in localStorage is a token in every XSS payload's reach."""
    script = (SURFACE_DIR / "studio.js").read_text(encoding="utf-8")
    html = (SURFACE_DIR / "index.html").read_text(encoding="utf-8")
    for forbidden in ("localStorage.", "localStorage[", "sessionStorage.", "document.cookie"):
        assert forbidden not in script, forbidden
        assert forbidden not in html, forbidden
    assert "principal_token" in script  # posted once, then cleared
    assert "sequence_number: 1," not in script, "a hardcoded sequence number would collide"
