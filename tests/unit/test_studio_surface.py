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
from nexus_ai_agent.api.studio_surface import REQUIRED_FILES, SURFACE_DIR, SURFACE_MOUNT

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
  sessions: surface.sessionItems({ projects: [{ id: "p1", jobs: [1, 2] }, "p2"] }),
  emptySessions: surface.sessionItems({}),
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
    assert out["sessions"] == [
        {"id": "p1", "title": "p1", "detail": "2 کار"},
        {"id": "p2", "title": "p2", "detail": "0 کار"},
    ]
    assert out["emptySessions"] == []
    assert "در دسترس نیست" in out["message"]
