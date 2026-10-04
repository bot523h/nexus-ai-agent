"""Composition and lineage recording for the existing creative execution path.

This module is deliberately not an executor.  It seals meaning into one typed
plan and records graph lineage; the application submits that plan to the
existing durable job queue, whose worker reaches policy, CommandBus, rendering,
and queue-owned verification.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from nexus_ai_agent.creative.intelligence.ir import CreativeWork

if TYPE_CHECKING:
    from nexus_ai_agent.creative.spine.strategy import StrategyProposal
from nexus_ai_agent.creative.spine.compiler import (
    CapabilityCompiler,
    CreativeWorkCompiler,
    GraphIntentPlanner,
    IntentPlanner,
)
from nexus_ai_agent.creative.spine.models import (
    CompiledIntent,
    CreativeGraph,
    GraphNode,
    Intent,
)
from nexus_ai_agent.creative.studio.capabilities import CapabilityRegistry


@dataclass(frozen=True)
class PreparedCreativeWork:
    """Validated intent, strategy, authoring document, plan, and graph anchors."""

    intent: Intent
    strategy: StrategyProposal
    work: CreativeWork
    compiled: CompiledIntent
    intent_node: GraphNode
    strategy_node: GraphNode
    work_node: GraphNode
    plan_node: GraphNode
    capability_nodes: tuple[GraphNode, ...]


class CreativeWorkLineage:
    """Single composition point for CreativeWork compilation and trace lineage."""

    def __init__(
        self,
        graph: CreativeGraph,
        *,
        compiler: CapabilityCompiler | None = None,
        planner: IntentPlanner | None = None,
    ) -> None:
        self._graph = graph
        self._compiler = compiler or CreativeWorkCompiler()
        self._planner = planner or GraphIntentPlanner()

    @property
    def graph(self) -> CreativeGraph:
        return self._graph

    def prepare(
        self,
        intent: Intent,
        strategy: StrategyProposal,
        work: CreativeWork,
        registry: CapabilityRegistry,
    ) -> PreparedCreativeWork:
        """Validate and compile one work, then record the immutable lineage."""
        if intent.project_id != self._graph.project_id:
            raise ValueError("intent targets a different project than the creative graph")
        compiled = self._compiler.compile(work, intent, registry)
        intent_node = self._graph.add_node(
            "intent",
            label=intent.goal,
            data={
                "intent_id": intent.intent_id,
                "request_id": intent.request_id,
                "project_id": intent.project_id,
                "objective": intent.objective,
                "confidence": intent.confidence,
                "ambiguity_state": intent.ambiguity_state,
                "unresolved_requirements": list(intent.unresolved_requirements),
                "constraints": list(intent.constraints),
            },
        )
        strategy_node = self._graph.add_node(
            "strategy",
            label=strategy.objective,
            data=strategy.model_dump(mode="json"),
            parents=(intent_node.node_id,),
        )
        work_node = self._graph.add_node(
            "creative_work",
            label=work.work_id,
            data={
                "work_id": work.work_id,
                "snapshot_sha256": _sha256(work.to_canonical_json()),
                "constraint_ids": [constraint.constraint_id for constraint in work.constraints],
            },
            parents=(strategy_node.node_id,),
        )
        plan_node = self._graph.add_node(
            "plan",
            label=compiled.plan_id,
            data=compiled.model_dump(mode="json"),
            parents=(work_node.node_id,),
        )
        capability_nodes = self._planner.plan(self._graph, plan_node, compiled)
        return PreparedCreativeWork(
            intent=intent,
            strategy=strategy,
            work=work,
            compiled=compiled,
            intent_node=intent_node,
            strategy_node=strategy_node,
            work_node=work_node,
            plan_node=plan_node,
            capability_nodes=tuple(capability_nodes),
        )

    def record_receipt(
        self,
        run: PreparedCreativeWork,
        *,
        receipt: dict[str, Any],
        verification: dict[str, Any],
    ) -> tuple[GraphNode, GraphNode, GraphNode, GraphNode]:
        """Append queue facts to this request-local graph; do not persist them."""
        job_node = self._graph.add_node(
            "job",
            label=str(receipt.get("job_id", "")),
            data={
                "job_id": receipt.get("job_id"),
                "command_id": receipt.get("command_id"),
                "transaction_id": receipt.get("transaction_id"),
                "execution_status": receipt.get("execution_status"),
            },
            parents=(run.plan_node.node_id,),
        )
        artifact_node = self._graph.add_node(
            "artifact",
            label=str(receipt.get("artifact_sha256", "")),
            data={
                "artifact_path": receipt.get("artifact_path"),
                "sha256": receipt.get("artifact_sha256"),
                "size_bytes": receipt.get("size_bytes"),
                "duration_us": receipt.get("duration_us"),
                "output_asset_id": receipt.get("output_asset_id"),
            },
            parents=(job_node.node_id,),
        )
        verification_node = self._graph.add_node(
            "verification",
            label=str(verification.get("status", "")),
            data=verification,
            parents=(artifact_node.node_id,),
        )
        receipt_node = self._graph.add_node(
            "receipt",
            label=str(receipt.get("job_id", "")),
            data=receipt,
            parents=(verification_node.node_id,),
        )
        return job_node, artifact_node, verification_node, receipt_node


def _sha256(value: str) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


__all__ = ["CreativeWorkLineage", "PreparedCreativeWork"]
