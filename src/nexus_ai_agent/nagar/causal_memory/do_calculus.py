"""Finite Structural Causal Models, d-separation and Pearl's do-calculus.

The graph is a DAG with categorical domains and validated conditional
probability tables.  Interventions are computed twice: by explicit graph
mutilation, and — when the back-door criterion holds — by the adjustment
formula, which the tests prove equal.  A read-only causal memory records
episodes and exposes Ebbinghaus-Wixted retention.
"""

from __future__ import annotations

import heapq
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import product
from types import MappingProxyType

__all__ = [
    "AverageCausalEffect",
    "CausalEffectError",
    "CausalGraph",
    "CausalMemory",
    "ConditionalProbabilityTable",
    "Episode",
    "ReadOnlyCausalMemory",
    "RetentionRecord",
    "StructuralCausalModel",
    "adjustment_distribution",
    "average_causal_effect",
    "is_d_separated",
    "is_valid_backdoor_set",
    "retention",
]

_PROBABILITY_TOLERANCE = 1e-9


class CausalEffectError(ValueError):
    """Raised when a causal model or query violates an identifying assumption."""


@dataclass(frozen=True, slots=True)
class CausalGraph:
    nodes: tuple[str, ...]
    edges: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        nodes = tuple(self.nodes)
        if not nodes or len(set(nodes)) != len(nodes):
            raise CausalEffectError("nodes must be non-empty and unique")
        if any(not isinstance(node, str) or not node for node in nodes):
            raise CausalEffectError("node names must be non-empty strings")
        known = set(nodes)
        edges = tuple((parent, child) for parent, child in self.edges)
        for parent, child in edges:
            if parent not in known or child not in known:
                raise CausalEffectError("edges must reference declared nodes")
            if parent == child:
                raise CausalEffectError("self-loops are not allowed")
        if len(set(edges)) != len(edges):
            raise CausalEffectError("duplicate edges are not allowed")
        object.__setattr__(self, "nodes", nodes)
        object.__setattr__(self, "edges", edges)
        if not self._is_acyclic():
            raise CausalEffectError("graph must be acyclic")

    def _is_acyclic(self) -> bool:
        indegree = {node: 0 for node in self.nodes}
        children: dict[str, list[str]] = {node: [] for node in self.nodes}
        for parent, child in self.edges:
            indegree[child] += 1
            children[parent].append(child)
        ready = [node for node in self.nodes if indegree[node] == 0]
        heapq.heapify(ready)
        visited = 0
        while ready:
            node = heapq.heappop(ready)
            visited += 1
            for child in sorted(children[node]):
                indegree[child] -= 1
                if indegree[child] == 0:
                    heapq.heappush(ready, child)
        return visited == len(self.nodes)

    def parents(self, node: str) -> tuple[str, ...]:
        self._require(node)
        return tuple(sorted(parent for parent, child in self.edges if child == node))

    def children(self, node: str) -> tuple[str, ...]:
        self._require(node)
        return tuple(sorted(child for parent, child in self.edges if parent == node))

    def ancestors(self, nodes: Sequence[str]) -> frozenset[str]:
        for node in nodes:
            self._require(node)
        result: set[str] = set()
        frontier = list(nodes)
        while frontier:
            node = frontier.pop()
            if node in result:
                continue
            result.add(node)
            frontier.extend(self.parents(node))
        return frozenset(result)

    def descendants(self, node: str) -> frozenset[str]:
        self._require(node)
        result: set[str] = set()
        frontier = list(self.children(node))
        while frontier:
            current = frontier.pop()
            if current in result:
                continue
            result.add(current)
            frontier.extend(self.children(current))
        return frozenset(result)

    def topological_order(self) -> tuple[str, ...]:
        indegree = {node: 0 for node in self.nodes}
        children: dict[str, list[str]] = {node: [] for node in self.nodes}
        for parent, child in self.edges:
            indegree[child] += 1
            children[parent].append(child)
        ready = [node for node in self.nodes if indegree[node] == 0]
        heapq.heapify(ready)
        order = []
        while ready:
            node = heapq.heappop(ready)
            order.append(node)
            for child in sorted(children[node]):
                indegree[child] -= 1
                if indegree[child] == 0:
                    heapq.heappush(ready, child)
        return tuple(order)

    def without_outgoing(self, node: str) -> CausalGraph:
        self._require(node)
        return CausalGraph(self.nodes, tuple(edge for edge in self.edges if edge[0] != node))

    def _require(self, node: str) -> None:
        if node not in self.nodes:
            raise CausalEffectError(f"unknown node {node!r}")


@dataclass(frozen=True, slots=True)
class ConditionalProbabilityTable:
    variable: str
    parents: tuple[str, ...]
    table: Mapping[tuple[str, ...], Mapping[str, float]]

    def __post_init__(self) -> None:
        if not isinstance(self.variable, str) or not self.variable:
            raise CausalEffectError("variable must be a non-empty string")
        parents = tuple(self.parents)
        if parents != tuple(sorted(parents)) or len(set(parents)) != len(parents):
            raise CausalEffectError("parents must be sorted and unique")
        frozen: dict[tuple[str, ...], Mapping[str, float]] = {}
        for assignment, distribution in self.table.items():
            key = tuple(assignment)
            if len(key) != len(parents):
                raise CausalEffectError("parent assignments must match parent count")
            values: dict[str, float] = {}
            for value, probability in distribution.items():
                if not isinstance(value, str) or not value:
                    raise CausalEffectError("values must be non-empty strings")
                number = float(probability)
                if not math.isfinite(number) or not 0.0 <= number <= 1.0:
                    raise CausalEffectError("probabilities must lie inside [0, 1]")
                values[value] = number
            total = sum(values.values())
            if abs(total - 1.0) > _PROBABILITY_TOLERANCE:
                raise CausalEffectError("each conditional distribution must sum to 1")
            frozen[key] = MappingProxyType(values)
        if not frozen:
            raise CausalEffectError("table must not be empty")
        object.__setattr__(self, "parents", parents)
        object.__setattr__(self, "table", MappingProxyType(frozen))

    def probability(self, value: str, assignment: Mapping[str, str]) -> float:
        try:
            key = tuple(assignment[parent] for parent in self.parents)
            return self.table[key][value]
        except KeyError as exc:
            raise CausalEffectError("assignment or value is outside the table") from exc


@dataclass(frozen=True, slots=True)
class StructuralCausalModel:
    graph: CausalGraph
    cpts: Mapping[str, ConditionalProbabilityTable]
    domains: Mapping[str, tuple[str, ...]]

    def __post_init__(self) -> None:
        if not isinstance(self.graph, CausalGraph):
            raise TypeError("graph must be a CausalGraph")
        cpts = dict(self.cpts)
        domains = {node: tuple(values) for node, values in self.domains.items()}
        if set(cpts) != set(self.graph.nodes) or set(domains) != set(self.graph.nodes):
            raise CausalEffectError("every node needs a CPT and a domain")
        for node, values in domains.items():
            if not values or len(set(values)) != len(values):
                raise CausalEffectError("domains must be non-empty with unique values")
            if any(not isinstance(value, str) or not value for value in values):
                raise CausalEffectError("domain values must be non-empty strings")
            cpt = cpts[node]
            if not isinstance(cpt, ConditionalProbabilityTable):
                raise TypeError("cpts must contain ConditionalProbabilityTable values")
            if cpt.variable != node:
                raise CausalEffectError("CPT variable must match its node")
            if cpt.parents != self.graph.parents(node):
                raise CausalEffectError("CPT parents must match the graph")
            for distribution in cpt.table.values():
                if not set(distribution) <= set(values):
                    raise CausalEffectError("CPT values must lie inside the domain")
        object.__setattr__(self, "cpts", MappingProxyType(cpts))
        object.__setattr__(self, "domains", MappingProxyType(domains))

    def _enumerate(self, fixed: Mapping[str, str]) -> list[dict[str, str]]:
        for node, value in fixed.items():
            if node not in self.domains:
                raise CausalEffectError(f"unknown variable {node!r}")
            if value not in self.domains[node]:
                raise CausalEffectError(f"value {value!r} is outside the domain of {node}")
        variables = [node for node in self.graph.nodes if node not in fixed]
        assignments: list[dict[str, str]] = []
        for combination in product(*(self.domains[node] for node in variables)):
            assignment = dict(fixed)
            assignment.update(zip(variables, combination, strict=True))
            assignments.append(assignment)
        return assignments

    def joint(self, assignment: Mapping[str, str]) -> float:
        if set(assignment) != set(self.graph.nodes):
            raise CausalEffectError("joint needs a complete assignment")
        probability = 1.0
        for node in self.graph.topological_order():
            probability *= self.cpts[node].probability(assignment[node], assignment)
        return probability

    def observational_distribution(
        self, target: str, evidence: Mapping[str, str] | None = None
    ) -> Mapping[str, float]:
        if target not in self.domains:
            raise CausalEffectError(f"unknown target {target!r}")
        fixed = dict(evidence or {})
        if target in fixed:
            raise CausalEffectError("target must not be observed as evidence")
        weights = {value: 0.0 for value in self.domains[target]}
        for assignment in self._enumerate(fixed):
            weights[assignment[target]] += self.joint(assignment)
        return MappingProxyType(_normalise(weights))

    def interventional_distribution(
        self,
        target: str,
        treatment: Mapping[str, str],
        evidence: Mapping[str, str] | None = None,
    ) -> Mapping[str, float]:
        if target not in self.domains:
            raise CausalEffectError(f"unknown target {target!r}")
        if not treatment:
            raise CausalEffectError("treatment must not be empty")
        fixed = dict(treatment)
        for node, value in (evidence or {}).items():
            if node in fixed and fixed[node] != value:
                raise CausalEffectError("evidence contradicts the treatment")
            fixed[node] = value
        weights = {value: 0.0 for value in self.domains[target]}
        for assignment in self._enumerate(fixed):
            weight = 1.0
            for node in self.graph.nodes:
                if node in treatment:
                    continue
                weight *= self.cpts[node].probability(assignment[node], assignment)
            weights[assignment[target]] += weight
        return MappingProxyType(_normalise(weights))


def _normalise(weights: Mapping[str, float]) -> dict[str, float]:
    total = sum(weights.values())
    if total <= 0.0:  # pragma: no cover - valid CPTs guarantee positive mass
        raise CausalEffectError("distribution has zero mass")
    return {value: weight / total for value, weight in weights.items()}


def is_d_separated(graph: CausalGraph, first: str, second: str, observed: set[str]) -> bool:
    """Exact d-separation by ancestral moralisation."""

    if not isinstance(graph, CausalGraph):
        raise TypeError("graph must be a CausalGraph")
    for node in {first, second} | set(observed):
        if node not in graph.nodes:
            raise CausalEffectError(f"unknown node {node!r}")
    if first == second:
        return False
    relevant = graph.ancestors((first, second, *observed))
    adjacency: dict[str, set[str]] = {node: set() for node in relevant}
    for parent, child in graph.edges:
        if parent in relevant and child in relevant:
            adjacency[parent].add(child)
            adjacency[child].add(parent)
    for node in relevant:
        parents = [parent for parent in graph.parents(node) if parent in relevant]
        for left in parents:
            for right in parents:
                if left != right:
                    adjacency[left].add(right)
    for node in observed:
        adjacency.pop(node, None)
        for neighbours in adjacency.values():
            neighbours.discard(node)
    if first not in adjacency or second not in adjacency:
        return True
    frontier = [first]
    seen = {first}
    while frontier:
        node = frontier.pop()
        if node == second:
            return False
        for neighbour in adjacency[node]:
            if neighbour not in seen:
                seen.add(neighbour)
                frontier.append(neighbour)
    return True


def is_valid_backdoor_set(
    graph: CausalGraph, treatment: str, outcome: str, adjustment: set[str]
) -> bool:
    """Pearl's back-door criterion on ``G_underline{X}``."""

    for node in {treatment, outcome} | set(adjustment):
        if node not in graph.nodes:
            raise CausalEffectError(f"unknown node {node!r}")
    if treatment == outcome:
        raise CausalEffectError("treatment and outcome must differ")
    if treatment in adjustment or outcome in adjustment:
        return False
    descendants = graph.descendants(treatment)
    if any(node in descendants for node in adjustment):
        return False
    mutilated = graph.without_outgoing(treatment)
    return is_d_separated(mutilated, treatment, outcome, set(adjustment))


def adjustment_distribution(
    model: StructuralCausalModel,
    treatment: str,
    value: str,
    outcome: str,
    adjustment: set[str],
) -> Mapping[str, float]:
    """Pearl's back-door adjustment ``sum_z P(Y|X=x,Z=z) P(Z=z)``."""

    if not is_valid_backdoor_set(model.graph, treatment, outcome, adjustment):
        raise CausalEffectError("adjustment set does not satisfy the back-door criterion")
    if value not in model.domains[treatment]:
        raise CausalEffectError("treatment value is outside the domain")
    adjusted = {candidate: 0.0 for candidate in model.domains[outcome]}
    ordered = tuple(sorted(adjustment))
    for combination in product(*(model.domains[node] for node in ordered)):
        fixed_z = dict(zip(ordered, combination, strict=True))
        p_z = sum(model.joint(assignment) for assignment in model._enumerate(fixed_z))
        conditional = {candidate: 0.0 for candidate in model.domains[outcome]}
        denominator = 0.0
        for assignment in model._enumerate({**fixed_z, treatment: value}):
            probability = model.joint(assignment)
            conditional[assignment[outcome]] += probability
            denominator += probability
        if denominator <= 0.0:  # pragma: no cover - valid CPTs guarantee positive mass
            continue
        for candidate, probability in conditional.items():
            adjusted[candidate] += (probability / denominator) * p_z
    return MappingProxyType(_normalise(adjusted))


@dataclass(frozen=True, slots=True)
class AverageCausalEffect:
    treated_mean: float
    control_mean: float
    ace: float

    def __post_init__(self) -> None:
        for name in ("treated_mean", "control_mean", "ace"):
            value = getattr(self, name)
            if not math.isfinite(value):
                raise CausalEffectError(f"{name} must be finite")


def average_causal_effect(
    model: StructuralCausalModel,
    treatment: str,
    outcome: str,
    treated_value: str,
    control_value: str,
) -> AverageCausalEffect:
    treated = model.interventional_distribution(outcome, {treatment: treated_value})
    control = model.interventional_distribution(outcome, {treatment: control_value})
    treated_mean = sum(float(value) * probability for value, probability in treated.items())
    control_mean = sum(float(value) * probability for value, probability in control.items())
    return AverageCausalEffect(treated_mean, control_mean, treated_mean - control_mean)


@dataclass(frozen=True, slots=True)
class Episode:
    episode_id: str
    utility: float
    observed_at_days: float
    retrievals: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.episode_id, str) or not self.episode_id:
            raise CausalEffectError("episode_id must be a non-empty string")
        if not math.isfinite(self.utility) or self.utility < 0.0:
            raise CausalEffectError("utility must be finite and non-negative")
        if not math.isfinite(self.observed_at_days) or self.observed_at_days < 0.0:
            raise CausalEffectError("observed_at_days must be finite and non-negative")
        if isinstance(self.retrievals, bool) or not isinstance(self.retrievals, int):
            raise TypeError("retrievals must be an int")
        if self.retrievals < 0:
            raise CausalEffectError("retrievals must be non-negative")


def retention(
    episode: Episode,
    now_days: float,
    *,
    stability: float = 1.0,
    alpha: float = 0.5,
    beta: float = 1.0,
) -> float:
    """Ebbinghaus-Wixted power-exponential retention with spaced retrievals."""

    if not isinstance(episode, Episode):
        raise TypeError("episode must be an Episode")
    if not math.isfinite(now_days) or now_days < episode.observed_at_days:
        raise CausalEffectError("now_days must be finite and not in the past")
    for label, value in (("stability", stability), ("alpha", alpha), ("beta", beta)):
        if not math.isfinite(value) or value <= 0.0:
            raise CausalEffectError(f"{label} must be finite and positive")
    elapsed = now_days - episode.observed_at_days
    stabilised = stability * math.pow(1.0 + alpha, episode.retrievals)
    value = math.exp(-math.pow(elapsed / stabilised, beta))
    return max(0.0, min(1.0, value))


@dataclass(frozen=True, slots=True)
class RetentionRecord:
    episode_id: str
    retention: float
    elapsed_days: float
    retrievals: int

    def __post_init__(self) -> None:
        if not math.isfinite(self.retention) or not 0.0 <= self.retention <= 1.0:
            raise CausalEffectError("retention must lie inside [0, 1]")
        if not math.isfinite(self.elapsed_days) or self.elapsed_days < 0.0:
            raise CausalEffectError("elapsed_days must be finite and non-negative")
        if isinstance(self.retrievals, bool) or not isinstance(self.retrievals, int):
            raise TypeError("retrievals must be an int")


@dataclass(frozen=True, slots=True)
class CausalMemory:
    """Append-only episodic memory; ``record`` returns a new value."""

    episodes: tuple[Episode, ...] = ()

    def __post_init__(self) -> None:
        episodes = tuple(self.episodes)
        if any(not isinstance(episode, Episode) for episode in episodes):
            raise TypeError("episodes must contain Episode values")
        if len({episode.episode_id for episode in episodes}) != len(episodes):
            raise CausalEffectError("episode ids must be unique")
        object.__setattr__(self, "episodes", episodes)

    def record(self, episode: Episode) -> CausalMemory:
        if not isinstance(episode, Episode):
            raise TypeError("episode must be an Episode")
        return CausalMemory(self.episodes + (episode,))

    def get(self, episode_id: str) -> Episode | None:
        for episode in self.episodes:
            if episode.episode_id == episode_id:
                return episode
        return None

    def retention_records(
        self,
        now_days: float,
        *,
        stability: float = 1.0,
        alpha: float = 0.5,
        beta: float = 1.0,
    ) -> tuple[RetentionRecord, ...]:
        records = []
        for episode in self.episodes:
            records.append(
                RetentionRecord(
                    episode_id=episode.episode_id,
                    retention=retention(
                        episode,
                        now_days,
                        stability=stability,
                        alpha=alpha,
                        beta=beta,
                    ),
                    elapsed_days=now_days - episode.observed_at_days,
                    retrievals=episode.retrievals,
                )
            )
        return tuple(records)

    def query(
        self,
        now_days: float,
        *,
        minimum_retention: float = 0.5,
        stability: float = 1.0,
        alpha: float = 0.5,
        beta: float = 1.0,
    ) -> tuple[Episode, ...]:
        if not 0.0 <= minimum_retention <= 1.0:
            raise CausalEffectError("minimum_retention must lie inside [0, 1]")
        return tuple(
            episode
            for episode in self.episodes
            if retention(episode, now_days, stability=stability, alpha=alpha, beta=beta)
            >= minimum_retention
        )


class ReadOnlyCausalMemory:
    """Capability proxy: read-only view passed to the director."""

    _memory: CausalMemory
    __slots__ = ("_memory",)

    def __init__(self, memory: CausalMemory) -> None:
        if not isinstance(memory, CausalMemory):
            raise TypeError("memory must be a CausalMemory")
        object.__setattr__(self, "_memory", memory)

    @property
    def episodes(self) -> tuple[Episode, ...]:
        return self._memory.episodes

    def get(self, episode_id: str) -> Episode | None:
        return self._memory.get(episode_id)

    def retention_records(
        self,
        now_days: float,
        *,
        stability: float = 1.0,
        alpha: float = 0.5,
        beta: float = 1.0,
    ) -> tuple[RetentionRecord, ...]:
        return self._memory.retention_records(now_days, stability=stability, alpha=alpha, beta=beta)

    def query(
        self,
        now_days: float,
        *,
        minimum_retention: float = 0.5,
        stability: float = 1.0,
        alpha: float = 0.5,
        beta: float = 1.0,
    ) -> tuple[Episode, ...]:
        return self._memory.query(
            now_days,
            minimum_retention=minimum_retention,
            stability=stability,
            alpha=alpha,
            beta=beta,
        )

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError(f"ReadOnlyCausalMemory does not allow setting {name!r}")
