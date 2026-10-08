"""Hatchet workflow that runs a CANONICAL Nexus handler and returns a witness.

This file is the *only* place that touches Hatchet's workflow/task API. It does
not decide anything, does not verify, does not publish and holds no Nexus
authority: it forwards the request's canonical payload to the matching
repository handler (``worker.default_job_handlers``) and returns the raw
result. Nexus verifies the artifact and performs the fenced commit afterwards.

The workflow is built from a client (``build_workflow``) rather than declared at
import time, so importing this module never needs a live engine or a token.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from nexus_ai_agent.integrations.execution.contract import BackendKind

from ._sdk import sdk

WORKFLOW_NAME = "nexus-execution-fabric"
STEP_NAME = "run-canonical"


class FabricInput(BaseModel):
    """The workflow's input shape: a canonical request plus its metadata."""

    operation: str
    payload: dict[str, Any] = Field(default_factory=dict)
    staging_dir: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


async def _run_canonical(operation: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Dispatch to the canonical repo handler for this operation (reuse only)."""
    from nexus_ai_agent.worker import default_job_handlers

    handlers = default_job_handlers()
    if operation not in handlers:
        return {"success": False, "error_code": "unsupported_operation", "error_detail": operation}
    return await handlers[operation](dict(payload))


#: Canonical handlers name their measured artifact differently; the fabric
#: normalizes them to (path, sha256) without re-measuring or re-validating.
_ARTIFACT_PATH_KEYS = ("artifact_path", "output_path", "path")
_ARTIFACT_SHA_KEYS = ("sha256", "content_sha256", "artifact_sha256")


def _artifact_fields(witness: dict[str, Any]) -> tuple[str | None, str | None]:
    path = next((witness[k] for k in _ARTIFACT_PATH_KEYS if witness.get(k)), None)
    sha = next((witness[k] for k in _ARTIFACT_SHA_KEYS if witness.get(k)), None)
    return (str(path) if path else None, str(sha) if sha else None)


def _is_success(witness: dict[str, Any], path: str | None, sha: str | None) -> bool:
    """Canonical handlers signal success differently: honour both shapes.

    ``creative_render`` returns an explicit ``success`` flag; ``story`` returns
    its measured artifact fields with no flag. Absence of ``error_code`` plus a
    present artifact is success; an ``error_code`` always fails closed.
    """
    if "success" in witness:
        return bool(witness["success"])
    if witness.get("error_code"):
        return False
    return bool(path or sha)


def build_workflow(client: Any) -> Any:  # SDK Workflow type is optional
    """Build the workflow for a given client (the worker registers this instance)."""
    sdk()  # fail with a typed error if the optional extra is absent
    workflow = client.workflow(name=WORKFLOW_NAME, input_validator=FabricInput)
    task = workflow.task

    @task(name=STEP_NAME, retries=3)
    async def run_canonical(input: FabricInput, ctx: Any) -> dict[str, Any]:
        witness = await _run_canonical(input.operation, input.payload)
        staged_path, sha = _artifact_fields(witness)
        return {
            "ok": _is_success(witness, staged_path, sha),
            "witness": witness,
            "backend": BackendKind.HATCHET.value,
            "request_id": input.metadata.get("request_id"),
            "job_id": input.metadata.get("job_id"),
            "try_id": input.metadata.get("try_id"),
            "fencing_token": input.metadata.get("fencing_token"),
            "provider_attempt": int(getattr(ctx, "attempt_number", 1) or 1),
            "staged_artifact_path": staged_path,
            "artifact_sha256": sha,
        }

    return workflow


def trigger_payload(step_output: dict[str, Any]) -> dict[str, Any]:
    """Hatchet returns ``{step_name: output}``; unwrap it to the witness."""
    return step_output.get(STEP_NAME, step_output)


__all__ = [
    "STEP_NAME",
    "WORKFLOW_NAME",
    "FabricInput",
    "build_workflow",
    "trigger_payload",
]
