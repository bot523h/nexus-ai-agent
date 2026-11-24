"""Composition compiler: typed DAG validation, Kahn schedule and JCS digest."""

from __future__ import annotations

import hashlib
import heapq
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

from nexus_ai_agent.creative.composition.contracts import (
    CapabilityContract,
    CompositionContractError,
)
from nexus_ai_agent.creative.composition.license_lattice import (
    LicenseCombination,
    LicenseRegime,
    combine_licenses,
)

__all__ = [
    "CompositionCompiler",
    "CompositionCompileError",
    "CompositionGraph",
    "CompositionPlan",
    "canonical_json",
    "sha256_digest",
]

_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class CompositionCompileError(ValueError):
    """Raised when a composition graph is ill-typed, cyclic or license-conflicted."""


def canonical_json(payload: object) -> str:
    """RFC 8785-style canonical JSON: sorted keys and no insignificant spacing."""

    try:
        return json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise CompositionCompileError("payload is not canonically serialisable") from exc


def sha256_digest(payload: object) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class CompositionGraph:
    contracts: tuple[CapabilityContract, ...]
    edges: tuple[tuple[str, str, str, str], ...]

    def __post_init__(self) -> None:
        contracts = tuple(self.contracts)
        if not contracts:
            raise CompositionCompileError("at least one capability is required")
        if any(not isinstance(contract, CapabilityContract) for contract in contracts):
            raise TypeError("contracts must contain CapabilityContract values")
        ids = [contract.capability_id for contract in contracts]
        if len(set(ids)) != len(ids):
            raise CompositionCompileError("capability ids must be unique")
        index = {contract.capability_id: contract for contract in contracts}
        edges = tuple(self.edges)
        for edge in edges:
            if len(edge) != 4:
                raise CompositionCompileError("edges must be (source, output, target, input)")
            source, output, target, input_port = edge
            if source not in index or target not in index:
                raise CompositionCompileError("edge references an unknown capability")
            if source == target:
                raise CompositionCompileError("self-composition is not allowed")
            try:
                produced = index[source].output(output).port_type
                required = index[target].input(input_port).port_type
            except CompositionContractError as exc:
                raise CompositionCompileError(str(exc)) from exc
            if not produced.is_subtype_of(required):
                raise CompositionCompileError(
                    f"type mismatch on {source}.{output} -> {target}.{input_port}"
                )
        consumed = [(target, input_port) for _source, _output, target, input_port in edges]
        if len(set(consumed)) != len(consumed):
            raise CompositionCompileError("an input port may be connected only once")
        object.__setattr__(self, "contracts", contracts)
        object.__setattr__(self, "edges", edges)
        if len(self.topological_order()) != len(contracts):
            raise CompositionCompileError("composition graph must be acyclic")

    def contract(self, capability_id: str) -> CapabilityContract:
        for contract in self.contracts:
            if contract.capability_id == capability_id:
                return contract
        raise CompositionCompileError(f"unknown capability {capability_id!r}")

    def topological_order(self) -> tuple[str, ...]:
        contracts = self.contracts
        indegree = {contract.capability_id: 0 for contract in contracts}
        children: dict[str, list[str]] = {contract.capability_id: [] for contract in contracts}
        for source, _output, target, _input in self.edges:
            indegree[target] += 1
            children[source].append(target)
        ready = [node for node, degree in indegree.items() if degree == 0]
        heapq.heapify(ready)
        order: list[str] = []
        while ready:
            node = heapq.heappop(ready)
            order.append(node)
            for child in sorted(children[node]):
                indegree[child] -= 1
                if indegree[child] == 0:
                    heapq.heappush(ready, child)
        return tuple(order)

    def incoming(self, capability_id: str) -> Mapping[str, tuple[str, str]]:
        """Input port -> (source capability, source output port)."""

        mapping = {}
        for source, output, target, input_port in self.edges:
            if target == capability_id:
                mapping[input_port] = (source, output)
        return MappingProxyType(mapping)


@dataclass(frozen=True, slots=True)
class CompositionPlan:
    order: tuple[str, ...]
    plan_digest: str
    node_digests: Mapping[str, str]
    edges: tuple[tuple[str, str, str, str], ...]
    licenses: LicenseCombination
    contracts: Mapping[str, CapabilityContract]

    def __post_init__(self) -> None:
        order = tuple(self.order)
        if not order or len(set(order)) != len(order):
            raise CompositionCompileError("order must be a non-empty unique sequence")
        if not isinstance(self.plan_digest, str) or not _DIGEST_PATTERN.match(self.plan_digest):
            raise CompositionCompileError("plan_digest must be a 64-character lowercase hex digest")
        digests = dict(self.node_digests)
        if set(digests) != set(order):
            raise CompositionCompileError("node digests must cover every scheduled node")
        for digest in digests.values():
            if not isinstance(digest, str) or not _DIGEST_PATTERN.match(digest):
                raise CompositionCompileError("node digests must be SHA-256 hex strings")
        contracts = dict(self.contracts)
        if set(contracts) != set(order):
            raise CompositionCompileError("contracts must cover every scheduled node")
        if not isinstance(self.licenses, LicenseCombination):
            raise TypeError("licenses must be a LicenseCombination")
        object.__setattr__(self, "order", order)
        object.__setattr__(self, "node_digests", MappingProxyType(digests))
        object.__setattr__(self, "contracts", MappingProxyType(contracts))

    def contract(self, capability_id: str) -> CapabilityContract:
        try:
            return self.contracts[capability_id]
        except KeyError as exc:
            raise CompositionCompileError(f"unknown capability {capability_id!r}") from exc


class CompositionCompiler:
    """Validate, schedule and seal a typed composition plan."""

    def compile(
        self,
        contracts: Sequence[CapabilityContract],
        edges: Sequence[tuple[str, str, str, str]],
        licenses: Mapping[str, LicenseRegime],
        *,
        commercial_intent: bool = False,
    ) -> CompositionPlan:
        graph = CompositionGraph(tuple(contracts), tuple(edges))
        order = graph.topological_order()
        if set(licenses) != set(order):
            raise CompositionCompileError("every capability needs exactly one license regime")
        combination = combine_licenses(
            tuple(licenses[node] for node in order), commercial_intent=commercial_intent
        )
        if not combination.compatible:
            raise CompositionCompileError("license combination is incompatible")
        node_digests = {
            node: sha256_digest(graph.contract(node).canonical_payload()) for node in order
        }
        payload = {
            "order": list(order),
            "nodes": [
                {
                    "capability_id": node,
                    "digest": node_digests[node],
                    "version": graph.contract(node).version,
                }
                for node in order
            ],
            "edges": [list(edge) for edge in graph.edges],
            "license_supremum": combination.supremum.value,
            "commercial_intent": commercial_intent,
        }
        digest = sha256_digest(payload)
        return CompositionPlan(
            order=order,
            plan_digest=digest,
            node_digests=node_digests,
            edges=graph.edges,
            licenses=combination,
            contracts={node: graph.contract(node) for node in order},
        )
