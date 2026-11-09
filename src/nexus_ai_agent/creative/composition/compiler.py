"""The Capability Composition Compiler.

The compiler takes a *graph* of capability instances and decides whether they
can be composed into one new capability. It validates four contracts before it
emits a derived :class:`CapabilitySpec`:

1. **Structure** -- the graph is a DAG; every required input is bound; no
   binding references a node/output that does not exist.
2. **Types** -- the media type produced by an upstream output is *compatible*
   with the media type required by the downstream input (see
   :meth:`MediaType.compatible_with`).
3. **Resources** -- the sum of every node's declared resources does not exceed
   the runtime quota. A composition that needs more RAM than the host has is
   refused at compile time, not at run time.
4. **Licence** -- copyleft propagates to the whole composition; if any
   component is non-commercial, the composition is non-commercial. The
   compiler records the *derived* licence so a policy engine can act on it.
5. **Determinism** -- if the composition claims to be deterministic, every
   node in it must be deterministic. A single stochastic node demotes the
   whole composition to non-deterministic (it does not refuse -- the caller
   decides whether that is acceptable).

The emitted derived spec is itself a :class:`CapabilitySpec`, so a composition
can be composed again -- composition is closed.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any

from .spec import (
    CapabilityInput,
    CapabilityOutput,
    CapabilitySpec,
    LicenseInfo,
    MediaType,
    ResourceRequirements,
    VerificationContract,
)


class CompositionError(ValueError):
    """A composition graph failed validation. The message names the first fault."""


# ---------------------------------------------------------------------------
# Graph model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InputBinding:
    """Binds one input of a node to either an external input or another node's output.

    Exactly one of ``external`` / ``node_output`` is set. ``external`` names a
    key in :attr:`CompositionGraph.external_inputs`; ``node_output`` is a
    ``(node_id, output_name)`` pair.
    """

    input_name: str
    external: str | None = None
    node_output: tuple[str, str] | None = None

    def __post_init__(self) -> None:
        if (self.external is None) == (self.node_output is None):
            raise ValueError("InputBinding must set exactly one of external or node_output")


@dataclass
class CompositionNode:
    """One capability instance in the composition graph."""

    node_id: str
    capability_id: str
    params: dict[str, Any] = field(default_factory=dict)
    bindings: dict[str, InputBinding] = field(default_factory=dict)


@dataclass
class CompositionGraph:
    """A composition: nodes, the external inputs fed in, and the outputs exposed.

    ``external_inputs`` maps an external name to the :class:`MediaType` it
    carries. ``expected_outputs`` maps an external output name to a
    ``(node_id, output_name)`` pair -- the value the composition exposes.
    """

    nodes: dict[str, CompositionNode] = field(default_factory=dict)
    external_inputs: dict[str, MediaType] = field(default_factory=dict)
    expected_outputs: dict[str, tuple[str, str]] = field(default_factory=dict)

    def add_node(self, node: CompositionNode) -> None:
        if node.node_id in self.nodes:
            raise CompositionError(f"duplicate node id: {node.node_id!r}")
        self.nodes[node.node_id] = node

    def add_external_input(self, name: str, media_type: MediaType) -> None:
        if name in self.external_inputs:
            raise CompositionError(f"duplicate external input: {name!r}")
        self.external_inputs[name] = media_type

    def add_expected_output(self, name: str, node_id: str, output_name: str) -> None:
        if name in self.expected_outputs:
            raise CompositionError(f"duplicate expected output: {name!r}")
        self.expected_outputs[name] = (node_id, output_name)


# ---------------------------------------------------------------------------
# The compiler
# ---------------------------------------------------------------------------


class CompositionCompiler:
    """Validates a :class:`CompositionGraph` and emits a derived spec.

    The compiler is stateless and pure: given the same registry and graph it
    always returns the same derived spec (or raises the same error). It does
    not execute anything -- execution is :class:`CompositionRuntime`'s job.
    """

    def __init__(self, registry: dict[str, CapabilitySpec]) -> None:
        if not registry:
            raise CompositionError("compiler requires a non-empty capability registry")
        self._registry = dict(registry)

    @property
    def registry(self) -> dict[str, CapabilitySpec]:
        return dict(self._registry)

    # -- public API ---------------------------------------------------------

    def compile(self, graph: CompositionGraph, *, quota: ResourceRequirements | None = None) -> CapabilitySpec:
        """Validate *graph* and return the derived :class:`CapabilitySpec`.

        Raises :class:`CompositionError` on any validation failure. The order
        of checks is structural first (so a typo is reported before a resource
        sum), then types, resources, licences, determinism.
        """
        if not graph.nodes:
            raise CompositionError("composition graph has no nodes")

        specs = self._resolve_specs(graph)
        order = self._topological_order(graph)
        self._validate_bindings(graph, specs)
        self._validate_types(graph, specs)
        resource_total = self._sum_resources(specs)
        if quota is not None:
            self._check_quota(resource_total, quota)
        derived_license = self._derive_license(specs)
        deterministic = all(s.deterministic for s in specs.values())
        derived_inputs, derived_outputs = self._derive_ports(graph, specs)
        verification = self._derive_verification(graph, specs)

        return CapabilitySpec(
            capability_id=self._derive_id(graph),
            version="1.0.0",
            description=f"composition of {len(graph.nodes)} capability(ies): "
            + ", ".join(sorted(graph.nodes.keys())),
            inputs=tuple(derived_inputs),
            outputs=tuple(derived_outputs),
            resources=resource_total,
            license=derived_license,
            deterministic=deterministic,
            cancellable=all(s.cancellable for s in specs.values()),
            idempotent=all(s.idempotent for s in specs.values()),
            undoable=all(s.undoable for s in specs.values()),
            network_required=any(s.network_required for s in specs.values()),
            verification=verification,
            tags=("composition",),
        )

    # -- validation steps ---------------------------------------------------

    def _resolve_specs(self, graph: CompositionGraph) -> dict[str, CapabilitySpec]:
        specs: dict[str, CapabilitySpec] = {}
        for node_id, node in graph.nodes.items():
            try:
                specs[node_id] = self._registry[node.capability_id]
            except KeyError:
                raise CompositionError(
                    f"node {node_id!r}: unknown capability {node.capability_id!r}"
                ) from None
        return specs

    def _topological_order(self, graph: CompositionGraph) -> list[str]:
        indegree: dict[str, int] = {nid: 0 for nid in graph.nodes}
        adj: dict[str, list[str]] = defaultdict(list)
        for node in graph.nodes.values():
            for binding in node.bindings.values():
                if binding.node_output is not None:
                    src = binding.node_output[0]
                    if src not in graph.nodes:
                        raise CompositionError(
                            f"node {node.node_id!r}: input {binding.input_name!r} "
                            f"binds to unknown node {src!r}"
                        )
                    adj[src].append(node.node_id)
                    indegree[node.node_id] += 1
        queue = deque(nid for nid, d in indegree.items() if d == 0)
        order: list[str] = []
        while queue:
            nid = queue.popleft()
            order.append(nid)
            for nxt in adj[nid]:
                indegree[nxt] -= 1
                if indegree[nxt] == 0:
                    queue.append(nxt)
        if len(order) != len(graph.nodes):
            raise CompositionError("composition graph contains a cycle")
        return order

    def _validate_bindings(self, graph: CompositionGraph, specs: dict[str, CapabilitySpec]) -> None:
        for node in graph.nodes.values():
            spec = specs[node.node_id]
            required = {i.name for i in spec.inputs if not i.optional}
            bound = set(node.bindings)
            missing = required - bound
            if missing:
                raise CompositionError(
                    f"node {node.node_id!r} ({spec.capability_id}): "
                    f"missing bindings for required input(s): {sorted(missing)}"
                )
            unknown = bound - {i.name for i in spec.inputs}
            if unknown:
                raise CompositionError(
                    f"node {node.node_id!r}: binding to unknown input(s): {sorted(unknown)}"
                )
            for binding in node.bindings.values():
                if binding.external is not None and binding.external not in graph.external_inputs:
                    raise CompositionError(
                        f"node {node.node_id!r}: input {binding.input_name!r} binds to "
                        f"unknown external input {binding.external!r}"
                    )
                if binding.node_output is not None:
                    src_id, out_name = binding.node_output
                    src_spec = specs[src_id]
                    try:
                        src_spec.output(out_name)
                    except KeyError:
                        raise CompositionError(
                            f"node {node.node_id!r}: input {binding.input_name!r} binds to "
                            f"unknown output {out_name!r} of node {src_id!r}"
                        ) from None

    def _validate_types(self, graph: CompositionGraph, specs: dict[str, CapabilitySpec]) -> None:
        for node in graph.nodes.values():
            spec = specs[node.node_id]
            for inp in spec.inputs:
                binding = node.bindings.get(inp.name)
                if binding is None:
                    continue
                if binding.external is not None:
                    produced = graph.external_inputs[binding.external]
                else:
                    src_id, out_name = binding.node_output
                    produced = specs[src_id].output(out_name).media_type
                required = inp.media_type
                if not produced.compatible_with(required):
                    raise CompositionError(
                        f"node {node.node_id!r}: input {inp.name!r} requires "
                        f"{self._mt(required)} but binding produces {self._mt(produced)}"
                    )

    def _sum_resources(self, specs: dict[str, CapabilitySpec]) -> ResourceRequirements:
        cpu = sum(s.resources.cpu_threads for s in specs.values())
        ram = sum(s.resources.ram_mb for s in specs.values())
        disk = sum(s.resources.disk_mb for s in specs.values())
        gpu = any(s.resources.gpu for s in specs.values())
        network = any(s.resources.network for s in specs.values())
        timeout = max(s.resources.timeout_seconds for s in specs.values())
        return ResourceRequirements(
            cpu_threads=cpu,
            ram_mb=ram,
            gpu=gpu,
            disk_mb=disk,
            network=network,
            timeout_seconds=timeout,
        )

    def _check_quota(self, total: ResourceRequirements, quota: ResourceRequirements) -> None:
        faults: list[str] = []
        if total.cpu_threads > quota.cpu_threads:
            faults.append(f"cpu {total.cpu_threads} > {quota.cpu_threads}")
        if total.ram_mb > quota.ram_mb:
            faults.append(f"ram {total.ram_mb}MB > {quota.ram_mb}MB")
        if total.disk_mb > quota.disk_mb:
            faults.append(f"disk {total.disk_mb}MB > {quota.disk_mb}MB")
        if total.gpu and not quota.gpu:
            faults.append("gpu required but quota has none")
        if total.network and not quota.network:
            faults.append("network required but quota forbids it")
        if faults:
            raise CompositionError("composition exceeds resource quota: " + "; ".join(faults))

    def _derive_license(self, specs: dict[str, CapabilitySpec]) -> LicenseInfo:
        licences = [s.license for s in specs.values()]
        copyleft = any(l.copyleft for l in licences)
        allow_commercial = all(l.effective_allow_commercial for l in licences)
        attribution = any(l.attribution_required for l in licences)
        spdx = "GPL-3.0-only" if copyleft else ("Proprietary" if not allow_commercial else "MIT")
        source_license = spdx
        notices = [l.notice for l in licences if l.notice]
        return LicenseInfo(
            spdx_id=spdx,
            source_license=source_license,
            model_license=None,
            copyleft=copyleft,
            allow_commercial=allow_commercial,
            attribution_required=attribution,
            notice=" | ".join(notices) if notices else "",
        )

    def _derive_ports(
        self, graph: CompositionGraph, specs: dict[str, CapabilitySpec]
    ) -> tuple[list[CapabilityInput], list[CapabilityOutput]]:
        derived_inputs = [
            CapabilityInput(name=name, media_type=mt, description=f"external input {name}")
            for name, mt in graph.external_inputs.items()
        ]
        derived_outputs: list[CapabilityOutput] = []
        for name, (node_id, out_name) in graph.expected_outputs.items():
            if node_id not in graph.nodes:
                raise CompositionError(f"expected output {name!r}: unknown node {node_id!r}")
            out = specs[node_id].output(out_name)
            derived_outputs.append(
                CapabilityOutput(name=name, media_type=out.media_type, description=out.description)
            )
        if not derived_outputs:
            raise CompositionError("composition exposes no expected outputs")
        return derived_inputs, derived_outputs

    def _derive_verification(
        self, graph: CompositionGraph, specs: dict[str, CapabilitySpec]
    ) -> VerificationContract:
        # The composition is verified by the structural properties of its final
        # outputs. We carry the most restrictive expected-properties set from
        # the nodes that produce the expected outputs.
        props: dict[str, Any] = {}
        for node_id, out_name in graph.expected_outputs.values():
            spec = specs[node_id]
            for k, v in spec.verification.expected_properties.items():
                props.setdefault(k, v)
        return VerificationContract(
            method="structural",
            expected_properties=props,
            hash_algorithm="sha256",
            description="composition verification: structural properties of exposed outputs",
        )

    def _derive_id(self, graph: CompositionGraph) -> str:
        # Deterministic id from sorted capability ids. Not a content hash -- the
        # derived spec carries its own version; the id is a stable label.
        caps = sorted({graph.nodes[n].capability_id for n in graph.nodes})
        return "nagar.composition." + ".".join(c.split(".")[-1] for c in caps)

    @staticmethod
    def _mt(mt: MediaType) -> str:
        parts = [mt.kind]
        if mt.codec:
            parts.append(mt.codec)
        if mt.container:
            parts.append(mt.container)
        return "/".join(parts)


__all__ = [
    "CompositionCompiler",
    "CompositionError",
    "CompositionGraph",
    "CompositionNode",
    "InputBinding",
]
