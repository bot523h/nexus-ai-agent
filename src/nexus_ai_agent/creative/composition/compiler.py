"""The composition compiler: a typed DAG over **canonical** capabilities.

The compiler is the one thing the composition layer is allowed to own.  It
answers four questions and refuses to answer them optimistically:

1. **Does every node name a capability the live registry actually has?**
   The registry is :func:`nexus_ai_agent.creative.packs.runtime.build_runtime_registry`
   — the same one the command bus authorizes against.  There is no second
   capability dictionary anywhere in this package.
2. **Is the dataflow well typed?**  Each node's media input port declares a
   :class:`~nexus_ai_agent.creative.studio.models.MediaContract`; a binding is
   legal only when the produced contract *satisfies* the required one.
3. **Is the graph acyclic and bound?**  A cycle, a missing required binding,
   a dangling reference or an unknown parameter is a compile error, never a
   runtime surprise.
4. **What does it cost?**  The compiler produces a :class:`ResourceEstimate`
   that distinguishes the *sequential* peak from the sum — a composition runs
   one node at a time, so the sum is not the peak.

The compiled plan is fingerprinted.  The fingerprint covers the plan **and**
the registry fingerprint, so a plan compiled against a different registry
produces a different fingerprint and cannot be replayed silently.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from pydantic import ConfigDict, ValidationError, create_model

from nexus_ai_agent.creative.studio.capabilities import CapabilityRegistry, OperationSpec
from nexus_ai_agent.creative.studio.models import MediaContract

from .contracts import (
    CompositionPlan,
    ExternalInput,
    PlanNode,
    PortBinding,
    ResourceEstimate,
    ResourceRequirements,
)

#: The typed error codes the compiler raises.  They are stable so a caller
#: can map them to a user-facing message without parsing prose.
ERROR_CODES: frozenset[str] = frozenset(
    {
        "unknown_operation",
        "capability_version_drift",
        "schema_version_drift",
        "not_composable",
        "missing_binding",
        "unknown_binding",
        "cyclic_graph",
        "type_mismatch",
        "invalid_parameters",
        "unknown_parameter",
        "registry_drift",
        "empty_plan",
    }
)


class CompositionError(ValueError):
    """A typed, caller-facing composition failure."""

    def __init__(self, code: str, detail: str = "") -> None:
        if code not in ERROR_CODES:
            raise ValueError(f"unknown composition error code: {code!r}")
        super().__init__(f"[{code}] {detail}".strip())
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class CompiledPlan:
    """A validated plan plus everything the executor needs to run it."""

    plan: CompositionPlan
    #: The deterministic execution order (topological, ties broken by node id).
    order: tuple[str, ...]
    #: The canonical operation spec of every node, resolved from the live registry.
    specs: Mapping[str, OperationSpec]
    #: ``node_id -> output port -> media contract``.
    output_contracts: Mapping[str, Mapping[str, MediaContract]]
    #: ``node_id -> input port -> media contract``.
    input_contracts: Mapping[str, Mapping[str, MediaContract]]
    estimate: ResourceEstimate
    #: ``node_id -> declared cost`` — the per-node budget the executor holds
    #: the runtime to (its timeout, its RAM, its scratch).
    requirements: Mapping[str, ResourceRequirements] = field(default_factory=dict)
    fingerprint: str = ""
    registry_fingerprint: str = ""

    def spec(self, node_id: str) -> OperationSpec:
        return self.specs[node_id]

    def node(self, node_id: str) -> PlanNode:
        return self.plan.node(node_id)


def registry_fingerprint(registry: CapabilityRegistry) -> str:
    """A stable fingerprint of the registry's operation surface.

    It covers every operation id together with its capability version and its
    schema version, so a capability that changed its contract produces a
    different fingerprint even though its id did not change.
    """
    parts: list[str] = []
    for operation_id in registry.list_operations():
        spec = registry.get_spec(operation_id)
        descriptor = registry.describe(operation_id)
        parts.append(
            f"{operation_id}@{descriptor.capability_id}#{descriptor.version}"
            f"#schema{spec.schema_version}"
            f"#perm{spec.permission_level.value}"
            f"#packs{','.join(spec.required_packs)}"
        )
    digest = hashlib.sha256("\n".join(sorted(parts)).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _params_model(spec: OperationSpec) -> type[Any]:
    """A validation model for the *caller-supplied* part of the input schema.

    The canonical input model is authoritative; this model is the same schema
    with the executor-filled fields (``media_derived_fields``) made optional.
    Everything else keeps its type, range and enum constraints, and
    ``extra="forbid"`` keeps an unknown parameter out of the worker.
    """
    base = spec.input_model
    fields: dict[str, Any] = {}
    for name, info in base.model_fields.items():
        if name in spec.media_derived_fields:
            annotation = info.annotation
            fields[name] = (
                (annotation | None) if annotation is not None else Any,
                None,
            )
        else:
            fields[name] = (info.annotation, info)
    return create_model(
        f"{spec.operation_id.replace('.', '_')}_params",
        __config__=ConfigDict(extra="forbid"),
        **fields,
    )


class CompositionCompiler:
    """Compile a :class:`CompositionPlan` against the live canonical registry."""

    def __init__(self, registry: CapabilityRegistry | None = None) -> None:
        if registry is None:
            from nexus_ai_agent.creative.packs.runtime import build_runtime_registry

            registry = build_runtime_registry()
        self._registry = registry
        self._registry_fingerprint = registry_fingerprint(registry)

    @property
    def registry(self) -> CapabilityRegistry:
        """The live registry this compiler resolves against."""
        return self._registry

    @property
    def registry_fingerprint(self) -> str:
        return self._registry_fingerprint

    # -- public API ---------------------------------------------------------

    def compile(self, plan: CompositionPlan) -> CompiledPlan:
        """Validate *plan* and return the executable :class:`CompiledPlan`."""
        if not plan.nodes:
            raise CompositionError("empty_plan", "a composition needs at least one node")

        specs: dict[str, OperationSpec] = {}
        for node in plan.nodes:
            specs[node.node_id] = self._resolve(node)

        self._check_registry_pin(plan)
        self._check_parameters(plan, specs)
        order = self._topological_order(plan)
        input_contracts = self._input_contracts(specs)
        output_contracts = self._output_contracts(specs)
        self._check_bindings(plan, specs, input_contracts, output_contracts)
        self._check_types(plan, specs, input_contracts, output_contracts)
        estimate = self.estimate_resources(plan, specs)
        # The per-node declared cost stays attached to the compiled plan so the
        # executor can hold each node to the deadline it published.
        per_node = {node.node_id: self._requirements(specs[node.node_id]) for node in plan.nodes}
        return CompiledPlan(
            plan=plan,
            order=order,
            specs=specs,
            input_contracts=input_contracts,
            output_contracts=output_contracts,
            estimate=estimate,
            requirements=per_node,
            fingerprint=self._fingerprint(plan),
            registry_fingerprint=self._registry_fingerprint,
        )

    def estimate_resources(
        self,
        plan: CompositionPlan,
        specs: Mapping[str, OperationSpec] | None = None,
    ) -> ResourceEstimate:
        """The declared cost of *plan*.

        The composition executes sequentially, so the peak resident memory is
        the **maximum** node requirement, not the sum.  The sum is reported as
        ``total_ram_mb`` (the parallel-execution figure) so the two are never
        confused.
        """
        resolved = specs or {node.node_id: self._resolve(node) for node in plan.nodes}
        requirements = [self._requirements(resolved[node.node_id]) for node in plan.nodes]
        if not requirements:  # pragma: no cover - an empty plan is refused earlier
            raise CompositionError("empty_plan", "a composition needs at least one node")
        peak = max(item.ram_mb for item in requirements)
        total = sum(item.ram_mb for item in requirements)
        return ResourceEstimate(
            peak_ram_mb=peak,
            total_ram_mb=total,
            peak_concurrent_ram_mb=peak,
            total_disk_mb=sum(item.disk_mb for item in requirements),
            scratch_mb=sum(item.scratch_mb for item in requirements),
            cpu_threads=max(item.cpu_threads for item in requirements),
            gpu_memory_mb=max(item.gpu_memory_mb for item in requirements),
            network_egress=any(item.network_egress for item in requirements),
            max_timeout_seconds=max(item.timeout_seconds for item in requirements),
            node_count=len(requirements),
        )

    # -- internals ----------------------------------------------------------

    def _resolve(self, node: PlanNode) -> OperationSpec:
        try:
            spec = self._registry.get_spec(node.operation)
        except Exception as exc:  # UnknownOperationError
            raise CompositionError(
                "unknown_operation",
                f"node {node.node_id!r} names operation {node.operation!r} which the live "
                f"NEXUS registry does not provide",
            ) from exc
        descriptor = self._registry.describe(node.operation)
        if descriptor.capability_id != node.capability_id:
            raise CompositionError(
                "capability_version_drift",
                f"node {node.node_id!r} claims capability {node.capability_id!r} but "
                f"{node.operation!r} belongs to {descriptor.capability_id!r}",
            )
        if descriptor.version != node.capability_version:
            raise CompositionError(
                "capability_version_drift",
                f"node {node.node_id!r} pins capability {node.capability_id}@"
                f"{node.capability_version} but the live registry carries "
                f"{descriptor.version}",
            )
        if spec.schema_version != node.operation_schema_version:
            raise CompositionError(
                "schema_version_drift",
                f"node {node.node_id!r} pins schema version "
                f"{node.operation_schema_version} for {node.operation!r} but the live "
                f"registry carries {spec.schema_version}",
            )
        if not spec.is_composable:
            raise CompositionError(
                "not_composable",
                f"operation {node.operation!r} declares no media ports, so it cannot "
                "participate in a typed media composition",
            )
        return spec

    def _check_registry_pin(self, plan: CompositionPlan) -> None:
        """A plan compiled against another registry must not execute silently."""
        if plan.registry_fingerprint and plan.registry_fingerprint != self._registry_fingerprint:
            raise CompositionError(
                "registry_drift",
                f"the plan was compiled against registry {plan.registry_fingerprint} but the "
                f"live registry is {self._registry_fingerprint}",
            )

    def _check_parameters(
        self,
        plan: CompositionPlan,
        specs: Mapping[str, OperationSpec],
    ) -> None:
        """Validate every node's parameters against the canonical input schema."""
        for node in plan.nodes:
            spec = specs[node.node_id]
            model = _params_model(spec)
            try:
                model.model_validate(node.params)
            except ValidationError as exc:
                unknown = sorted(
                    {
                        str(error["loc"][0])
                        for error in exc.errors()
                        if error.get("type") == "extra_forbidden"
                    }
                )
                if unknown:
                    raise CompositionError(
                        "unknown_parameter",
                        f"node {node.node_id!r} ({node.operation}) carries unknown "
                        f"parameter(s): {', '.join(unknown)}",
                    ) from exc
                raise CompositionError(
                    "invalid_parameters",
                    f"node {node.node_id!r} ({node.operation}) has invalid parameters: {exc}",
                ) from exc

    def _input_contracts(
        self, specs: Mapping[str, OperationSpec]
    ) -> dict[str, dict[str, MediaContract]]:
        return {
            node_id: {port.name: port.contract for port in spec.media_inputs}
            for node_id, spec in specs.items()
        }

    def _output_contracts(
        self, specs: Mapping[str, OperationSpec]
    ) -> dict[str, dict[str, MediaContract]]:
        return {
            node_id: {port.name: port.contract for port in spec.media_outputs}
            for node_id, spec in specs.items()
        }

    def _check_bindings(
        self,
        plan: CompositionPlan,
        specs: Mapping[str, OperationSpec],
        input_contracts: Mapping[str, Mapping[str, MediaContract]],
        output_contracts: Mapping[str, Mapping[str, MediaContract]],
    ) -> None:
        """Every required port bound exactly once, every binding resolvable."""
        for node in plan.nodes:
            spec = specs[node.node_id]
            required = {port.name for port in spec.media_inputs if port.required}
            bound = {binding.input_port for binding in node.bindings}
            missing = sorted(required - bound)
            if missing:
                raise CompositionError(
                    "missing_binding",
                    f"node {node.node_id!r} ({node.operation}) does not bind required media "
                    f"input(s): {', '.join(missing)}",
                )
            unknown_ports = sorted(bound - set(input_contracts[node.node_id]))
            if unknown_ports:
                raise CompositionError(
                    "unknown_binding",
                    f"node {node.node_id!r} ({node.operation}) binds unknown media input "
                    f"port(s): {', '.join(unknown_ports)}",
                )
            for binding in node.bindings:
                self._check_binding_target(plan, binding, output_contracts)

    def _check_binding_target(
        self,
        plan: CompositionPlan,
        binding: PortBinding,
        output_contracts: Mapping[str, Mapping[str, MediaContract]],
    ) -> None:
        if binding.source == "external":
            try:
                plan.external_input(binding.external or "")
            except KeyError as exc:
                raise CompositionError(
                    "unknown_binding",
                    f"binding {binding.input_port!r} names unknown external input "
                    f"{binding.external!r}",
                ) from exc
            return
        producer = output_contracts.get(binding.node_id or "")
        if producer is None:
            raise CompositionError(
                "unknown_binding",
                f"binding {binding.input_port!r} names unknown node {binding.node_id!r}",
            )
        if binding.output_port not in producer:
            raise CompositionError(
                "unknown_binding",
                f"binding {binding.input_port!r} names unknown output port "
                f"{binding.output_port!r} of node {binding.node_id!r}",
            )

    def _check_types(
        self,
        plan: CompositionPlan,
        specs: Mapping[str, OperationSpec],
        input_contracts: Mapping[str, Mapping[str, MediaContract]],
        output_contracts: Mapping[str, Mapping[str, MediaContract]],
    ) -> None:
        """A binding is legal only when the produced contract satisfies the required one."""
        for node in plan.nodes:
            for binding in node.bindings:
                required = input_contracts[node.node_id][binding.input_port]
                produced = self._produced_contract(plan, binding, output_contracts)
                if produced is None:  # pragma: no cover - checked earlier
                    continue
                if not required.accepts(produced):
                    raise CompositionError(
                        "type_mismatch",
                        f"node {node.node_id!r} binds {binding.input_port!r} to "
                        f"{binding.describe()}, whose media contract {produced.model_dump()} "
                        f"does not satisfy the required contract {required.model_dump()}",
                    )

    def _produced_contract(
        self,
        plan: CompositionPlan,
        binding: PortBinding,
        output_contracts: Mapping[str, Mapping[str, MediaContract]],
    ) -> MediaContract | None:
        if binding.source == "external":
            external: ExternalInput = plan.external_input(binding.external or "")
            return external.contract
        producer = output_contracts.get(binding.node_id or "")
        if producer is None:
            return None
        return producer.get(binding.output_port or "")

    def _topological_order(self, plan: CompositionPlan) -> tuple[str, ...]:
        """Kahn's algorithm with a deterministic tie-break (node id)."""
        dependencies: dict[str, set[str]] = {node.node_id: set() for node in plan.nodes}
        dependents: dict[str, set[str]] = {node.node_id: set() for node in plan.nodes}
        for node in plan.nodes:
            for binding in node.bindings:
                if binding.source != "node_output":
                    continue
                producer = binding.node_id or ""
                if producer not in dependencies:
                    raise CompositionError(
                        "unknown_binding",
                        f"node {node.node_id!r} binds to unknown node {producer!r}",
                    )
                dependencies[node.node_id].add(producer)
                dependents[producer].add(node.node_id)

        ready = sorted(node_id for node_id, deps in dependencies.items() if not deps)
        order: list[str] = []
        remaining = dict(dependencies)
        while ready:
            node_id = ready.pop(0)
            order.append(node_id)
            for dependent in sorted(dependents[node_id]):
                remaining[dependent].discard(node_id)
                if not remaining[dependent] and dependent not in order and dependent not in ready:
                    ready.append(dependent)
            ready.sort()
        if len(order) != len(plan.nodes):
            cyclic = sorted(set(dependencies) - set(order))
            raise CompositionError(
                "cyclic_graph",
                f"the composition contains a cycle through: {', '.join(cyclic)}",
            )
        return tuple(order)

    def _requirements(self, spec: OperationSpec) -> ResourceRequirements:
        """The declared cost of one canonical capability.

        The canonical contract does not carry a resource budget per operation
        (the pack manifest does, per pack), so the compiler derives a
        conservative default from the operation's media contract: a video
        operation is more expensive than a still.  This is an **estimate** and
        is labelled as such; the runtime enforces the limits it can and
        measures the rest.
        """
        contract = spec.media_contract
        if contract is None:
            return ResourceRequirements()
        if contract.kind == "video":
            return ResourceRequirements(
                cpu_threads=2,
                ram_mb=512,
                disk_mb=256,
                scratch_mb=256,
                timeout_seconds=300,
            )
        if contract.kind == "audio":
            return ResourceRequirements(
                cpu_threads=1,
                ram_mb=256,
                disk_mb=128,
                scratch_mb=128,
                timeout_seconds=180,
            )
        return ResourceRequirements(
            cpu_threads=1,
            ram_mb=256,
            disk_mb=64,
            scratch_mb=64,
            timeout_seconds=120,
        )

    def _fingerprint(self, plan: CompositionPlan) -> str:
        """``sha256:<hex>`` over the plan *and* the registry it was compiled against."""
        digest = hashlib.sha256(plan.canonical_json().encode("utf-8")).hexdigest()
        combined = hashlib.sha256(f"{digest}:{self._registry_fingerprint}".encode()).hexdigest()
        return f"sha256:{combined}"


__all__ = [
    "ERROR_CODES",
    "CompositionCompiler",
    "CompositionError",
    "CompiledPlan",
    "registry_fingerprint",
]
