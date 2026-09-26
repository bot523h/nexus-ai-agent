"""Deploy-smoke readiness + stale-deployment detection (mission: deploy truth).

The live deploy gate (``scripts/deploy_smoke.py``) is the executable answer
to "how do we know the deployment actually landed the build we shipped?"
These tests pin the readiness/identity check:

* healthy backend → ``readyz`` PASS with the running version reported;
* backend answering ``503 not_ready`` → FAIL with the readiness reason AND
  the explicit "do not promote / roll back" operator direction (never a bare
  red cross on a deploy gate);
* version mismatch against ``--expect-version`` → FAIL naming BOTH versions
  and the rollback direction — the stale-deployment chaos leg;
* a 200 without identity payload → FAIL (identity is the contract, not the
  status code alone);
* unreachable instance → FAIL unreachable (network chaos);
* offline contract check now includes the ``/readyz`` route;
* CLI ``--expect-version`` wires through to the check.

Stubs below are local HTTP doubles for the smoke's OWN assertions — the
live Koyeb leg stays documented-as-owner-side and is never faked/greened.
"""

from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from deploy_smoke import (  # noqa: E402
    CheckResult,
    check_contract,
    check_readyz,
    main,
    run_smoke,
)


class _ReadyzStub(BaseHTTPRequestHandler):
    """Answers /readyz according to a shared mode (local stub for the gate)."""

    mode = "healthy"  # healthy | not_ready | no_identity | wrong_version | bare_ok
    payload: dict | None = None

    def log_message(self, *_args) -> None:  # keep the suite quiet
        return

    def _send(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:
        # Webhook gate probe expects rejection without the secret token:
        self._send(403, {"detail": "forbidden"})

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._send(200, {"status": "ok" if self.mode == "healthy" else "broken"})
            return
        if self.path != "/readyz":
            self._send(404, {})
            return
        if self.payload is not None:
            self._send(200, self.payload)
            return
        if self.mode == "healthy":
            self._send(
                200,
                {
                    "status": "ready",
                    "version": "3.14.0",
                    "deploy_git_sha": "deadbeef",
                    "db": {"ok": True, "backend": "sqlite", "detail": None},
                },
            )
        elif self.mode == "not_ready":
            self._send(
                503,
                {
                    "status": "not_ready",
                    "version": "3.14.0",
                    "db": {"ok": False, "backend": "sqlite", "detail": "db file missing"},
                },
            )
        elif self.mode == "no_identity":
            self._send(200, {"something": "else"})
        elif self.mode == "bare_ok":
            self._send(200, {"status": "ready"})


@pytest.fixture()
def server():
    stub_server = ThreadingHTTPServer(("127.0.0.1", 0), _ReadyzStub)
    thread = __import__("threading").Thread(target=stub_server.serve_forever, daemon=True)
    thread.start()
    _ReadyzStub.payload = None
    _ReadyzStub.mode = "healthy"
    yield f"http://127.0.0.1:{stub_server.server_address[1]}"
    stub_server.shutdown()
    thread.join(timeout=5)


class TestReadyzCheckMatrix:
    def test_healthy_backend_passes_with_version(self, server):
        result = check_readyz(server, timeout=2.0)
        assert result.status == "PASS"
        assert "3.14.0" in result.detail
        assert "deadbeef" in result.detail

    def test_expect_version_match_passes(self, server):
        assert check_readyz(server, timeout=2.0, expect_version="3.14.0").status == "PASS"

    def test_stale_deployment_version_mismatch_fails_with_rollback_direction(self, server):
        # The central chaos leg: operator expects 9.9.9, runtime reports 3.14.0.
        result = check_readyz(server, timeout=2.0, expect_version="9.9.9")
        assert result.status == "FAIL"
        assert "9.9.9" in result.detail and "3.14.0" in result.detail
        assert "stale" in result.detail and "roll back" in result.detail

    def test_not_ready_backend_fails_actionably(self, server):
        _ReadyzStub.mode = "not_ready"
        result = check_readyz(server, timeout=2.0)
        assert result.status == "FAIL"
        assert "NOT READY" in result.detail
        assert "db file missing" in result.detail
        assert "roll back" in result.detail

    def test_200_without_identity_payload_fails(self, server):
        _ReadyzStub.mode = "no_identity"
        assert check_readyz(server, timeout=2.0).status == "FAIL"

    def test_200_with_status_but_no_version_fails(self, server):
        _ReadyzStub.mode = "bare_ok"
        assert check_readyz(server, timeout=2.0).status == "FAIL"

    def test_unreachable_instance_fails_unreachable(self):
        # Nothing binds this port in CI runs reliably enough for a refusal:
        result = check_readyz("http://127.0.0.1:9", timeout=1.0)
        assert result.status == "FAIL"
        assert "unreachable" in result.detail


class TestOfflineContractExtension:
    def test_contract_state_now_includes_readyz_route(self):
        results = check_contract(Path(__file__).resolve().parents[2])
        route_results = [r for r in results if "/readyz" in r.detail]
        assert route_results and all(r.status == "PASS" for r in route_results), results

    def test_run_smoke_offline_is_unaffected_without_url(self):
        results = run_smoke()
        assert all(isinstance(r, CheckResult) for r in results)
        assert {r.name for r in results} == {"manifest", "contract"}


class TestCliWiring:
    def test_expect_version_flag_survives_to_live_checks(self, server):
        assert main(["--url", server, "--expect-version", "3.14.0"]) == 0

    def test_stale_deployment_exits_one(self, server):
        assert main(["--url", server, "--expect-version", "9.9.9"]) == 1
