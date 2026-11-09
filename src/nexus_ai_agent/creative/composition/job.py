"""The canonical queue adapter for the composition runtime.

This module is the composition layer's **only** seam with the canonical job
queue.  The worker it exposes is registered in
:func:`nexus_ai_agent.worker.default_job_handlers` under the job type
``creative_composition``, so a composition is dispatched by the existing
:class:`~nexus_ai_agent.adapters.in_process_job_queue.InProcessJobQueue` and
executed by the existing worker registry — the composition layer builds no
queue, no bus and no authority of its own.

What the worker does, in order:

1. validates the queue row (a durable row is an untrusted structure);
2. guards the workspace and every staged input against the canonical creative
   root (containment, symlink refusal, digest re-measurement);
3. rebuilds the plan and **re-compiles it against the live registry**, so a
   plan that was compiled against a different registry is refused rather than
   executed against whatever the registry happens to offer now;
4. executes the plan through the canonical command bus;
5. returns the canonical artifact-claim dialect, so the queue's registered
   verifier can re-measure the produced artifact independently.
"""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .compiler import CompositionCompiler, CompositionError
from .contracts import CompositionPlan, ExpectedOutput, ExternalInput, PlanNode, PortBinding
from .executor import CompositionExecutionError, CompositionExecutor
from .proof import ProofCarryingArtifact

#: The job type the composition worker registers.
COMPOSITION_JOB_TYPE = "creative_composition"

#: The plan version assumed when a row does not state one.  It is a version
#: number, not a verdict: an absent ``plan_version`` means "the first version
#: of the plan shape", not "the plan is fine".
DEFAULT_PLAN_VERSION = 1

#: The typed failure codes this worker surfaces.
ERROR_CODES: frozenset[str] = frozenset(
    {
        "invalid_request",
        "unknown_capability",
        "policy_refused",
        "execution_failed",
        "verification_failed",
    }
)


class CompositionJobError(ValueError):
    """A typed, user-visible composition job failure."""

    def __init__(self, code: str, detail: str = "") -> None:
        if code not in ERROR_CODES:
            raise ValueError(f"unknown composition error code: {code!r}")
        super().__init__(f"[{code}] {detail}".strip())
        self.code = code
        self.detail = detail


class StagedInput(BaseModel):
    """One staged external input of a composition row."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=1024)
    sha256: str = Field(min_length=64, max_length=71)


class ActorClaim(BaseModel):
    """The identity the row was submitted under.

    The worker never invents an identity: it binds the grant to the actor the
    request names, so a row cannot silently self-grant an anonymous service
    principal.
    """

    model_config = ConfigDict(extra="forbid")

    #: The canonical actor kinds.  A row may not invent a fourth one: the claim
    #: has to name a principal the bus knows how to check.
    kind: Literal["user", "service", "agent"]
    actor_id: str = Field(min_length=1, max_length=128)


class CompositionJobPayload(BaseModel):
    """The trusty schema of the composition queue row.

    ``extra="forbid"`` rejects a row that tries to widen its own policy: the
    only knobs it carries are the ones this worker re-checks itself.

    Two fields exist so the worker cannot grant more than it was asked for:

    * ``actor`` — the principal the request was made under.  It is **required**:
      a row without an identity is refused, because an anonymous row would
      otherwise be executed under a self-granted service identity.
    * ``confirmed`` — the requester's explicit confirmation for level-C
      operations.  The worker never fabricates it; without it a level-C node is
      refused before any media work happens.
    """

    model_config = ConfigDict(extra="forbid")

    #: The composition plan (a JSON object) — compiled against the live registry.
    plan: dict[str, Any]
    #: The staged external inputs, by the plan's external-input name.
    external_inputs: dict[str, StagedInput] = Field(default_factory=dict)
    #: The guarded workspace the row was admitted against.
    workspace_dir: str = Field(min_length=1, max_length=1024)
    #: The durable idempotency key of the row.
    idempotency_key: str = Field(min_length=1, max_length=128)
    #: The principal the request was submitted under.
    actor: ActorClaim
    #: The requester's explicit confirmation for level-C operations.
    confirmed: bool = False
    #: Whether the requester intends a commercial use of the output.
    commercial_use: bool = False


# ---------------------------------------------------------------------------
# workspace and input guards
# ---------------------------------------------------------------------------


def trusted_workspace(raw: str) -> Path:
    """Resolve the job's workspace against the canonical creative root.

    The rule is the canonical one (``creative/render_jobs.py``): the root is
    validated *before* the workspace is canonicalized, the workspace node
    itself must not be a symlink, the workspace must be inside the root, and
    its name must carry the reserved prefix.  The composition layer does not
    get a second, laxer rule.
    """
    from nexus_ai_agent.creative.render_jobs import (
        WORKSPACE_PREFIX,
        trusted_creative_root,
    )

    root = trusted_creative_root()
    raw_path = Path(raw)
    try:
        info: os.stat_result | None = os.lstat(raw_path)
    except FileNotFoundError:
        info = None
    except OSError as exc:
        raise CompositionJobError("invalid_request", f"workspace unreadable: {raw}") from exc
    if info is not None and stat.S_ISLNK(info.st_mode):
        raise CompositionJobError("invalid_request", f"workspace is a symlink: {raw}")
    try:
        workspace = Path(raw).resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise CompositionJobError("invalid_request", f"workspace resolution failed: {exc}") from exc
    try:
        workspace.relative_to(root)
    except ValueError as exc:
        raise CompositionJobError("invalid_request", "workspace outside creative temp dir") from exc
    if not workspace.name.startswith(WORKSPACE_PREFIX):
        raise CompositionJobError("invalid_request", "workspace name missing reserved prefix")
    return workspace


def guarded_input(path: str, workspace: Path, expected_sha256: str) -> Path:
    """Resolve a staged input, refusing a symlink, an escape or a changed file."""
    raw = Path(path)
    try:
        info = os.lstat(raw)
    except OSError as exc:
        raise CompositionJobError(
            "invalid_request", f"staged input is not readable: {path}"
        ) from exc
    if stat.S_ISLNK(info.st_mode):
        raise CompositionJobError("invalid_request", f"staged input is a symlink: {path}")
    if not stat.S_ISREG(info.st_mode):
        raise CompositionJobError("invalid_request", f"staged input is not a regular file: {path}")
    candidate = raw.resolve(strict=False)
    try:
        candidate.relative_to(workspace)
    except ValueError as exc:
        raise CompositionJobError("invalid_request", "input path escapes its workspace") from exc
    if not candidate.is_file():
        raise CompositionJobError("invalid_request", f"staged input is missing: {path}")
    digest = hashlib.sha256()
    with candidate.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    measured = digest.hexdigest()
    expected = (
        expected_sha256.split(":", 1)[1]
        if expected_sha256.startswith("sha256:")
        else expected_sha256
    )
    if measured != expected:
        raise CompositionJobError(
            "invalid_request",
            f"staged input digest mismatch: expected {expected}, measured {measured}",
        )
    return candidate


# ---------------------------------------------------------------------------
# plan decoding
# ---------------------------------------------------------------------------


def _bindings_from(item: dict[str, Any]) -> list[PortBinding]:
    """Decode one node's bindings.

    Two shapes are accepted, and both are *closed*:

    * the **wire** shape — ``{"source": {"external": "src"}}`` or
      ``{"source": {"node_output": ["resize", "resized"]}}``, keyed by the input
      port name;
    * the **model** shape — a list of
      ``{"input_port", "source", "external", "node_id", "output_port"}`` objects,
      which is what ``CompositionPlan.model_dump`` produces.

    Accepting the model shape is what makes a plan **round-trip**: a plan that
    was dumped and re-read must compile to the same fingerprint.
    """
    raw = item.get("bindings") or ()
    if isinstance(raw, dict):
        decoded: list[PortBinding] = []
        for port, binding in raw.items():
            if not isinstance(binding, dict):
                raise CompositionJobError("invalid_request", f"binding {port!r} is not an object")
            unknown = set(binding) - {"external", "node_output"}
            if unknown:
                raise CompositionJobError(
                    "invalid_request",
                    f"binding {port!r} carries unknown keys {sorted(unknown)}",
                )
            if "external" in binding and "node_output" in binding:
                raise CompositionJobError(
                    "invalid_request",
                    f"binding {port!r} names both external and node_output; "
                    "exactly one source must be given",
                )
            if "external" in binding:
                if not isinstance(binding["external"], str) or not binding["external"]:
                    raise CompositionJobError(
                        "invalid_request", f"binding {port!r} names an empty external input"
                    )
                decoded.append(
                    PortBinding(
                        input_port=port,
                        source="external",
                        external=binding["external"],
                    )
                )
            elif "node_output" in binding:
                pair = binding["node_output"]
                if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                    raise CompositionJobError(
                        "invalid_request",
                        f"binding {port!r} names a malformed node_output pair",
                    )
                decoded.append(
                    PortBinding(
                        input_port=port,
                        source="node_output",
                        node_id=pair[0],
                        output_port=pair[1],
                    )
                )
            else:
                raise CompositionJobError(
                    "invalid_request",
                    f"binding {port!r} names neither external nor node_output",
                )
        return decoded
    if isinstance(raw, (list, tuple)):
        decoded = []
        for binding in raw:
            if not isinstance(binding, dict):
                raise CompositionJobError("invalid_request", "a binding is not an object")
            # The model shape goes through the canonical model itself, so
            # ``extra="forbid"`` applies: an unknown key is refused here exactly
            # as it is in the wire shape, rather than being silently dropped.
            try:
                decoded.append(PortBinding.model_validate(binding))
            except ValidationError as exc:
                raise CompositionJobError(
                    "invalid_request", f"malformed binding {sorted(binding)}: {exc}"
                ) from exc
        return decoded
    raise CompositionJobError("invalid_request", "bindings must be an object or a list")


def plan_from_dict(raw: dict[str, Any]) -> CompositionPlan:
    """Decode the row's plan into a :class:`CompositionPlan`."""
    if not isinstance(raw, dict):
        raise CompositionJobError("invalid_request", "plan must be an object")
    try:
        nodes = []
        for item in raw.get("nodes", []):
            nodes.append(
                PlanNode(
                    node_id=item["node_id"],
                    operation=item["operation"],
                    capability_id=item["capability_id"],
                    capability_version=item["capability_version"],
                    operation_schema_version=item["operation_schema_version"],
                    params=item.get("params", {}),
                    bindings=tuple(_bindings_from(item)),
                )
            )
        external_inputs = [
            ExternalInput(
                name=item["name"],
                media_kind=item["media_kind"],
                contract=item.get("contract", {"kind": item["media_kind"]}),
            )
            for item in raw.get("external_inputs", [])
        ]
        expected_outputs = [
            ExpectedOutput(
                name=item["name"],
                node_id=item.get("node_id", item.get("node", "")),
                output_port=item["output_port"],
            )
            for item in raw.get("expected_outputs", [])
        ]
        return CompositionPlan(
            plan_version=raw.get("plan_version", DEFAULT_PLAN_VERSION),
            nodes=tuple(nodes),
            external_inputs=tuple(external_inputs),
            expected_outputs=tuple(expected_outputs),
            registry_fingerprint=raw.get("registry_fingerprint", ""),
        )
    except CompositionJobError:
        raise
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        raise CompositionJobError("invalid_request", f"malformed composition plan: {exc}") from exc


def plan_to_dict(plan: CompositionPlan) -> dict[str, Any]:
    """Encode a plan into its durable **wire** form.

    The wire form is the one the operator guide documents: the bindings are
    keyed by their input port name, so a plan is readable without knowing the
    model's field names.  ``plan_from_dict`` accepts both this form and the
    model's own dump, so a plan round-trips either way.
    """
    return {
        "plan_version": plan.plan_version,
        "nodes": [
            {
                "node_id": node.node_id,
                "operation": node.operation,
                "capability_id": node.capability_id,
                "capability_version": node.capability_version,
                "operation_schema_version": node.operation_schema_version,
                "params": dict(node.params),
                "bindings": {
                    binding.input_port: (
                        {"external": binding.external}
                        if binding.source == "external"
                        else {"node_output": [binding.node_id, binding.output_port]}
                    )
                    for binding in node.bindings
                },
            }
            for node in plan.nodes
        ],
        "external_inputs": [
            {
                "name": item.name,
                "media_kind": item.media_kind,
                "contract": item.contract.model_dump(mode="json"),
            }
            for item in plan.external_inputs
        ],
        "expected_outputs": [
            {
                "name": item.name,
                "node": item.node_id,
                "output_port": item.output_port,
            }
            for item in plan.expected_outputs
        ],
        "registry_fingerprint": plan.registry_fingerprint,
    }


# ---------------------------------------------------------------------------
# the worker
# ---------------------------------------------------------------------------


async def composition_render_job(payload: dict[str, Any]) -> dict[str, Any]:
    """Execute one composition through the canonical runtime (queue worker entry).

    Returns the canonical artifact-claim dialect on success and a typed
    failure block otherwise — the same shape ``creative_render_job`` uses, so
    the queue's registered verifier can re-measure the artifact.
    """
    try:
        data = CompositionJobPayload.model_validate(payload)
    except Exception as exc:  # noqa: BLE001 - a pydantic validation error
        return {
            "success": False,
            "error_code": "invalid_request",
            "error_detail": str(exc),
        }

    try:
        workspace = trusted_workspace(data.workspace_dir)
    except CompositionJobError as exc:
        return {"success": False, "error_code": exc.code, "error_detail": exc.detail}

    inputs: dict[str, Path] = {}
    for name, staged in data.external_inputs.items():
        try:
            inputs[name] = guarded_input(staged.path, workspace, staged.sha256)
        except CompositionJobError as exc:
            return {"success": False, "error_code": exc.code, "error_detail": exc.detail}

    try:
        plan = plan_from_dict(data.plan)
    except CompositionJobError as exc:
        return {"success": False, "error_code": exc.code, "error_detail": exc.detail}

    compiler = CompositionCompiler()
    try:
        compiled = compiler.compile(plan)
    except CompositionError as exc:
        code = "unknown_capability" if exc.code == "unknown_operation" else "policy_refused"
        return {"success": False, "error_code": code, "error_detail": str(exc)}

    from nexus_ai_agent.creative.studio.models import ActorIdentity

    executor = CompositionExecutor(
        actor=ActorIdentity(kind=data.actor.kind, actor_id=data.actor.actor_id),
        confirmed=data.confirmed,
    )
    try:
        result = executor.execute(compiled, external_inputs=inputs, workspace=workspace)
    except (CompositionExecutionError, CompositionError) as exc:
        return {"success": False, "error_code": "execution_failed", "error_detail": str(exc)}
    except Exception as exc:  # noqa: BLE001 - an environment failure is operator-visible
        return {
            "success": False,
            "error_code": "execution_failed",
            "error_detail": f"{type(exc).__name__}: {exc}",
        }

    primary = _primary_artifact(result.artifacts)
    if primary is None:
        return {
            "success": False,
            "error_code": "execution_failed",
            "error_detail": "the composition produced no artifact",
        }
    artifact: ProofCarryingArtifact = primary
    if not artifact.verified:
        return {
            "success": False,
            "error_code": "verification_failed",
            "error_detail": "; ".join([*artifact.integrity.findings, *artifact.semantic.findings]),
        }
    return {
        "success": True,
        "artifact_path": artifact.output_path,
        "artifact_kind": "video" if artifact.output_path.endswith(".mp4") else "image",
        "sha256": artifact.output_hash,
        "size_bytes": artifact.output_size_bytes,
        "duration_us": artifact.semantic.measurements.get("duration_us", 0),
        "operation": artifact.operation_manifest.get("nodes", [{}])[-1].get("operation", ""),
        "output_asset_id": artifact.receipts[-1].asset_id if artifact.receipts else "",
        "plan_fingerprint": artifact.plan_fingerprint,
        "registry_fingerprint": artifact.registry_fingerprint,
        "integrity_verified": artifact.integrity.verified,
        "semantic_verified": artifact.semantic.verified,
        "semantic_measurements": dict(artifact.semantic.measurements),
        "media_contract": dict(artifact.media_contract),
        "lineage": list(artifact.lineage),
        "license_ledger": dict(artifact.license_ledger),
        "evidence": artifact.as_dict(),
        "state_revision": result.state_revision,
        "state_hash": result.state_hash,
        "duration_seconds": result.duration_seconds,
        "resource_estimate": result.estimate.model_dump(mode="json"),
    }


def _primary_artifact(artifacts: dict[str, ProofCarryingArtifact]) -> ProofCarryingArtifact | None:
    """The artifact the row exposes: the last expected output, else the first."""
    if not artifacts:
        return None
    return list(artifacts.values())[-1]


__all__ = [
    "COMPOSITION_JOB_TYPE",
    "ERROR_CODES",
    "ActorClaim",
    "CompositionJobError",
    "CompositionJobPayload",
    "StagedInput",
    "composition_render_job",
    "guarded_input",
    "plan_from_dict",
    "plan_to_dict",
    "trusted_workspace",
]
