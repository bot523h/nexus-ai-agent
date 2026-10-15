#!/bin/sh
# Container liveness probe (closeout 2026-10-15, audit A-16).
#
# The image runs in two modes (see bot/webhook.py):
#   webhook  — an HTTP server is up; probe GET /healthz on the bound port
#              (PORT > DASHBOARD_PORT > 8000, the same priority as the server).
#   polling  — there is NO HTTP server, so probing /healthz would mark every
#              healthy polling deployment unhealthy; instead assert that the
#              bot process (`… cli run-bot …`) is alive in this PID namespace.
# Exit 0 = healthy, 1 = unhealthy (Docker semantics).
set -u
mode="${NEXUS_RUN_MODE:-polling}"
if [ "$mode" = "webhook" ]; then
  exec python - <<'PY'
import os, sys, urllib.request
port = os.environ.get("PORT") or os.environ.get("DASHBOARD_PORT") or "8000"
try:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5) as response:
        sys.exit(0 if response.status == 200 else 1)
except Exception:  # noqa: BLE001 - any failure is "unhealthy"
    sys.exit(1)
PY
fi
exec python - <<'PY'
import os, sys
for pid in os.listdir("/proc"):
    if not pid.isdigit():
        continue
    try:
        argv = open(f"/proc/{pid}/cmdline", "rb").read().split(b"\0")
    except OSError:
        continue
    if b"run-bot" in argv and any(b"nexus_ai_agent" in a or a.endswith(b"nexus") for a in argv):
        sys.exit(0)
sys.exit(1)
PY
