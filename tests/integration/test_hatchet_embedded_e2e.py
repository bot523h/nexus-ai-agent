"""Real embedded-Hatchet E2E proof for the Execution Fabric (task-255).

Deselected from the default suite (pytest.ini ``-m "not integration"`` would
need the marker; we use an explicit name so it never runs by accident) and
gated behind ``NEXUS_HATCHET_E2E=1``: it starts the embedded Hatchet engine
(no token, no Docker) and is therefore a development/CI proof, not a unit test.

It asserts the *seam* on a real engine: the adapter keeps ONE Nexus fencing
identity, the provider run is real, and a canonical handler's typed failure
flows back as a claim (never a commit).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
RUNNER = Path(__file__).with_name("_hatchet_embedded_e2e.py")

pytestmark = pytest.mark.skipif(
    os.environ.get("NEXUS_HATCHET_E2E") != "1",
    reason="embedded Hatchet E2E is a dev/CI proof; set NEXUS_HATCHET_E2E=1 to run",
)


def test_embedded_e2e_keeps_one_fencing_identity() -> None:
    proc = subprocess.run(
        [sys.executable, str(RUNNER)],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        timeout=300,
    )
    assert proc.returncode == 0, f"runner failed:\n{proc.stdout}\n{proc.stderr}"
    line = next((ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT_JSON=")), None)
    assert line, f"no RESULT_JSON line:\n{proc.stdout}\n{proc.stderr}"
    summary = json.loads(line.split("=", 1)[1])
    assert summary["backend"] == "hatchet"
    assert summary["workflow_registered"] is True
    # A real provider run, and the canonical handler's artifact flowed back.
    assert summary["provider_run_id"], "a real provider run id must be recorded"
    assert summary["run_status"].endswith("COMPLETED")
    assert summary["state"] == "succeeded"
    assert summary["is_claim"] is True  # a claim for the verifier, never a commit
    # ONE Nexus fencing identity survives real provider execution (PHASE E).
    assert summary["fencing_token"] == 5
    assert summary["witness"]["artifact_sha256"]
