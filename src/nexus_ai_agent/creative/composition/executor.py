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

import tempfile
import time
import uuid
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
from .contracts import CompositionPlan, ResourceEstimate

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
        actor: ActorIdentity | None = None,
    ) -> None:
        if bus is not None:
            self._bus = bus
            self._actor = actor or SERVICE_ACTOR
        else:
            self._project = project or _new_project()
            self._actor = actor or SERVICE_ACTOR
            self._bus = _build_bus(self._project, self._actor)
        self._engine = engine or MediaEngine()
        self._license_ledger = ffmpeg_ledger(self._engine.ffmpeg_binary)

    @property
    def bus(self) -> CommandBus:
        return self._bus

    @property
    def license_ledger(self) -> Any:
        """The measured licence ledger of the tool that produced the media."""
        return self._license_ledger

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
    ) -> ExecutionResult:
        """Execute *compiled* and return the artifacts plus their evidence."""
        started = time.monotonic()
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
            node_dir = workspace / f"node_{node_id}"
            node_dir.mkdir(parents=True, exist_ok=True)
            # Every node gets its **own** scratch directory, so a node can
            # only write inside it and cannot see a sibling's intermediate.
            scratch_dir = Path(tempfile.mkdtemp(prefix="scratch_", dir=node_dir))

            bound = self._resolve_binding(compiled, node, produced, external_inputs)
            source_path = bound["path"]
            source_asset_id = self._register_source(bound)
            input_hashes.update(
                {f"{node_id}:{key}": value for key, value in bound["hashes"].items()}
            )
            if bound.get("external_name"):
                consumed_inputs[str(bound["external_name"])] = bound["hashes"].get("source", "")

            params = dict(node.params)
            outcome = self._realize(node_id, node, spec, source_path, params, node_dir, scratch_dir)
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
    ) -> dict[str, Any]:
        """Find the media that feeds a node, with its measured facts."""
        from nexus_ai_agent.creative.slideshow.probe import probe_image

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
        evidence = probe_image(path)
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
                    idempotency_key=f"scan:{evidence.evidence_id}",
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

    def _realize(
        self,
        node_id: str,
        node: Any,
        spec: Any,
        source: Path,
        params: dict[str, Any],
        node_dir: Path,
        scratch_dir: Path,
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
            )
        except MediaEngineError as exc:
            raise CompositionExecutionError(f"node {node_id!r} failed: {exc}") from exc
        published = node_dir / f"{node_id}{suffix}"
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

    def _dispatch(self, node_id: str, node: Any, spec: Any, command_input: dict[str, Any]) -> Any:
        """Dispatch one canonical operation on the canonical bus."""
        try:
            return self._bus.dispatch(
                _command(
                    node.operation,
                    command_input,
                    project_id=self._bus.project.project_id,
                    actor=self._actor,
                    idempotency_key=f"composition:{node_id}:{node.operation}",
                    confirmed=True,
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
                parameters={node.node_id: dict(node.params) for node in compiled.plan.nodes},
                lineage=tuple(
                    f"{node.node_id}:{node.operation}@{node.capability_version}"
                    for node in compiled.plan.nodes
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


def _new_project() -> Project:
    """A fresh, empty project for a composition to run in."""
    return new_project(
        project_id=f"proj_{uuid.uuid4().hex[:12]}",
        name="composition",
        timeline=Timeline(
            timeline_id=f"tl_{uuid.uuid4().hex[:12]}",
            duration_us=0,
            playhead=Playhead(timecode_us=0),
        ),
    )


def _build_bus(project: Project, actor: ActorIdentity) -> CommandBus:
    """Build the canonical bus for a composition.

    The bus is built through the **single reviewed construction site**,
    :func:`nexus_ai_agent.creative.render_jobs.build_job_bus`, so the registry
    is always the runtime registry and the experimental opt-in stays
    server-controlled.  The composition layer builds no bus of its own — it
    only supplies the authority for its own service actor.

    A caller that has a real project and a real principal injects its own bus
    instead.
    """
    from nexus_ai_agent.creative.render_jobs import build_job_bus
    from nexus_ai_agent.creative.studio.authorization import ProjectAccess

    access = ProjectAccess(
        actor=actor,
        project_id=project.project_id,
        permissions=frozenset({"project:read", "project:write"}),
    )
    return build_job_bus(project, authorizer=access)


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
