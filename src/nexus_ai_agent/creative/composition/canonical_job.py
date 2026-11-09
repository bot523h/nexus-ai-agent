"""Canonical job adapter for the composition layer (Gate 2 / Gate 8).

This module is the composition layer's **only** seam with the canonical job
queue. It is a plain async worker function shaped exactly like
``nexus_ai_agent.creative.render_jobs.creative_render_job`` so the existing
``JobQueuePort`` and worker registration can run a composition **without the
composition layer constructing any queue, bus or authority of its own**.

What it deliberately does *not* do (pinned by
``tests/architecture/test_composition_boundary.py``):

* it builds no ``CommandBus`` and no ``JobQueue`` implementation;
* it does not import an execution authority -- it is a *callee* of the queue;
* it does not carry a lifecycle opt-in inside the payload for anything other
  than the composition layer's own experimental gate, and that gate fails
  closed.

The adapter validates the incoming payload (a queue row is an untrusted
structure), builds the composition graph, validates the capability catalogue
against the live registry, executes through the :class:`CompositionRuntime`
(which routes FFmpeg through the canonical allow-list and verifies the output
both for integrity and semantics), and returns measured artifact facts in the
same ``{"success": ...}`` shape the render worker uses.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .catalog import builtin_capabilities
from .canonical import CanonicalRegistryError, build_snapshot
from .compiler import CompositionError, CompositionGraph, CompositionNode, InputBinding
from .runtime import CompositionRuntime, RuntimePolicy
from .spec import MediaType, ResourceRequirements

COMPOSITION_JOB_TYPE = "creative_composition"

ERROR_CODES: frozenset[str] = frozenset(
    {"invalid_request", "unknown_capability", "policy_refused", "execution_failed"}
)


class CompositionJobPayload(BaseModel):
    """Trusty schema for the composition queue row.

    ``extra="forbid"`` rejects a row that tries to widen its own policy: the
    only knobs it carries are the ones this adapter re-checks against the
    runtime policy.
    """

    model_config = ConfigDict(extra="forbid")

    composition: dict[str, Any]
    external_inputs: dict[str, str] = Field(default_factory=dict)
    workspace_dir: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    commercial_use: bool = False
    allow_network: bool = False
    allow_experimental: bool = False
    quota: dict[str, Any] | None = None


class CompositionJobError(ValueError):
    """A typed, user-visible composition job failure."""

    def __init__(self, code: str, detail: str = "") -> None:
        if code not in ERROR_CODES:
            raise ValueError(f"unknown composition error code: {code}")
        super().__init__(f"[{code}] {detail}".strip())
        self.code = code
        self.detail = detail


def graph_from_dict(spec: dict[str, Any]) -> CompositionGraph:
    """Build a :class:`CompositionGraph` from its JSON declaration.

    Raises :class:`CompositionJobError` (``invalid_request``) on a malformed
    declaration so a bad row never reaches the runtime.
    """
    if not isinstance(spec, dict):
        raise CompositionJobError("invalid_request", "composition must be an object")
    graph = CompositionGraph()
    try:
        for item in spec.get("external_inputs", []):
            graph.add_external_input(item["name"], MediaType.model_validate(item["media_type"]))
        for node in spec.get("nodes", []):
            bindings: dict[str, InputBinding] = {}
            for input_name, binding in (node.get("bindings") or {}).items():
                if "external" in binding:
                    bindings[input_name] = InputBinding(input_name, external=binding["external"])
                elif "node_output" in binding:
                    node_output = binding["node_output"]
                    bindings[input_name] = InputBinding(
                        input_name, node_output=(node_output[0], node_output[1])
                    )
                else:
                    raise CompositionJobError(
                        "invalid_request", f"binding {input_name!r} names neither external nor node_output"
                    )
            graph.add_node(
                CompositionNode(
                    node["id"],
                    node["capability"],
                    params=node.get("params", {}),
                    bindings=bindings,
                )
            )
        for out in spec.get("expected_outputs", []):
            graph.add_expected_output(out["name"], out["node"], out["output"])
    except CompositionJobError:
        raise
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        raise CompositionJobError("invalid_request", f"malformed composition: {exc}") from exc
    return graph


def _guarded_output_dir(workspace_dir: str) -> Path:
    workspace = Path(workspace_dir).resolve(strict=False)
    outputs = workspace / "composition"
    try:
        outputs.relative_to(workspace)
    except ValueError as exc:  # pragma: no cover - /composition is always inside
        raise CompositionJobError("invalid_request", "output dir escaped its workspace") from exc
    outputs.mkdir(parents=True, exist_ok=True)
    return outputs


async def composition_render_job(payload: dict[str, Any]) -> dict[str, Any]:
    """Run one composition through the composition runtime (queue worker entry).

    Returns ``{"success": True, ...}`` with measured artifact facts, or
    ``{"success": False, "error_code": <typed>, ...}`` for an expected,
    user-typed failure. Mirrors ``creative_render_job``'s contract.
    """
    try:
        data = CompositionJobPayload.model_validate(payload)
    except Exception as exc:  # noqa: BLE001 - pydantic validation error
        return {"success": False, "error_code": "invalid_request", "error_detail": str(exc)}

    try:
        graph = graph_from_dict(data.composition)
    except CompositionJobError as exc:
        return {"success": False, "error_code": exc.code, "error_detail": exc.detail}

    try:
        snapshot = build_snapshot(builtin_capabilities())
    except CanonicalRegistryError as exc:  # pragma: no cover - static catalogue
        return {"success": False, "error_code": "unknown_capability", "error_detail": str(exc)}

    quota = ResourceRequirements.model_validate(data.quota) if data.quota else ResourceRequirements(
        cpu_threads=8, ram_mb=8192, disk_mb=8192, network=data.allow_network
    )
    policy = RuntimePolicy(
        allow_network=data.allow_network,
        quota=quota,
        allow_experimental=data.allow_experimental,
    )
    runtime = CompositionRuntime(snapshot, policy=policy)

    outputs_dir = _guarded_output_dir(data.workspace_dir)
    try:
        result = await asyncio.to_thread(
            runtime.execute,
            graph,
            data.external_inputs,
            output_dir=outputs_dir,
            commercial_use=data.commercial_use,
        )
    except (RuntimeError, CompositionError) as exc:
        return {"success": False, "error_code": "policy_refused", "error_detail": str(exc)}
    except Exception as exc:  # noqa: BLE001 - unexpected environment failure is operator-visible
        return {"success": False, "error_code": "execution_failed", "error_detail": f"{type(exc).__name__}: {exc}"}

    primary = next(iter(result.outputs))
    artifact = result.artifacts[primary]
    return {
        "success": True,
        "artifact_path": artifact.output_path,
        "artifact_id": artifact.artifact_id,
        "sha256": artifact.output_hash,
        "size_bytes": Path(artifact.output_path).stat().st_size,
        "outputs": dict(result.outputs),
        "derived_capability_id": result.derived_spec.capability_id,
        "live_registry_fingerprint": result.live_registry_fingerprint,
        "integrity_verified": artifact.verification.verified,
        "semantic_verified": bool(artifact.semantic and artifact.semantic.verified),
        "resource_estimate": result.resource_estimate.to_dict() if result.resource_estimate else None,
        "workspace_dir": data.workspace_dir,
    }


__all__ = [
    "COMPOSITION_JOB_TYPE",
    "CompositionJobError",
    "CompositionJobPayload",
    "composition_render_job",
    "graph_from_dict",
]
