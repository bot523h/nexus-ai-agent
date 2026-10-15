"""Container image contract (audit A-16, closeout 2026-10-15).

* the bot runs as an unprivileged user — ``USER`` is set *after* the install
  steps and before ``CMD``;
* the image declares a ``HEALTHCHECK`` that is **mode-aware**: polling
  deployments have no HTTP server, so a blanket ``/healthz`` probe would mark
  every healthy worker-type deployment unhealthy.  The probe script is run for
  real here: against a local HTTP server in webhook mode, and against a live
  ``run-bot`` process in polling mode.
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
DOCKERFILE = (REPO / "Dockerfile").read_text(encoding="utf-8")
HEALTHCHECK = REPO / "docker" / "healthcheck.sh"

_INSTRUCTIONS = [
    line.split()[0]
    for line in DOCKERFILE.splitlines()
    if line.strip() and not line.startswith("#") and not line.startswith(" ")
]


def test_image_runs_as_an_unprivileged_user() -> None:
    assert "USER nexus" in DOCKERFILE
    user_at = _INSTRUCTIONS.index("USER")
    assert "CMD" in _INSTRUCTIONS[user_at:], "USER must precede CMD"
    # Every RUN (apt-get, pip install, chown) happens before dropping privileges.
    assert "RUN" not in _INSTRUCTIONS[user_at + 1 :]
    assert re.search(r"chown -R nexus:nexus /app", DOCKERFILE), "the app dir must be writable"


def test_image_declares_a_mode_aware_healthcheck() -> None:
    assert "HEALTHCHECK" in _INSTRUCTIONS
    assert '"/app/docker/healthcheck.sh"' in DOCKERFILE
    assert HEALTHCHECK.is_file()
    assert os.access(HEALTHCHECK, os.X_OK), "healthcheck.sh must be executable"
    assert "docker/" not in (REPO / ".dockerignore").read_text(encoding="utf-8")
    script = HEALTHCHECK.read_text(encoding="utf-8")
    assert "NEXUS_RUN_MODE" in script and "run-bot" in script
    # The Dockerfile itself never hard-codes an HTTP probe (polling has none).
    healthcheck_line = next(line for line in DOCKERFILE.splitlines() if "HEALTHCHECK" in line)
    assert "healthz" not in healthcheck_line and "curl" not in healthcheck_line


class _Healthz(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        status = 200 if self.path == "/healthz" else 404
        self.send_response(status)
        self.end_headers()
        self.wfile.write(b'{"status": "ok"}')

    def log_message(self, *args: object) -> None:  # silence
        return


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _probe(**env: str) -> int:
    merged = {k: v for k, v in os.environ.items() if k not in {"PORT", "DASHBOARD_PORT"}}
    merged.update(env)
    return subprocess.run(["sh", str(HEALTHCHECK)], env=merged, timeout=30, check=False).returncode


def test_webhook_mode_probes_healthz_on_the_bound_port() -> None:
    port = _free_port()
    server = HTTPServer(("127.0.0.1", port), _Healthz)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert _probe(NEXUS_RUN_MODE="webhook", PORT=str(port)) == 0
        assert _probe(NEXUS_RUN_MODE="webhook", DASHBOARD_PORT=str(port)) == 0  # fallback order
    finally:
        server.shutdown()
        server.server_close()
    assert _probe(NEXUS_RUN_MODE="webhook", PORT=str(port)) == 1  # server gone → unhealthy


@pytest.mark.skipif(not Path("/proc").is_dir(), reason="needs procfs")
def test_polling_mode_checks_the_bot_process_not_http() -> None:
    # No run-bot process anywhere → unhealthy, and no HTTP server was needed.
    assert _probe(NEXUS_RUN_MODE="polling") == 1
    bot = subprocess.Popen(  # a sleeper carrying the production argv shape
        [sys.executable, "-c", "import time; time.sleep(30)", "nexus_ai_agent.cli", "run-bot"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        time.sleep(0.2)
        assert _probe(NEXUS_RUN_MODE="polling") == 0
    finally:
        bot.kill()
        bot.wait(timeout=10)
    assert _probe(NEXUS_RUN_MODE="polling") == 1
