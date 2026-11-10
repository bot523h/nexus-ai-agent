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

import asyncio
import hashlib
import os
import stat
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .compiler import CompositionCompiler, CompositionError
from .contracts import CompositionPlan, ExpectedOutput, ExternalInput, PlanNode, PortBinding
from .executor import CompositionExecutionError, CompositionExecutor, ExecutionResult
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
        "revision_unavailable",
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
    #: The **durable** project the request belongs to.  When present the worker
    #: resolves the requester's authority through the trusted application
    #: directory and refuses the row unless the actor is a member of that
    #: project; when absent the row runs on an ephemeral project the worker
    #: creates, and the result says so (``authority``).
    project_id: str | None = Field(default=None, min_length=1, max_length=128)
    #: The immutable project revision this row was admitted against.  When
    #: present, the worker executes EXACTLY this revision (never the project
    #: head, which can move after enqueue) and the row is only accepted with a
    #: complete pin.  This mirrors ``CreativeRenderPayload``'s revision pin.
    revision_id: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    #: The pinned revision's canonical ``state_hash``.  The pin is complete only
    #: when both the id and its hash are present.
    revision_hash: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _revision_pin_is_complete(self) -> CompositionJobPayload:
        """A revision pin requires the project it belongs to and its hash."""
        if self.revision_id is not None and (self.project_id is None or self.revision_hash is None):
            raise ValueError(
                "revision_id requires both project_id and revision_hash (a complete pin)"
            )
        return self

    @property
    def has_revision_pin(self) -> bool:
        """Whether this row carries a complete revision pin."""
        return (
            self.revision_id is not None
            and self.project_id is not None
            and self.revision_hash is not None
        )


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
    # Bound the read *before* it happens: hashing a hostile terabyte is already
    # a denial of service, so the size guard cannot wait for the engine.
    from nexus_ai_agent.creative.composition.engine import MAX_INPUT_BYTES

    if info.st_size > MAX_INPUT_BYTES:
        raise CompositionJobError(
            "invalid_request",
            f"staged input is {info.st_size} bytes; the limit is {MAX_INPUT_BYTES}",
        )
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


#: The only keys a submitted (durable) plan may carry.  A wire plan is a
#: *closed* shape: a misspelled or unsupported key is refused, never dropped,
#: because dropping it would execute a different plan than the one submitted.
PLAN_KEYS = frozenset(
    {"plan_version", "nodes", "external_inputs", "expected_outputs", "registry_fingerprint"}
)
NODE_KEYS = frozenset(
    {
        "node_id",
        "operation",
        "capability_id",
        "capability_version",
        "operation_schema_version",
        "params",
        "bindings",
    }
)
EXTERNAL_INPUT_KEYS = frozenset({"name", "media_kind", "contract"})
EXPECTED_OUTPUT_KEYS = frozenset({"name", "node", "node_id", "output_port"})


def _closed(item: dict[str, Any], allowed: frozenset[str], what: str) -> None:
    """Refuse an object that carries a key outside *allowed*."""
    if not isinstance(item, dict):
        raise CompositionJobError("invalid_request", f"{what} is not an object")
    unknown = set(item) - allowed
    if unknown:
        raise CompositionJobError(
            "invalid_request", f"{what} carries unknown keys {sorted(unknown)}"
        )


def plan_from_dict(raw: dict[str, Any]) -> CompositionPlan:
    """Decode the row's plan into a :class:`CompositionPlan`.

    The wire shape is **closed** (see :data:`PLAN_KEYS`) and a submitted plan
    must **pin the registry fingerprint it was authored against**: the row is
    durable and may execute later, so an unpinned plan could silently run
    against a registry that changed in the meantime.
    """
    if not isinstance(raw, dict):
        raise CompositionJobError("invalid_request", "plan must be an object")
    _closed(raw, PLAN_KEYS, "plan")
    if not raw.get("registry_fingerprint"):
        raise CompositionJobError(
            "invalid_request",
            "a submitted plan must pin registry_fingerprint; "
            "compile against the live registry and submit the pinned plan",
        )
    try:
        nodes = []
        for item in raw.get("nodes", []):
            _closed(item, NODE_KEYS, "plan node")
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
        for item in raw.get("external_inputs", []):
            _closed(item, EXTERNAL_INPUT_KEYS, "external input")
        external_inputs = [
            ExternalInput(
                name=item["name"],
                media_kind=item["media_kind"],
                contract=item.get("contract", {"kind": item["media_kind"]}),
            )
            for item in raw.get("external_inputs", [])
        ]
        for item in raw.get("expected_outputs", []):
            _closed(item, EXPECTED_OUTPUT_KEYS, "expected output")
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
            registry_fingerprint=raw["registry_fingerprint"],
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
                "contract": (
                    item.contract.model_dump(mode="json")
                    if item.contract is not None
                    else {"kind": item.media_kind}
                ),
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


async def _resolve_pinned_project(data: CompositionJobPayload) -> Any | None:  # noqa: ANN401
    """Load the EXACT revision this row was admitted against, or refuse.

    A row that carries a revision pin must execute that immutable revision, not
    the project's current head: the head can move after enqueue, and a job must
    never silently run a different state than the one it was admitted against.
    The store re-validates the revision's bytes, and the pin's
    ``revision_hash`` must match the revision's canonical ``state_hash``.  Every
    failure is a typed refusal (``revision_unavailable``); there is no fallback.
    """
    if data.revision_id is None:
        return None
    if data.project_id is None or data.revision_hash is None:  # pragma: no cover - model guard
        raise CompositionJobError("revision_unavailable", "incomplete revision pin")
    from nexus_ai_agent.creative.project_state.models import (
        CrossProjectAccess,
        RevisionIntegrityError,
        RevisionNotFound,
    )
    from nexus_ai_agent.creative.project_state.store import SqlProjectRevisionStore

    store = SqlProjectRevisionStore()
    try:
        record = await store.load_revision(data.project_id, data.revision_id)
    except (RevisionNotFound, RevisionIntegrityError, CrossProjectAccess) as exc:
        raise CompositionJobError(
            "revision_unavailable", f"pinned revision cannot be loaded: {type(exc).__name__}"
        ) from exc
    if record.state_hash != data.revision_hash:
        raise CompositionJobError(
            "revision_unavailable", "pinned revision hash does not match the row's pin"
        )
    return record.to_project()


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

    actor = ActorIdentity(kind=data.actor.kind, actor_id=data.actor.actor_id)
    if data.project_id is not None:
        # The requester named a durable project: the *directory* decides whether
        # this actor may act on it.  A membership that does not exist is a
        # refusal, never a grant minted here.
        from nexus_ai_agent.adapters.sql_application_directory import SQLApplicationDirectory

        # When the row pins an immutable revision, execute EXACTLY that
        # revision — never the live head — so the state the plan was compiled
        # against is the state that runs.  An unloadable or mismatched pin is a
        # typed refusal, never a silent fall back to the head.
        try:
            pinned_project = await _resolve_pinned_project(data)
        except CompositionJobError as exc:
            return {"success": False, "error_code": exc.code, "error_detail": exc.detail}
        try:
            executor = CompositionExecutor(
                actor=actor,
                project=pinned_project,
                project_id=data.project_id,
                authorizer=SQLApplicationDirectory(),
                confirmed=data.confirmed,
            )
        except Exception as exc:  # noqa: BLE001 - AuthorizationError and friends
            return {
                "success": False,
                "error_code": "policy_refused",
                "error_detail": (
                    f"the requester has no authority over project {data.project_id!r}: "
                    f"{type(exc).__name__}: {exc}"
                ),
            }
    else:
        executor = CompositionExecutor(actor=actor, confirmed=data.confirmed)

    if data.commercial_use and plan.external_inputs:
        # The runtime can measure *what* it consumed but not the terms it
        # consumed it under, and an input asset's record is honestly UNKNOWN.
        # The commercial verdict therefore cannot be true for such a plan, so
        # the row is refused *before* any media work.
        return {
            "success": False,
            "error_code": "policy_refused",
            "error_detail": (
                "commercial use is not approved: the licence terms of the consumed "
                f"input(s) {sorted(item.name for item in plan.external_inputs)} are unknown"
            ),
            "authority": executor.authority_source,
        }
    try:
        result = await asyncio.to_thread(
            executor.execute,
            compiled,
            external_inputs=inputs,
            workspace=workspace,
            expected_digests={name: staged.sha256 for name, staged in data.external_inputs.items()},
        )
    except (CompositionExecutionError, CompositionError) as exc:
        return {"success": False, "error_code": "execution_failed", "error_detail": str(exc)}
    except Exception as exc:  # noqa: BLE001 - an environment failure is operator-visible
        return {
            "success": False,
            "error_code": "execution_failed",
            "error_detail": f"{type(exc).__name__}: {exc}",
        }

    if data.commercial_use and result.license_ledger.get("effective_allow_commercial") is not True:
        return {
            "success": False,
            "error_code": "policy_refused",
            "error_detail": (
                "commercial use is not approved: the measured ledger does not prove it "
                f"(unknown categories: {result.license_ledger.get('unknown_categories', [])})"
            ),
            "license_ledger": dict(result.license_ledger),
            "authority": executor.authority_source,
        }

    # Every expected output is claimed, verified and returned -- not only the
    # last one.  A plan that asked for two deliverables must not lose one.
    claims = _artifact_claims(result.artifacts)
    if not claims:
        return {
            "success": False,
            "error_code": "execution_failed",
            "error_detail": "the composition produced no artifact",
        }
    failed = [
        claim
        for claim in claims
        if not (claim["integrity_verified"] and claim["semantic_verified"])
    ]
    if failed:
        findings: list[str] = []
        for name in [claim["name"] for claim in failed]:
            failed_artifact = result.artifacts[name]
            findings.extend(failed_artifact.integrity.findings)
            findings.extend(failed_artifact.semantic.findings)
        return {
            "success": False,
            "error_code": "verification_failed",
            "error_detail": "; ".join(findings),
            "outputs": claims,
            "authority": executor.authority_source,
        }
    primary = claims[-1]
    primary_artifact: ProofCarryingArtifact = result.artifacts[primary["name"]]
    output = {
        "success": True,
        **primary,
        "outputs": claims,
        "authority": executor.authority_source,
        "semantic_measurements": dict(primary_artifact.semantic.measurements),
        "lineage": list(primary_artifact.lineage),
        "license_ledger": dict(primary_artifact.license_ledger),
        "evidence": primary_artifact.as_dict(),
        "state_revision": result.state_revision,
        "state_hash": result.state_hash,
        "duration_seconds": result.duration_seconds,
        "resource_estimate": result.estimate.model_dump(mode="json"),
    }
    if data.has_revision_pin:
        # A pinned row executed one immutable revision and produced artifacts
        # that must be durably bound to it (never to a head that can move).  The
        # *measured* content address of every claimed output is recorded as an
        # intent here; the queue's post-publication confirm hook commits the
        # binding — committing inside the worker would bind an artifact the
        # queue may still reject.
        try:
            output["artifact_bindings"] = _binding_intents(data, result)
        except CompositionExecutionError as exc:
            return {"success": False, "error_code": "execution_failed", "error_detail": str(exc)}
    return output


def _binding_intents(data: CompositionJobPayload, result: ExecutionResult) -> list[dict[str, Any]]:
    """The measured artifact→revision binding intents for a pinned row.

    One intent per claimed output, each carrying the *measured* content address
    of the artifact bytes — never a digest copied from the producer's report.
    An output whose bytes cannot be measured is a hard ``execution_failed``: a
    pinned row must never claim an artifact it cannot bind.
    """
    from nexus_ai_agent.creative.project_state.store import (
        content_address_of_file,
        is_content_address,
    )

    assert data.project_id is not None and data.revision_id is not None
    intents: list[dict[str, Any]] = []
    for name, artifact in result.artifacts.items():
        path = Path(artifact.output_path)
        try:
            measured = content_address_of_file(path)
        except OSError as exc:
            raise CompositionExecutionError(
                f"artifact {name!r} is unreadable for binding: {type(exc).__name__}"
            ) from exc
        if not is_content_address(measured):
            raise CompositionExecutionError(f"artifact {name!r} has no content address")
        intents.append(
            {
                "artifact_id": measured,
                "project_id": data.project_id,
                "revision_id": data.revision_id,
                "artifact_kind": "video" if path.suffix == ".mp4" else "image",
                "size_bytes": int(artifact.output_size_bytes),
            }
        )
    return intents


async def composition_render_binding_confirm(
    payload: dict[str, Any], result: dict[str, Any]
) -> None:
    """Queue confirm hook: durably commit the artifact→revision bindings.

    Runs after the queue independently verified and published the artifact and
    before the durable COMPLETED commit — the only point at which a binding may
    be written, so a rejected artifact never leaves one behind.  Each digest is
    re-measured from the *published* bytes, and every failure is a typed
    ``CompositionJobError`` so the queue fails the job closed.  Idempotent: a
    binding is content-addressed, so a retry is a no-op.
    """
    intents = result.get("artifact_bindings")
    if not isinstance(intents, list) or not intents:
        return  # unpinned (legacy) row: nothing to confirm
    from nexus_ai_agent.creative.project_state.store import (
        SqlArtifactBindingStore,
        content_address_of_file,
    )

    published = result.get("outputs")
    job_id = str(result.get("_queue_job_id") or "")
    store = SqlArtifactBindingStore()
    for intent in intents:
        if not isinstance(intent, dict):
            raise CompositionJobError("invalid_request", "malformed artifact binding intent")
        artifact_path = _published_path_for(str(intent.get("artifact_id") or ""), published)
        if artifact_path is None:
            raise CompositionJobError(
                "invalid_request", "a binding intent names an output that was not published"
            )
        try:
            measured = content_address_of_file(artifact_path)
        except OSError as exc:
            raise CompositionJobError(
                "invalid_request",
                f"published artifact unreadable: {type(exc).__name__}",
            ) from exc
        if measured != intent.get("artifact_id"):
            raise CompositionJobError(
                "invalid_request",
                "published artifact bytes do not match the measured binding digest",
            )
        try:
            version_id = await _register_artifact_in_library(
                payload, measured, artifact_path, project_id=str(intent["project_id"])
            )
        except CompositionJobError:
            raise
        except Exception as exc:  # noqa: BLE001 - registration must fail the job closed
            raise CompositionJobError(
                "invalid_request",
                f"artifact registration failed: {type(exc).__name__}: {exc}",
            ) from exc
        try:
            await store.bind(
                project_id=str(intent["project_id"]),
                revision_id=str(intent["revision_id"]),
                artifact_id=measured,
                job_id=job_id,
                artifact_kind=str(intent.get("artifact_kind") or "binary"),
                size_bytes=int(intent.get("size_bytes") or 0),
                artifact_version_id=version_id,
            )
        except Exception as exc:  # noqa: BLE001 - every binding failure must be typed
            raise CompositionJobError(
                "invalid_request", f"artifact binding failed: {type(exc).__name__}: {exc}"
            ) from exc


async def _register_artifact_in_library(
    payload: dict[str, Any],
    artifact_id: str,
    artifact_path: Path,
    *,
    project_id: str,
) -> str:
    """Register one published artifact as an Asset Library version; return its id.

    The artifact→revision binding completes the chain
    ``artifact → revision → asset version``; without the version id the last hop
    is unresolvable.  Registration goes through the library's own authorized
    ingest under the row's actor, so an actor who may run the job but lacks
    ``project:write`` is refused here rather than silently skipped.

    Idempotent by construction: the content address is the idempotency key, so a
    retried confirm re-uses the same version instead of minting a second one.
    """
    actor = payload.get("actor") or {}
    actor_kind = str(actor.get("kind") or "")
    actor_id = str(actor.get("actor_id") or "")
    if not actor_kind or not actor_id:
        raise CompositionJobError("invalid_request", "the row names no actor to register under")

    from nexus_ai_agent.creative.assets.factory import build_asset_library
    from nexus_ai_agent.creative.assets.models import IngestRequest

    library = build_asset_library()
    # The filename is derived from the content address, never the caller's path,
    # so a retry with a different workspace still re-uses the same version.
    request = IngestRequest(
        idempotency_key=f"composition:{artifact_id}",
        original_filename=f"{artifact_id[:16]}{artifact_path.suffix}",
    )
    with open(artifact_path, "rb") as stream:  # noqa: PTH123 - a real file handle
        result = await library.ingest(
            actor_kind=actor_kind,
            actor_id=actor_id,
            project_id=project_id,
            request=request,
            stream=stream,
        )
    return result.version_id


def _published_path_for(artifact_id: str, outputs: Any) -> Path | None:
    """Resolve the published path of the claim whose *published* digest matches."""
    from nexus_ai_agent.creative.project_state.store import content_address_of_file

    if not isinstance(outputs, list):
        return None
    for claim in outputs:
        if not isinstance(claim, dict):
            continue
        raw = claim.get("artifact_path")
        if not isinstance(raw, str) or not raw:
            continue
        path = Path(raw)
        try:
            if content_address_of_file(path) == artifact_id:
                return path
        except OSError:
            continue
    return None


def _artifact_claims(artifacts: dict[str, ProofCarryingArtifact]) -> list[dict[str, Any]]:
    """One canonical claim per expected output, each with its own evidence.

    The claim dialect is the one ``creative_render_job`` returns, so the
    queue's registered verifier can re-measure every deliverable, not just one.
    """
    claims: list[dict[str, Any]] = []
    for name, artifact in artifacts.items():
        claims.append(
            {
                "name": name,
                "artifact_path": artifact.output_path,
                "artifact_kind": "video" if artifact.output_path.endswith(".mp4") else "image",
                "sha256": artifact.output_hash,
                "size_bytes": artifact.output_size_bytes,
                "duration_us": artifact.semantic.measurements.get("duration_us", 0),
                "operation": artifact.operation_manifest.get("nodes", [{}])[-1].get(
                    "operation", ""
                ),
                "output_asset_id": artifact.receipts[-1].asset_id if artifact.receipts else "",
                "plan_fingerprint": artifact.plan_fingerprint,
                "registry_fingerprint": artifact.registry_fingerprint,
                "integrity_verified": artifact.integrity.verified,
                "semantic_verified": artifact.semantic.verified,
                "media_contract": dict(artifact.media_contract),
            }
        )
    return claims


__all__ = [
    "COMPOSITION_JOB_TYPE",
    "ERROR_CODES",
    "ActorClaim",
    "CompositionJobError",
    "CompositionJobPayload",
    "StagedInput",
    "composition_render_binding_confirm",
    "composition_render_job",
    "guarded_input",
    "plan_from_dict",
    "plan_to_dict",
    "trusted_workspace",
]
