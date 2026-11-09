"""The Composition Runtime.

The runtime is the only thing that *executes* a composition. It takes a
validated :class:`CompositionGraph`, runs each node through a
:class:`MediaWorker` in topological order, and returns both the output files
and a :class:`ProofCarryingArtifact` for each exposed output.

Authority boundary (same law as the rest of this repository):

* The runtime does **not** create a queue, a bus, or a commit authority. It
  is a synchronous executor over a scratch directory.
* The runtime does **not** grant itself permission to run anything. The
  caller passes a :class:`RuntimePolicy`; a composition that violates the
  policy (network access, commercial use of a non-commercial licence, a
  resource total above the quota) is refused before any worker runs.
* The runtime does **not** trust worker output. It independently hashes
  every output and bakes the hash into the proof-carrying artifact.

This is the vertical slice that proves the NAGAR thesis: three independent
media capabilities (resize, grayscale, image→video) are composed into a new
capability (``grayscale_resized_video``) that none of them provides alone,
executed for real, and verified.
"""

from __future__ import annotations

import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .compiler import CompositionCompiler, CompositionError, CompositionGraph
from .proof import (
    NodeReceipt,
    ProofCarryingArtifact,
    VerificationReport,
    sha256_file,
    verify_proof,
)
from .spec import CapabilitySpec, ResourceRequirements
from .workers import BuiltinMediaWorker, MediaWorker, WorkerError, detect_tool_versions


class RuntimeError(Exception):
    """The runtime refused or failed to execute a composition."""


@dataclass(frozen=True)
class RuntimePolicy:
    """What the runtime is allowed to do.

    ``allow_network`` gates any composition whose derived spec declares
    ``network_required=True``. ``allow_commercial`` gates any composition
    whose derived licence is non-commercial. ``quota`` caps the summed
    resource requirements. ``max_nodes`` caps graph size as a guard against
    runaway compositions.
    """

    allow_network: bool = False
    allow_commercial: bool = True
    quota: ResourceRequirements = field(default_factory=ResourceRequirements)
    max_nodes: int = 64


@dataclass
class ExecutionResult:
    """The outcome of executing one composition."""

    derived_spec: CapabilitySpec
    outputs: dict[str, str]
    artifacts: dict[str, ProofCarryingArtifact]
    receipts: tuple[NodeReceipt, ...]
    duration_seconds: float
    scratch_dir: str


class CompositionRuntime:
    """Execute a composition graph and produce proof-carrying artifacts."""

    def __init__(
        self,
        registry: dict[str, CapabilitySpec],
        *,
        worker: MediaWorker | None = None,
        policy: RuntimePolicy | None = None,
    ) -> None:
        if not registry:
            raise RuntimeError("runtime requires a non-empty capability registry")
        self._registry = dict(registry)
        self._compiler = CompositionCompiler(self._registry)
        self._worker: MediaWorker = worker or BuiltinMediaWorker()
        self._policy = policy or RuntimePolicy()
        self._tool_versions = detect_tool_versions()

    # -- public API ---------------------------------------------------------

    @property
    def policy(self) -> RuntimePolicy:
        return self._policy

    @property
    def tool_versions(self) -> dict[str, str]:
        return self._tool_versions.to_dict()

    def execute(
        self,
        graph: CompositionGraph,
        external_inputs: dict[str, str],
        *,
        output_dir: str | Path | None = None,
        commercial_use: bool = False,
    ) -> ExecutionResult:
        """Execute *graph* and return outputs + proof-carrying artifacts.

        ``external_inputs`` maps external input name -> absolute path to the
        input media. ``output_dir`` is the scratch root; a temporary
        directory is used when omitted. ``commercial_use`` is checked
        against the derived licence.

        Raises :class:`RuntimeError` (or :class:`CompositionError`) before
        any worker runs if the graph fails validation or policy.
        """
        start = time.monotonic()
        if len(graph.nodes) > self._policy.max_nodes:
            raise RuntimeError(
                f"composition has {len(graph.nodes)} nodes, exceeds max "
                f"{self._policy.max_nodes}"
            )

        # 1. Compile (validates structure, types, resources, licences, determinism)
        derived = self._compiler.compile(graph, quota=self._policy.quota)

        # 2. Policy gate (before any execution)
        self._enforce_policy(derived, commercial_use)

        # 3. Validate external inputs are present and exist
        for name in graph.external_inputs:
            if name not in external_inputs:
                raise RuntimeError(f"missing external input: {name!r}")
            if not Path(external_inputs[name]).exists():
                raise RuntimeError(f"external input {name!r} not found: {external_inputs[name]}")

        # 4. Execute in topological order
        scratch = Path(output_dir) if output_dir else Path(tempfile.mkdtemp(prefix="nagar_compose_"))
        scratch.mkdir(parents=True, exist_ok=True)

        # hash external inputs once
        external_hashes = {name: sha256_file(p) for name, p in external_inputs.items()}

        node_outputs: dict[str, dict[str, str]] = {}  # node_id -> {output_name: path}
        receipts: list[NodeReceipt] = []

        order = self._compiler._topological_order(graph)  # noqa: SLF001 -- deterministic order
        for node_id in order:
            node = graph.nodes[node_id]
            spec = self._registry[node.capability_id]
            node_dir = scratch / f"node_{node_id}"
            node_dir.mkdir(parents=True, exist_ok=True)

            # resolve inputs into this node's scratch dir
            resolved_inputs: dict[str, str] = {}
            input_hashes: dict[str, str] = {}
            for inp in spec.inputs:
                binding = node.bindings[inp.name]
                if binding.external is not None:
                    src_path = external_inputs[binding.external]
                    h = external_hashes[binding.external]
                else:
                    src_node, out_name = binding.node_output
                    src_path = node_outputs[src_node][out_name]
                    h = sha256_file(src_path)
                # copy into node dir so the worker's path guard is meaningful
                dst = node_dir / Path(src_path).name
                shutil.copy2(src_path, dst)
                resolved_inputs[inp.name] = str(dst)
                input_hashes[inp.name] = h

            # run the worker
            t0 = time.monotonic()
            try:
                outputs = self._worker.run(
                    node.capability_id,
                    resolved_inputs,
                    dict(node.params),
                    node_dir,
                )
            except WorkerError as exc:
                raise RuntimeError(
                    f"node {node_id!r} ({node.capability_id}) failed: {exc}"
                ) from exc
            duration = time.monotonic() - t0

            # verify the worker produced the declared outputs
            for out in spec.outputs:
                if out.name not in outputs:
                    raise RuntimeError(
                        f"node {node_id!r}: worker did not produce output {out.name!r}"
                    )
                if not Path(outputs[out.name]).exists():
                    raise RuntimeError(
                        f"node {node_id!r}: worker output {out.name!r} does not exist"
                    )

            output_hashes = {name: sha256_file(p) for name, p in outputs.items()}
            node_outputs[node_id] = outputs
            receipts.append(
                NodeReceipt(
                    node_id=node_id,
                    capability_id=node.capability_id,
                    version=spec.version,
                    params=dict(node.params),
                    input_hashes=input_hashes,
                    output_paths=outputs,
                    output_hashes=output_hashes,
                    duration_seconds=duration,
                    worker=type(self._worker).__name__,
                )
            )

        # 5. Collect expected outputs + build proof-carrying artifacts
        final_outputs: dict[str, str] = {}
        artifacts: dict[str, ProofCarryingArtifact] = {}
        manifest = self._build_manifest(graph, derived)
        generated_at = datetime.now(timezone.utc).isoformat()

        for out_name, (node_id, port_name) in graph.expected_outputs.items():
            out_path = node_outputs[node_id][port_name]
            final_outputs[out_name] = out_path
            out_hash = sha256_file(out_path)
            artifact = ProofCarryingArtifact(
                artifact_id=f"art_{uuid.uuid4().hex[:12]}",
                output_name=out_name,
                output_path=out_path,
                output_hash=out_hash,
                input_hashes=external_hashes,
                operation_manifest=manifest,
                tool_versions=self._tool_versions.to_dict(),
                parameters={nid: graph.nodes[nid].params for nid in order},
                license=derived.license,
                execution_receipts=tuple(receipts),
                lineage=tuple(
                    f"{graph.nodes[nid].capability_id}@{self._registry[graph.nodes[nid].capability_id].version}"
                    for nid in order
                ),
                verification=VerificationReport(
                    verified=True,
                    method="independent_rehash",
                    findings=(),
                    remeasured_hashes={out_name: out_hash},
                ),
                generated_at=generated_at,
            )
            # independently verify right now (fail-closed)
            report = verify_proof(artifact)
            if not report.verified:
                raise RuntimeError(
                    f"artifact {out_name!r} failed independent verification: "
                    + "; ".join(report.findings)
                )
            artifact = ProofCarryingArtifact(
                **{**artifact.__dict__, "verification": report}
            )
            artifacts[out_name] = artifact

        total = time.monotonic() - start
        return ExecutionResult(
            derived_spec=derived,
            outputs=final_outputs,
            artifacts=artifacts,
            receipts=tuple(receipts),
            duration_seconds=total,
            scratch_dir=str(scratch),
        )

    # -- internals ----------------------------------------------------------

    def _enforce_policy(self, derived: CapabilitySpec, commercial_use: bool) -> None:
        if derived.network_required and not self._policy.allow_network:
            raise RuntimeError(
                "composition requires network access but policy forbids it "
                "(allow_network=False)"
            )
        if commercial_use and not derived.license.effective_allow_commercial:
            raise RuntimeError(
                "commercial use requested but composition contains a "
                "non-commercial-licensed component"
            )
        if not self._policy.allow_commercial and not derived.license.effective_allow_commercial:
            raise RuntimeError(
                "composition is non-commercial but policy does not allow "
                "non-commercial execution"
            )

    def _build_manifest(self, graph: CompositionGraph, derived: CapabilitySpec) -> dict[str, Any]:
        nodes = []
        for nid, node in graph.nodes.items():
            nodes.append({
                "node_id": nid,
                "capability_id": node.capability_id,
                "version": self._registry[node.capability_id].version,
                "params": node.params,
                "bindings": {
                    name: (b.external if b.external else f"{b.node_output[0]}.{b.node_output[1]}")
                    for name, b in node.bindings.items()
                },
            })
        return {
            "derived_capability_id": derived.capability_id,
            "derived_version": derived.version,
            "nodes": nodes,
            "external_inputs": list(graph.external_inputs),
            "expected_outputs": list(graph.expected_outputs),
        }


__all__ = [
    "CompositionRuntime",
    "ExecutionResult",
    "RuntimeError",
    "RuntimePolicy",
]
