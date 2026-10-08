"""Hatchet adapter tests (task-255) with a fake provider — no SDK, no network.

They prove the adapter's safety properties at the seam: it reuses the canonical
handler, keeps Nexus identity stable across provider retries, maps a missing
result fail-closed, refuses a staged path that escapes staging, cancels
best-effort, and raises a typed error when the provider refuses the run.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from nexus_ai_agent.integrations.execution import (
    BackendKind,
    ExecutionFailure,
    ExecutionIdentity,
    ExecutionRequest,
    ExecutionResult,
    OutcomeState,
)
from nexus_ai_agent.integrations.hatchet.adapter import (
    HatchetExecutionAdapter,
    HatchetTriggerError,
    reconcile_provider_output,
)


def _identity(**over: object) -> ExecutionIdentity:
    base: dict[str, Any] = dict(
        request_id="req-1",
        idempotency_key="idem-1",
        job_id="job-1",
        try_id="job-1::try-1",
        fencing_token=1,
        backend=BackendKind.HATCHET,
    )
    base.update(over)
    return ExecutionIdentity(**base)


class _FakeRun:
    def __init__(self, result: dict[str, Any] | None, run_id: str = "prov-run-1") -> None:
        self.workflow_run_id = run_id
        self._result = result

    async def aio_result(self) -> dict[str, Any] | None:
        return self._result


class _FakeWorkflow:
    def __init__(self, run: _FakeRun | None, boom: bool = False) -> None:
        self._run = run
        self._boom = boom
        self.received: dict[str, Any] | None = None

    async def aio_run_no_wait(self, input: dict[str, Any]) -> _FakeRun:
        self.received = input
        if self._boom:
            raise RuntimeError("provider refused")
        assert self._run is not None
        return self._run


class _FakeRuns:
    def __init__(self) -> None:
        self.cancelled: list[str] = []

    async def cancel(self, run_id: str) -> None:
        self.cancelled.append(run_id)


class _FakeClient:
    def __init__(self, workflow: _FakeWorkflow, runs: _FakeRuns) -> None:
        self._workflow = workflow
        self.runs = runs

    def workflow(self, *, name: str) -> _FakeWorkflow:
        assert name  # the adapter always addresses the task by name
        return self._workflow


def _request(staging: Path, **over: object) -> ExecutionRequest:
    return ExecutionRequest(
        identity=_identity(**over),
        operation="creative_render",
        payload={"command": "edit", "operation": "trim"},
        staging_dir=str(staging),
    )


@pytest.mark.asyncio
async def test_happy_path_returns_a_claim_not_a_commit(tmp_path: Path) -> None:
    staging = tmp_path / "stage"
    staging.mkdir()
    artifact = staging / "output.mp4"
    artifact.write_bytes(b"bytes")
    raw = {
        "ok": True,
        "witness": {"success": True},
        "provider_attempt": 1,
        "staged_artifact_path": str(artifact),
        "artifact_sha256": "deadbeef",
    }
    client = _FakeClient(_FakeWorkflow(_FakeRun(raw)), _FakeRuns())
    adapter = HatchetExecutionAdapter(base_staging_dir=str(tmp_path), client=client)

    result = await adapter.execute(_request(staging))
    assert isinstance(result, ExecutionResult)
    assert result.is_success_claim
    assert result.state is OutcomeState.SUCCEEDED
    assert result.artifact_sha256 == "deadbeef"


@pytest.mark.asyncio
async def test_adapter_stamps_identity_and_uses_per_attempt_staging(tmp_path: Path) -> None:
    staging = tmp_path / "stage"
    staging.mkdir()
    workflow = _FakeWorkflow(_FakeRun({"ok": False}))
    adapter = HatchetExecutionAdapter(
        base_staging_dir=str(tmp_path), client=_FakeClient(workflow, _FakeRuns())
    )
    req = _request(staging)
    await adapter.execute(req)

    assert workflow.received is not None
    meta = workflow.received["metadata"]
    assert meta["fencing_token"] == 1
    assert meta["backend"] == "hatchet"
    assert meta["idempotency_key"] == "idem-1"
    # The provider is pointed at a staging dir, never a final artifact path.
    assert "staging" in workflow.received["staging_dir"]


@pytest.mark.asyncio
async def test_provider_retry_keeps_one_nexus_identity(tmp_path: Path) -> None:
    staging = tmp_path / "stage"
    staging.mkdir()
    raw = {"ok": True, "witness": {"success": True}, "provider_attempt": 3}
    adapter = HatchetExecutionAdapter(
        base_staging_dir=str(tmp_path),
        client=_FakeClient(_FakeWorkflow(_FakeRun(raw)), _FakeRuns()),
    )
    result = await adapter.execute(_request(staging, fencing_token=7))
    assert isinstance(result, ExecutionResult)
    assert result.attempt.provider_attempt == 3  # provider retries are metadata
    assert result.identity.fencing_token == 7  # ONE Nexus fencing identity


@pytest.mark.asyncio
async def test_missing_result_is_unknown_fail_closed(tmp_path: Path) -> None:
    staging = tmp_path / "stage"
    staging.mkdir()
    adapter = HatchetExecutionAdapter(
        base_staging_dir=str(tmp_path),
        client=_FakeClient(_FakeWorkflow(_FakeRun(None)), _FakeRuns()),
    )
    verdict = await adapter.execute(_request(staging))
    assert isinstance(verdict, ExecutionFailure)
    assert verdict.state is OutcomeState.UNKNOWN
    assert verdict.retryable is True


@pytest.mark.asyncio
async def test_staged_path_escape_is_refused(tmp_path: Path) -> None:
    staging = tmp_path / "stage"
    staging.mkdir()
    escape = tmp_path / "final.mp4"
    escape.write_bytes(b"x")
    raw = {
        "ok": True,
        "witness": {"success": True},
        "staged_artifact_path": str(escape),
    }
    adapter = HatchetExecutionAdapter(
        base_staging_dir=str(tmp_path),
        client=_FakeClient(_FakeWorkflow(_FakeRun(raw)), _FakeRuns()),
    )
    verdict = await adapter.execute(_request(staging))
    assert isinstance(verdict, ExecutionFailure)
    assert verdict.code == "staged_path_escape"


def test_reconcile_refuses_stale_identity(tmp_path: Path) -> None:
    staging = tmp_path / "stage"
    staging.mkdir()
    req = _request(staging, fencing_token=2)
    superseded = _identity(fencing_token=1)
    verdict = reconcile_provider_output(superseded, {"ok": True, "witness": {"success": True}}, req)
    # The result carries the superseded identity; the caller expects token 2.
    from nexus_ai_agent.integrations.execution import is_stale

    assert is_stale(req.identity, verdict)


@pytest.mark.asyncio
async def test_trigger_failure_is_a_typed_provider_error(tmp_path: Path) -> None:
    staging = tmp_path / "stage"
    staging.mkdir()
    adapter = HatchetExecutionAdapter(
        base_staging_dir=str(tmp_path),
        client=_FakeClient(_FakeWorkflow(None, boom=True), _FakeRuns()),
    )
    with pytest.raises(HatchetTriggerError):
        await adapter.execute(_request(staging))


@pytest.mark.asyncio
async def test_cancel_is_best_effort_and_propagated(tmp_path: Path) -> None:
    staging = tmp_path / "stage"
    staging.mkdir()
    runs = _FakeRuns()
    adapter = HatchetExecutionAdapter(
        base_staging_dir=str(tmp_path),
        client=_FakeClient(_FakeWorkflow(_FakeRun({"ok": True})), runs),
    )
    identity = _identity()
    assert await adapter.cancel(identity) is False  # nothing triggered yet
    await adapter.execute(_request(staging))
    assert await adapter.cancel(identity) is True
    assert runs.cancelled == ["prov-run-1"]
