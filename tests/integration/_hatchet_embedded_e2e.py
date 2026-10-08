"""Standalone Hatchet *embedded* E2E runner (task-255, dev/CI proof only).

Run as a script (never imported): starts the embedded Hatchet engine (no API
token, no Docker — ``Hatchet.from_embedded``), registers the fabric worker,
then drives the REAL adapter end-to-end against a canonical ``creative_render``
payload. Prints a single ``RESULT_JSON=...`` line the pytest wrapper asserts on.

Embedded is a development/CI proof path; production never selects it.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

os.environ.setdefault("NEXUS_HATCHET_ALLOW_EMBEDDED", "1")

from nexus_ai_agent.integrations.execution import (  # noqa: E402
    BackendKind,
    ExecutionAttempt,
    ExecutionFailure,
    ExecutionIdentity,
    ExecutionRequest,
    ExecutionResult,
)
from nexus_ai_agent.integrations.hatchet._sdk import new_client  # noqa: E402
from nexus_ai_agent.integrations.hatchet.adapter import (  # noqa: E402
    HatchetExecutionAdapter,
    reconcile_provider_output,
)
from nexus_ai_agent.integrations.hatchet.worker_task import (  # noqa: E402
    WORKFLOW_NAME,
    build_workflow,
)


def _identity() -> ExecutionIdentity:
    return ExecutionIdentity(
        request_id="e2e-req-1",
        idempotency_key="e2e-idem-1",
        job_id="e2e-job-1",
        try_id="e2e-job-1::try-1",
        fencing_token=5,
        backend=BackendKind.HATCHET,
    )


def _run_once(adapter: HatchetExecutionAdapter, request: ExecutionRequest):  # noqa: ANN202
    try:
        return asyncio.run(adapter.execute(request))
    except Exception as exc:  # provider transport failure → unknown, fail-closed
        return ExecutionFailure(
            identity=request.identity,
            code="provider_error",
            detail=f"{type(exc).__name__}: {exc}",
        )


def _run_once(adapter: HatchetExecutionAdapter, request: ExecutionRequest):  # noqa: ANN202
    try:
        return asyncio.run(adapter.execute(request))
    except Exception as exc:  # provider transport failure → unknown, fail-closed
        return ExecutionFailure(
            identity=request.identity,
            code="provider_error",
            detail=f"{type(exc).__name__}: {exc}",
        )


def _poll_run(client, run_id: str, timeout: float = 90.0):  # noqa: ANN202
    """Poll the REST run details until terminal (or timeout)."""
    deadline = time.time() + timeout
    details = None
    while time.time() < deadline:
        details = client.runs.get(run_id)
        status = str(getattr(details.run, "status", ""))
        if status in {"COMPLETED", "FAILED", "CANCELLED"}:
            return details
        time.sleep(2)
    return details


def _wait_for_workflow(client, name: str, timeout: float = 60.0) -> bool:
    """Poll until the embedded engine knows this workflow (registered)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            listing = client.workflows.list(workflow_name=name, limit=1)
            if getattr(listing, "rows", None):
                return True
        except Exception:
            pass
        time.sleep(2)
    return False


def main() -> int:
    client = new_client(embedded=True)
    workflow = build_workflow(client)
    worker = client.worker("nexus-fabric-e2e-worker", workflows=[workflow])
    thread = threading.Thread(target=worker.start, daemon=True)
    thread.start()
    # Trigger only after the worker has registered the workflow (real readiness).
    ready = _wait_for_workflow(client, WORKFLOW_NAME)
    print(f"WORKFLOW_REGISTERED={ready}")

    identity = _identity()
    base = tempfile.mkdtemp(prefix="nexus_e2e_")
    payload = {
        "user_id": 1,
        "text": "nexus execution fabric e2e",
        "output_path": str(Path(base) / "story.png"),
    }
    ref = client.workflow(name=WORKFLOW_NAME).run_no_wait(
        {
            "operation": "story",
            "payload": payload,
            "staging_dir": base,
            "metadata": identity.to_metadata(),
        },
        additional_metadata=identity.to_metadata(),
    )
    run_id = ref.workflow_run_id
    details = _poll_run(client, run_id, timeout=90)
    task = details.tasks[0] if getattr(details, "tasks", None) else None
    raw_witness = getattr(task, "output", None)
    attempt = int(getattr(task, "attempt", 0) or 0) + 1

    # Exercise the REAL seam mapping on the REAL witness from the engine.
    request = ExecutionRequest(
        identity=identity,
        operation="story",
        payload=payload,
        staging_dir=base,  # the workspace the handler wrote into (rule 10)
    )
    reconciled = reconcile_provider_output(identity, raw_witness, request)
    provider_result = ExecutionResult(
        identity=identity.with_provider_run(run_id),
        attempt=ExecutionAttempt(identity=identity, provider_attempt=max(1, attempt)),
        state=reconciled.state,
        witness=dict(raw_witness or {}),
        staged_artifact_path=(raw_witness or {}).get("staged_artifact_path"),
        artifact_sha256=(raw_witness or {}).get("artifact_sha256"),
    )
    summary = {
        "backend": BackendKind.HATCHET.value,
        "workflow_registered": ready,
        "provider_run_id": run_id,
        "run_status": str(getattr(details.run, "status", "")),
        "task_status": str(getattr(task, "status", "")),
        "provider_attempt": max(1, attempt),
        "fencing_token": provider_result.identity.fencing_token,
        "is_claim": provider_result.is_success_claim,
        "state": reconciled.state.value,
        "witness": dict(raw_witness or {}),
    }
    try:
        client.stop_embedded()
    except Exception:
        pass
    print("RESULT_JSON=" + json.dumps(summary, sort_keys=True), flush=True)
    time.sleep(2)
    os._exit(0)  # embedded engine threads are daemons; exit after the proof


if __name__ == "__main__":
    sys.exit(main())
