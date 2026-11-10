"""Execute a compiled composition through the **canonical** command bus.

This is the seam where the composition layer stops being a design and becomes
part of NEXUS.  Every node of the plan is executed as a canonical
:class:`~nexus_ai_agent.creative.studio.models.TypedCommand` on the existing
:class:`~nexus_ai_agent.creative.studio.bus.CommandBus`, which means every
node gets the canonical treatment:

* the operation is resolved in the **live** registry (unknown operation → the
  bus refuses it);
* the actor's project grant is checked by the trusted authorizer;
* the pack lifecycle gate runs (``nexus.media.transform`` must be executable);
* the A/B/C/D permission ladder runs (level C needs the explicit confirmation
  the composition request itself carries);
* the idempotency reservation runs, so a redelivered node never executes
  twice.

The composition layer adds exactly three things on top and owns none of the
authority above:

1. the **media realization** (the engine) — the bytes;
2. the **registration** of the produced media as a project asset, through the
   canonical ``slideshow.scan_assets`` operation;
3. the **evidence** — the receipts, the integrity check and the independent
   semantic verification of every produced artifact.
"""

from __future__ import annotations

import re
import tempfile
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nexus_ai_agent.creative.composition.engine import (
    MediaEngine,
    MediaEngineError,
    MediaOutcome,
)
from nexus_ai_agent.creative.composition.license import (
    compose_ledger,
    ffmpeg_ledger,
    ledger_report,
)
from nexus_ai_agent.creative.composition.proof import (
    NodeReceipt,
    ProofCarryingArtifact,
    verify_integrity,
    verify_media_semantics,
)
from nexus_ai_agent.creative.studio.authorization import ProjectAccess
from nexus_ai_agent.creative.studio.bus import CommandBus
from nexus_ai_agent.creative.studio.models import (
    ActorIdentity,
    Playhead,
    Project,
    TargetRef,
    Timeline,
    TypedCommand,
    new_project,
)

from .compiler import CompiledPlan
from .contracts import NODE_ID_PATTERN, CompositionPlan, ResourceEstimate

#: The canonical operation that registers probed media in the project asset
#: registry.  The composition layer does not keep its own asset registry — it
#: uses the canonical one.
SCAN_OPERATION = "slideshow.scan_assets"

#: The service actor the composition runtime uses on its own project.  The
#: project is created by the runtime, so the grant is a *local* one; a
#: caller that has a real project supplies its own authorizer.
SERVICE_ACTOR = ActorIdentity(kind="service", actor_id="composition-runtime")


class CompositionExecutionError(RuntimeError):
    """A composition could not be executed."""


@dataclass
class ExecutionResult:
    """The outcome of executing one compiled composition."""

    plan: CompositionPlan
    plan_fingerprint: str
    registry_fingerprint: str
    outputs: dict[str, str]
    artifacts: dict[str, ProofCarryingArtifact]
    receipts: tuple[NodeReceipt, ...]
    state_revision: int
    state_hash: str
    duration_seconds: float
    estimate: ResourceEstimate
    tool_versions: dict[str, str] = field(default_factory=dict)
    license_ledger: dict[str, Any] = field(default_factory=dict)

    @property
    def verified(self) -> bool:
        """True only when every exposed artifact passed both verification layers."""
        return bool(self.artifacts) and all(
            artifact.verified for artifact in self.artifacts.values()
        )


class CompositionExecutor:
    """Run a compiled plan, node by node, on the canonical command bus."""

    def __init__(
        self,
        *,
        bus: CommandBus | None = None,
        engine: MediaEngine | None = None,
        project: Project | None = None,
        project_id: str | None = None,
        actor: ActorIdentity | None = None,
        access: ProjectAccess | None = None,
        authorizer: Any | None = None,
        confirmed: bool = False,
    ) -> None:
        self._confirmed = confirmed
        if bus is not None:
            self._bus = bus
            self._actor = actor or SERVICE_ACTOR
            self._access = access
            self._authority = "injected_bus"
        elif authorizer is not None:
            # A trusted authorizer decides whether *this* actor may touch the
            # project; the runtime never widens the answer.  ``ProjectAccess``
            # is the only thing the bus consults, so it is exactly what the
            # authorizer returned -- never a permission set built here.
            self._project = project or _new_project(project_id)
            self._actor = actor or SERVICE_ACTOR
            decided = authorizer.authorize(self._actor, self._project.project_id)
            self._access, self._bus = _build_bus(self._project, self._actor, decided)
            self._authority = "injected_authorizer"
        else:
            self._project = project or _new_project(project_id)
            self._actor = actor or SERVICE_ACTOR
            self._access, self._bus = _build_bus(self._project, self._actor)
            #: The grant below is minted over a project this call just created.
            #: It is *not* the requester's authority, and a caller that needs
            #: the requester's authority must inject a bus or an authorizer.
            self._authority = "ephemeral_service_grant"
        self._engine = engine or MediaEngine()
        self._license_ledger = ffmpeg_ledger(self._engine.ffmpeg_binary)
        #: A nonce unique to the execution in flight.  The bus reserves
        #: ``(project, operation, idempotency_key)``, so a key derived only from
        #: the node would make a *second* composition collide with the first on
        #: the same bus and return its reserved result instead of executing.
        self._run_id = uuid.uuid4().hex[:12]

    @property
    def bus(self) -> CommandBus:
        return self._bus

    @property
    def license_ledger(self) -> Any:
        """The measured licence ledger of the tool that produced the media."""
        return self._license_ledger

    @property
    def authority_source(self) -> str:
        """Where this execution's authority came from.

        ``injected_bus`` / ``injected_authorizer`` mean a trusted composition
        root decided; ``ephemeral_service_grant`` means the runtime minted a
        local grant over a project it created in this call, which is **not**
        the requester's project authority.
        """
        return self._authority

    @property
    def project(self) -> Project:
        return self._bus.project

    # -- public API ---------------------------------------------------------

    def execute(
        self,
        compiled: CompiledPlan,
        *,
        external_inputs: dict[str, Path],
        workspace: Path,
        expected_digests: Mapping[str, str] | None = None,
    ) -> ExecutionResult:
        """Execute *compiled* and return the artifacts plus their evidence."""
        started = time.monotonic()
        # A re-run of the same executor is a *new* execution: it must not reuse
        # the previous run's reservations.
        self._run_id = uuid.uuid4().hex[:12]
        workspace = Path(workspace)
        workspace.mkdir(parents=True, exist_ok=True)

        produced: dict[str, MediaOutcome] = {}
        node_outputs: dict[str, dict[str, MediaOutcome]] = {}
        receipts: list[NodeReceipt] = []
        input_hashes: dict[str, str] = {}
        consumed_inputs: dict[str, str] = {}

        for node_id in compiled.order:
            node = compiled.plan.node(node_id)
            spec = compiled.spec(node_id)
            # The node directory is **derived**, then **verified**: a node id
            # is a name, never a path, and the resolved directory must stay
            # inside the guarded workspace even if the workspace itself is
            # re-pointed by a symlink.  This is the containment guard for the
            # per-node working area.
            node_dir = _node_directory(workspace, node_id)
            # Every node gets its **own** scratch directory, so a node can
            # only write inside it and cannot see a sibling's intermediate.
            scratch_dir = Path(tempfile.mkdtemp(prefix="scratch_", dir=node_dir))

            bound = self._resolve_binding(
                compiled, node, produced, external_inputs, expected_digests or {}
            )
            source_path = bound["path"]
            source_asset_id = self._register_source(bound)
            input_hashes.update(
                {f"{node_id}:{key}": value for key, value in bound["hashes"].items()}
            )
            if bound.get("external_name"):
                consumed_inputs[str(bound["external_name"])] = bound["hashes"].get("source", "")

            # The canonical authorization runs **before** any media work: a
            # node the bus would refuse must not leave output files behind.
            self._authorize_node(node_id, node, spec)

            params = dict(node.params)
            outcome = self._realize(
                node_id,
                node,
                spec,
                source_path,
                params,
                node_dir,
                scratch_dir,
                timeout_seconds=self._node_timeout(compiled, node_id),
            )
            produced[node_id] = outcome

            # Every node is verified, not only the exposed outputs: an
            # intermediate that is the wrong size or is not actually
            # grayscale must fail the composition, not slip through to the
            # last node.
            node_semantic = self._verify_node(node_id, node, spec, outcome)

            command_input = self._command_input(
                node_id=node_id,
                node=node,
                spec=spec,
                params=params,
                source_asset_id=source_asset_id,
                source=bound,
                outcome=outcome,
            )
            result = self._dispatch(node_id, node, spec, command_input)
            node_outputs[node_id] = {name: outcome for name in outcome.facts.get("ports", {}) or {}}

            receipts.append(
                NodeReceipt(
                    node_id=node_id,
                    operation=node.operation,
                    capability_id=node.capability_id,
                    capability_version=node.capability_version,
                    params=dict(node.params),
                    input_hashes=dict(bound["hashes"]),
                    output_path=outcome.path,
                    output_hash=outcome.sha256,
                    output_size_bytes=outcome.size_bytes,
                    duration_seconds=float(outcome.facts.get("duration_seconds", 0.0)),
                    state_revision=result.state_revision,
                    state_hash=result.state_hash,
                    asset_id=str(result.output.get("asset_id", "")),
                    semantic=node_semantic,
                )
            )

        # The ledger records the *consumed* inputs as well as the measured
        # tool: an input asset's own terms are unknown to this runtime, and
        # that keeps the commercial verdict false.
        ledger = compose_ledger(self._license_ledger, consumed_inputs)
        artifacts = self._collect_artifacts(
            compiled,
            produced=produced,
            receipts=tuple(receipts),
            input_hashes=input_hashes,
            ledger=ledger,
        )
        return ExecutionResult(
            plan=compiled.plan,
            plan_fingerprint=compiled.fingerprint,
            registry_fingerprint=compiled.registry_fingerprint,
            outputs={name: artifact.output_path for name, artifact in artifacts.items()},
            artifacts=artifacts,
            receipts=tuple(receipts),
            state_revision=self._bus.state_revision,
            state_hash=self._bus.state_hash,
            duration_seconds=time.monotonic() - started,
            estimate=compiled.estimate,
            tool_versions=self._engine.tool_versions(),
            license_ledger=ledger_report(ledger),
        )

    # -- internals ----------------------------------------------------------

    def _resolve_binding(
        self,
        compiled: CompiledPlan,
        node: Any,
        produced: dict[str, MediaOutcome],
        external_inputs: dict[str, Path],
        expected_digests: Mapping[str, str],
    ) -> dict[str, Any]:
        """Find the media that feeds a node, with its measured facts.

        The file is **re-measured at the point of use** and, for a staged
        external input, compared with the digest the row was admitted with.  A
        concurrent writer that replaced the file between admission and this
        read would otherwise hand the encoder bytes the row never authorized.
        """
        from nexus_ai_agent.creative.slideshow.probe import probe_image

        from .engine import MAX_INPUT_BYTES, enforce_input_limit

        bindings = {binding.input_port: binding for binding in node.bindings}
        if not bindings:
            raise CompositionExecutionError(
                f"node {node.node_id!r} has no media binding; the compiler requires one"
            )
        binding = next(iter(bindings.values()))
        if binding.source == "external":
            source = external_inputs.get(binding.external or "")
            if source is None:
                raise CompositionExecutionError(
                    f"external input {binding.external!r} was not supplied"
                )
            path = Path(source)
            external_name = binding.external
        else:
            upstream = produced.get(binding.node_id or "")
            if upstream is None:
                raise CompositionExecutionError(
                    f"node {node.node_id!r} binds to node {binding.node_id!r}, which has "
                    "not produced anything yet"
                )
            path = Path(upstream.path)
            external_name = None
        if not path.is_file():
            raise CompositionExecutionError(f"bound media is missing: {path}")
        # Bound the file *before* decoding it: a huge staged image would
        # otherwise consume the read and the decode before the engine's own
        # limit ever ran.
        try:
            enforce_input_limit(path, MAX_INPUT_BYTES)
        except Exception as exc:
            raise CompositionExecutionError(
                f"node {node.node_id!r}: the bound media is over the input limit: {exc}"
            ) from exc
        evidence = probe_image(path)
        if external_name is not None:
            admitted = expected_digests.get(external_name)
            if admitted and _strip_sha_prefix(admitted) != _strip_sha_prefix(
                evidence.content_sha256
            ):
                raise CompositionExecutionError(
                    f"external input {external_name!r} changed after it was admitted: "
                    f"admitted {admitted}, measured {evidence.content_sha256}"
                )
        return {
            "path": path,
            "evidence": evidence,
            "hashes": {"source": evidence.content_sha256},
            "external_name": external_name,
        }

    def _register_source(self, bound: dict[str, Any]) -> str:
        """Register the bound media in the canonical project asset registry."""
        evidence = bound["evidence"]
        try:
            result = self._bus.dispatch(
                _command(
                    SCAN_OPERATION,
                    {"assets": [evidence.model_dump(mode="json")]},
                    project_id=self._bus.project.project_id,
                    actor=self._actor,
                    # The nonce keeps a *second* execution of the same plan a
                    # new command: the same content probed again produces a
                    # payload with a different path, and reusing the key would
                    # raise an idempotency conflict instead of running.
                    idempotency_key=f"scan:{self._run_id}:{evidence.evidence_id}",
                )
            )
        except Exception as exc:
            raise CompositionExecutionError(
                f"registering the source media failed: {type(exc).__name__}: {exc}"
            ) from exc
        registered = result.output.get("registered") or []
        unchanged = result.output.get("unchanged") or []
        if registered:
            return str(registered[0])
        if unchanged:
            return str(unchanged[0])
        raise CompositionExecutionError("the canonical asset registry returned no asset id")

    def _node_timeout(self, compiled: CompiledPlan, node_id: str) -> int:
        """The **declared** budget of one node, in seconds.

        The estimate the compiler published is the deadline the encoder is
        held to, so a caller that reads ``ResourceEstimate.max_timeout_seconds``
        reads the number that actually bounds the work.
        """
        requirement = compiled.requirements.get(node_id)
        if requirement is not None:  # pragma: no branch
            return int(requirement.timeout_seconds)
        return int(compiled.estimate.max_timeout_seconds)

    def _realize(
        self,
        node_id: str,
        node: Any,
        spec: Any,
        source: Path,
        params: dict[str, Any],
        node_dir: Path,
        scratch_dir: Path,
        *,
        timeout_seconds: int | None = None,
    ) -> MediaOutcome:
        """Produce the node's media with the canonical FFmpeg primitives.

        The encoder writes **inside the node's own scratch directory**; the
        finished artifact is then published into the node directory with an
        atomic rename.  A node therefore cannot write outside its scratch,
        and a failed encode leaves no artifact behind.
        """
        suffix = ".mp4" if node.operation.endswith("image_to_video") else ".png"
        scratch_output = scratch_dir / f"{node_id}{suffix}"
        try:
            outcome = self._engine.realize(
                node.operation,
                source=source,
                output_path=scratch_output,
                params=params,
                scratch_dir=scratch_dir,
                timeout_seconds=timeout_seconds,
            )
        except MediaEngineError as exc:
            raise CompositionExecutionError(f"node {node_id!r} failed: {exc}") from exc
        # The published name carries the execution nonce: two compositions
        # sharing a workspace and a node id can never publish over each
        # other's artifact (a deterministic name let one job replace bytes the
        # other had already hashed and was about to verify).
        published = node_dir / f"{node_id}-{self._run_id}{suffix}"
        Path(outcome.path).replace(published)
        outcome.facts["duration_seconds"] = 0.0
        return MediaOutcome(
            path=str(published),
            sha256=outcome.sha256,
            size_bytes=outcome.size_bytes,
            width=outcome.width,
            height=outcome.height,
            duration_us=outcome.duration_us,
            facts=outcome.facts,
        )

    def _verify_node(
        self,
        node_id: str,
        node: Any,
        spec: Any,
        outcome: MediaOutcome,
    ) -> Any:
        """Verify one node's output against the canonical media contract."""
        ports = spec.media_outputs
        contract = ports[0].contract if ports else None
        report = verify_media_semantics(
            Path(outcome.path),
            contract,
            expected_width=int(outcome.width) or None,
            expected_height=int(outcome.height) or None,
            expected_duration_us=int(outcome.duration_us) or None,
            expected_fps=int(outcome.facts.get("fps", 0)) or None,
        )
        if not report.verified:
            raise CompositionExecutionError(
                f"node {node_id!r} ({node.operation}) produced an artifact that does not "
                f"match its media contract: {'; '.join(report.findings)}"
            )
        return report

    def _command_input(
        self,
        *,
        node_id: str,
        node: Any,
        spec: Any,
        params: dict[str, Any],
        source_asset_id: str,
        source: dict[str, Any],
        outcome: MediaOutcome,
    ) -> dict[str, Any]:
        """Build the canonical operation input: the caller's params plus the measured facts."""
        evidence = source["evidence"]
        measured: dict[str, Any] = {
            "source_asset_id": source_asset_id,
            "source_sha256": evidence.content_sha256,
            "output_path": outcome.path,
            "output_sha256": outcome.sha256,
        }
        if "source_width" in spec.input_model.model_fields:
            measured["source_width"] = int(evidence.width or 0)
            measured["source_height"] = int(evidence.height or 0)
        if "width" in spec.input_model.model_fields and "width" not in params:
            measured["width"] = int(evidence.width or 0)
            measured["height"] = int(evidence.height or 0)
        if "mean_luma" in spec.input_model.model_fields:
            measured["mean_luma"] = float(evidence.mean_luma)
        payload = {**params, **measured}
        # Only the fields the canonical schema declares are allowed through.
        allowed = set(spec.input_model.model_fields)
        return {key: value for key, value in payload.items() if key in allowed}

    def _authorize_node(self, node_id: str, node: Any, spec: Any) -> None:
        """Ask the **bus** whether it would authorize this node, before running it.

        The dispatch performs these checks itself, but by then the encoder has
        already run and left files behind; a refused node must never have
        produced media.  Rather than re-implementing the gates -- which would
        create a second authority -- this calls ``CommandBus.check_authorization``,
        which runs the *same* helpers the dispatch runs (actor grant, capability
        availability, pack lifecycle, A/B/C/D ladder) and reserves nothing.

        The input model is not validated here on purpose: the canonical
        operations take the produced artifact's hash as input, and that hash
        does not exist until the encode.  The dispatch validates the real input
        before anything is applied.

        One authority, consulted twice.
        """
        try:
            self._bus.check_authorization(
                _command(
                    node.operation,
                    dict(node.params),
                    project_id=self._bus.project.project_id,
                    actor=self._actor,
                    idempotency_key=f"composition:{self._run_id}:{node_id}",
                    confirmed=self._confirmed,
                    operation_schema_version=spec.schema_version,
                )
            )
        except Exception as exc:
            raise CompositionExecutionError(
                f"the canonical bus refused node {node_id!r} ({node.operation}) "
                f"before any media work: {type(exc).__name__}: {exc}"
            ) from exc

    def _dispatch(self, node_id: str, node: Any, spec: Any, command_input: dict[str, Any]) -> Any:
        """Dispatch one canonical operation on the canonical bus."""
        try:
            return self._bus.dispatch(
                _command(
                    node.operation,
                    command_input,
                    project_id=self._bus.project.project_id,
                    actor=self._actor,
                    idempotency_key=f"composition:{self._run_id}:{node_id}",
                    confirmed=self._confirmed,
                    operation_schema_version=spec.schema_version,
                )
            )
        except Exception as exc:
            raise CompositionExecutionError(
                f"the canonical bus refused node {node_id!r} ({node.operation}): "
                f"{type(exc).__name__}: {exc}"
            ) from exc

    def _collect_artifacts(
        self,
        compiled: CompiledPlan,
        *,
        produced: dict[str, MediaOutcome],
        receipts: tuple[NodeReceipt, ...],
        input_hashes: dict[str, str],
        ledger: Any,
    ) -> dict[str, ProofCarryingArtifact]:
        """Verify every exposed output and build its proof-carrying artifact."""
        artifacts: dict[str, ProofCarryingArtifact] = {}
        for expected in compiled.plan.expected_outputs:
            outcome = produced.get(expected.node_id)
            if outcome is None:
                raise CompositionExecutionError(
                    f"expected output {expected.name!r} names node {expected.node_id!r}, "
                    "which produced nothing"
                )
            spec = compiled.spec(expected.node_id)
            contract = spec.media_output(expected.output_port).contract
            integrity = verify_integrity(Path(outcome.path), outcome.sha256)
            semantic = verify_media_semantics(
                Path(outcome.path),
                contract,
                expected_width=int(outcome.width) or None,
                expected_height=int(outcome.height) or None,
                expected_duration_us=int(outcome.duration_us) or None,
                expected_fps=int(outcome.facts.get("fps", 0)) or None,
            )
            artifacts[expected.name] = ProofCarryingArtifact(
                artifact_id=f"art_{uuid.uuid4().hex[:12]}",
                output_name=expected.name,
                output_path=outcome.path,
                output_hash=outcome.sha256,
                output_size_bytes=outcome.size_bytes,
                input_hashes=dict(input_hashes),
                operation_manifest={
                    "plan_fingerprint": compiled.fingerprint,
                    "registry_fingerprint": compiled.registry_fingerprint,
                    "nodes": [
                        {
                            "node_id": node.node_id,
                            "operation": node.operation,
                            "capability_id": node.capability_id,
                            "capability_version": node.capability_version,
                            "params": dict(node.params),
                        }
                        for node in compiled.plan.nodes
                    ],
                    "order": list(compiled.order),
                },
                tool_versions=self._engine.tool_versions(),
                parameters={
                    node_id: dict(compiled.node(node_id).params) for node_id in compiled.order
                },
                # The lineage states the order the nodes actually ran in: a plan
                # may declare its nodes in any order, and provenance that
                # reorders them would misdescribe the derivation.
                lineage=tuple(
                    f"{node_id}:{compiled.node(node_id).operation}"
                    f"@{compiled.node(node_id).capability_version}"
                    for node_id in compiled.order
                ),
                receipts=receipts,
                integrity=integrity,
                semantic=semantic,
                plan_fingerprint=compiled.fingerprint,
                registry_fingerprint=compiled.registry_fingerprint,
                license_ledger=ledger_report(ledger),
                state_revision=self._bus.state_revision,
                state_hash=self._bus.state_hash,
                media_contract=contract.model_dump(mode="json"),
            )
        return artifacts


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _node_directory(workspace: Path, node_id: str) -> Path:
    """The per-node working directory, **verified** to stay inside the workspace.

    The node id is validated as a *name* (no separators, no parent
    references) and the resolved directory is checked against the resolved
    workspace.  Both checks are needed: the pattern alone would not survive a
    workspace that is itself re-pointed by a symlink, and the containment
    check alone would not survive a node id that names a sibling directory.
    """
    if not NODE_ID_PATTERN or not re.fullmatch(NODE_ID_PATTERN, node_id):
        raise CompositionExecutionError(
            f"node id {node_id!r} is not a plain name and may not be used as a directory"
        )
    root = Path(workspace).resolve()
    node_dir = (root / f"node_{node_id}").resolve(strict=False)
    try:
        node_dir.relative_to(root)
    except ValueError as exc:
        raise CompositionExecutionError(
            f"node {node_id!r} resolves outside the guarded workspace"
        ) from exc
    node_dir.mkdir(parents=True, exist_ok=True)
    return node_dir


def _new_project(project_id: str | None = None) -> Project:
    """A fresh, empty project for a composition to run in.

    ``project_id`` lets a row run under the **durable** project identity it was
    authorized against; without one the project is a local, ephemeral one.
    """
    return new_project(
        project_id=project_id or f"proj_{uuid.uuid4().hex[:12]}",
        name="composition",
        timeline=Timeline(
            timeline_id=f"tl_{uuid.uuid4().hex[:12]}",
            duration_us=0,
            playhead=Playhead(timecode_us=0),
        ),
    )


def _build_bus(
    project: Project,
    actor: ActorIdentity,
    access: ProjectAccess | None = None,
) -> tuple[ProjectAccess, CommandBus]:
    """Build the canonical bus for a composition.

    The bus is built through the **single reviewed construction site**,
    :func:`nexus_ai_agent.creative.render_jobs.build_job_bus`, so the registry
    is always the runtime registry and the experimental opt-in stays
    server-controlled.  The composition layer builds no bus of its own — it
    only supplies the authority for its own service actor.

    The grant is returned as well, because the executor's **pre-flight**
    authorization consults the very same object the bus will consult later —
    one authority, consulted twice, never two authorities.

    A caller that has a real project and a real principal injects its own bus
    instead.
    """
    from nexus_ai_agent.creative.render_jobs import build_job_bus
    from nexus_ai_agent.creative.studio.authorization import ProjectAccess

    if access is None:
        access = ProjectAccess(
            actor=actor,
            project_id=project.project_id,
            permissions=frozenset({"project:read", "project:write"}),
        )
    return access, build_job_bus(project, authorizer=access)


def _command(
    operation: str,
    payload: dict[str, Any],
    *,
    project_id: str,
    actor: ActorIdentity,
    idempotency_key: str,
    confirmed: bool = False,
    operation_schema_version: int = 1,
) -> TypedCommand:
    """Build a canonical typed command."""
    return TypedCommand(
        protocol_version="nagar.command.v1",
        operation_schema_version=operation_schema_version,
        command_id=f"cmd_{uuid.uuid4().hex[:16]}",
        session_id="composition",
        actor=actor,
        target=TargetRef(project_id=project_id, track_id="main"),
        operation=operation,
        input=payload,
        input_refs=(),
        idempotency_key=idempotency_key,
        confirmed=confirmed,
    )


__all__ = [
    "SCAN_OPERATION",
    "SERVICE_ACTOR",
    "CompositionExecutionError",
    "CompositionExecutor",
    "ExecutionResult",
]


def _strip_sha_prefix(digest: str) -> str:
    """``sha256:<hex>`` and ``<hex>`` name the same bytes."""
    return digest.split(":", 1)[1] if digest.startswith("sha256:") else digest
