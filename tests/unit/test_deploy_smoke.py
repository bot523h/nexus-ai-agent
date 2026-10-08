"""Unit tests for scripts/deploy_smoke.py (task-130).

The manifest/contract checks run against fixture files only; the live probes
run against an in-process stub HTTP server, so the suite needs no network,
no PyYAML-free fallback assumptions, and no deployed instance.
"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from deploy_smoke import (  # noqa: E402
    CheckResult,
    check_contract,
    check_healthz,
    check_manifest,
    check_webhook_gate,
    main,
    run_smoke,
    summarize,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

VALID_MANIFEST = """\
name: nexus-ai
services:
  - name: bot
    type: web
    docker:
      dockerfile: Dockerfile
    env:
      - name: TELEGRAM_BOT_TOKEN
        value: "{{ .Env.TELEGRAM_BOT_TOKEN }}"
      - name: GEMINI_API_KEY
        value: "{{ .Env.GEMINI_API_KEY }}"
      - name: NEXUS_RUN_MODE
        value: "webhook"
      - name: NEXUS_WEBHOOK_URL
        value: "{{ .Env.NEXUS_WEBHOOK_URL }}"
      - name: NEXUS_WEBHOOK_SECRET
        value: "{{ .Env.NEXUS_WEBHOOK_SECRET }}"
    health_check:
      path: /healthz
"""


def _write_manifest(tmp_path: Path, text: str = VALID_MANIFEST) -> Path:
    path = tmp_path / "koyeb.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def _statuses(results: list[CheckResult]) -> list[str]:
    return [r.status for r in results]


def test_valid_manifest_passes(tmp_path: Path) -> None:
    results = check_manifest(_write_manifest(tmp_path))
    assert "FAIL" not in _statuses(results)
    assert any("type 'web'" in r.detail for r in results)


def test_manifest_rejects_worker_type(tmp_path: Path) -> None:
    results = check_manifest(
        _write_manifest(tmp_path, VALID_MANIFEST.replace("type: web", "type: worker"))
    )
    assert any(r.status == "FAIL" and "scale-to-zero" in r.detail for r in results)


def test_manifest_rejects_wrong_health_path(tmp_path: Path) -> None:
    results = check_manifest(
        _write_manifest(tmp_path, VALID_MANIFEST.replace("path: /healthz", "path: /"))
    )
    assert any(r.status == "FAIL" and "/healthz" in r.detail for r in results)


def test_manifest_rejects_missing_required_env(tmp_path: Path) -> None:
    text = "\n".join(
        line for line in VALID_MANIFEST.splitlines() if "NEXUS_WEBHOOK_SECRET" not in line
    )
    results = check_manifest(_write_manifest(tmp_path, text))
    assert any(r.status == "FAIL" and "NEXUS_WEBHOOK_SECRET" in r.detail for r in results)


def test_manifest_rejects_polling_run_mode(tmp_path: Path) -> None:
    results = check_manifest(
        _write_manifest(tmp_path, VALID_MANIFEST.replace('value: "webhook"', 'value: "polling"'))
    )
    assert any(r.status == "FAIL" and "NEXUS_RUN_MODE" in r.detail for r in results)


def test_manifest_rejects_missing_service(tmp_path: Path) -> None:
    results = check_manifest(_write_manifest(tmp_path, "name: nexus-ai\nservices: []\n"))
    assert any(r.status == "FAIL" and "no service" in r.detail for r in results)


def test_manifest_missing_file_fails(tmp_path: Path) -> None:
    results = check_manifest(tmp_path / "koyeb.yaml")
    assert _statuses(results) == ["FAIL"]


def test_contract_passes_on_real_repo() -> None:
    assert "FAIL" not in _statuses(check_contract(REPO_ROOT))


def test_contract_fails_on_empty_dir(tmp_path: Path) -> None:
    results = check_contract(tmp_path)
    assert any(r.status == "FAIL" and "Dockerfile" in r.detail for r in results)
    assert any(r.status == "FAIL" and "api/app.py" in r.detail for r in results)
    assert any(r.status == "FAIL" and "locale files" in r.detail for r in results)


def test_contract_flags_broken_locale(tmp_path: Path) -> None:
    locales = tmp_path / "src" / "nexus_ai_agent" / "i18n" / "locales"
    locales.mkdir(parents=True)
    for index in range(15):
        (locales / f"l{index:02d}.json").write_text('{"k": "v"}', encoding="utf-8")
    (locales / "l00.json").write_text("{not json", encoding="utf-8")
    results = check_contract(tmp_path)
    assert any(r.status == "FAIL" and "l00" in r.detail for r in results)


class _StubHandler(BaseHTTPRequestHandler):
    mode: str = "healthy"

    def log_message(self, *_args: object) -> None:  # silence test output
        return

    def _send(self, status: int, payload: object) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/healthz" and _StubHandler.mode == "healthy":
            self._send(200, {"status": "ok"})
        elif self.path == "/healthz":
            self._send(200, {"status": "broken"})
        else:
            self._send(404, {})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        if self.path == "/webhook/telegram" and _StubHandler.mode == "healthy":
            self._send(403, {"detail": "bad secret"})
        else:
            self._send(200, {"status": "ok"})


@pytest.fixture()
def stub_url() -> str:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
    _StubHandler.mode = "healthy"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    thread.join(timeout=5)


def test_healthz_probe_passes(stub_url: str) -> None:
    assert check_healthz(stub_url, timeout=5).status == "PASS"


def test_healthz_probe_fails_on_wrong_payload(stub_url: str) -> None:
    _StubHandler.mode = "broken"
    result = check_healthz(stub_url, timeout=5)
    assert result.status == "FAIL"


def test_healthz_probe_fails_when_unreachable() -> None:
    result = check_healthz("http://127.0.0.1:1", timeout=1)
    assert result.status == "FAIL" and "unreachable" in result.detail


def test_webhook_gate_probe_passes(stub_url: str) -> None:
    assert check_webhook_gate(stub_url, timeout=5).status == "PASS"


def test_webhook_gate_probe_fails_when_not_rejected(stub_url: str) -> None:
    _StubHandler.mode = "broken"
    result = check_webhook_gate(stub_url, timeout=5)
    assert result.status == "FAIL" and "403" in result.detail


def test_webhook_gate_probe_fails_when_unreachable() -> None:
    result = check_webhook_gate("http://127.0.0.1:1", timeout=1)
    assert result.status == "FAIL" and "unreachable" in result.detail


def test_run_smoke_offline_only_without_url() -> None:
    results = run_smoke(repo_root=REPO_ROOT)
    names = {r.name for r in results}
    assert names == {"manifest", "contract"}


def test_run_smoke_includes_live_probes(stub_url: str) -> None:
    results = run_smoke(repo_root=REPO_ROOT, base_url=stub_url, timeout=5)
    names = {r.name for r in results}
    assert {"manifest", "contract", "healthz", "webhook-gate"} <= names
    assert "FAIL" not in _statuses(results)


def test_summarize_exit_codes(capsys: pytest.CaptureFixture[str]) -> None:
    assert summarize([CheckResult("a", "PASS", "ok")], strict=True) == 0
    assert summarize([CheckResult("a", "SKIP", "na")], strict=False) == 0
    assert summarize([CheckResult("a", "SKIP", "na")], strict=True) == 1
    assert summarize([CheckResult("a", "FAIL", "bad")], strict=False) == 1


def test_main_offline_green_on_real_repo(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    assert "checks green" in capsys.readouterr().out


def test_main_rejects_bad_timeout() -> None:
    assert main(["--timeout", "0"]) == 2
