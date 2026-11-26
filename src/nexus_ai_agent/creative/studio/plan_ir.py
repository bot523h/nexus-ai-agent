"""Versioned, immutable and bounded DAG admission representation.

This representation contains command digests, never executable callbacks. Hashes
identify content only; neither a hash nor a node grants execution authority.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from nexus_ai_agent.creative.studio.plan_identifiers import validate_plan_identity

MAX_NODES = 256
MAX_DEPTH = 64
MAX_EDGES = 4096


@dataclass(frozen=True)
class PlanNode:
    node_id: str
    operation: str
    command_digest: str
    dependencies: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        validate_plan_identity(self.node_id, name="node_id")
        validate_plan_identity(self.operation, name="operation")
        if (
            not isinstance(self.command_digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", self.command_digest) is None
        ):
            raise ValueError("command_digest must be lowercase SHA-256")
        if not isinstance(self.dependencies, tuple) or len(self.dependencies) > MAX_NODES:
            raise ValueError("dependencies must be a bounded tuple")
        for dependency in self.dependencies:
            validate_plan_identity(dependency, name="dependency")
        if len(set(self.dependencies)) != len(self.dependencies):
            raise ValueError("duplicate dependency")


@dataclass(frozen=True)
class PlanIR:
    project_id: str
    plan_id: str
    nodes: tuple[PlanNode, ...]
    version: int = 1

    def __post_init__(self) -> None:
        validate_plan_identity(self.project_id, name="project_id")
        validate_plan_identity(self.plan_id, name="plan_id")
        if type(self.version) is not int or self.version != 1:
            raise ValueError("unsupported plan IR version")
        if not isinstance(self.nodes, tuple) or not 1 <= len(self.nodes) <= MAX_NODES:
            raise ValueError("plan node count is outside bounds")
        if any(not isinstance(node, PlanNode) for node in self.nodes):
            raise ValueError("plan requires typed nodes")
        self.topological_nodes()

    def topological_nodes(self) -> tuple[PlanNode, ...]:
        by_id = {node.node_id: node for node in self.nodes}
        if len(by_id) != len(self.nodes):
            raise ValueError("duplicate node identity")
        if sum(len(node.dependencies) for node in self.nodes) > MAX_EDGES:
            raise ValueError("plan edge limit exceeded")
        children: dict[str, list[str]] = {key: [] for key in by_id}
        remaining = {node.node_id: len(node.dependencies) for node in self.nodes}
        depth = dict.fromkeys(by_id, 1)
        for node in self.nodes:
            for dependency in node.dependencies:
                if dependency not in by_id:
                    raise ValueError("unknown dependency")
                children[dependency].append(node.node_id)
        ready = [key for key, count in remaining.items() if count == 0]
        ordered: list[PlanNode] = []
        while ready:
            ready.sort(reverse=True)
            key = ready.pop()
            ordered.append(by_id[key])
            for child in children[key]:
                depth[child] = max(depth[child], depth[key] + 1)
                if depth[child] > MAX_DEPTH:
                    raise ValueError("plan depth limit exceeded")
                remaining[child] -= 1
                if remaining[child] == 0:
                    ready.append(child)
        if len(ordered) != len(self.nodes):
            raise ValueError("plan contains a cycle")
        return tuple(ordered)

    def canonical_bytes(self) -> bytes:
        body = {
            "version": self.version,
            "project_id": self.project_id,
            "plan_id": self.plan_id,
            "nodes": [
                {
                    "id": node.node_id,
                    "operation": node.operation,
                    "command_digest": node.command_digest,
                    "dependencies": sorted(node.dependencies),
                }
                for node in sorted(self.nodes, key=lambda item: item.node_id)
            ],
        }
        return json.dumps(
            body, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False
        ).encode("ascii")

    @property
    def plan_hash(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()
