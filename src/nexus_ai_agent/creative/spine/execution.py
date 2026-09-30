"""The spine: intent -> compiled commands -> bus -> artifact + lineage/evidence.

:class:`CreativeExecutionSpine` is the composition point that turns a user's
intent into authorized, typed studio commands, runs them through the single
write path (:class:`~nexus_ai_agent.creative.studio.bus.CommandBus`), and
records the result as an artifact with lineage and evidence in a
:class:`CreativeGraph`.

It does **not** bypass the bus: every step still passes Policy and Authority, so
``Intent != Authority`` and ``Capability != Authorization`` hold. A command the
bus refuses fails the run and leaves no artifact -- there is no second write
path.
"""

from __future__ import annotations

from dataclasses import dataclass

from nexus_ai_agent.creative.spine.compiler import (
    CapabilityCompiler,
    GraphIntentPlanner,
    IntentPlanner,
    RulesCapabilityCompiler,
)
from nexus_ai_agent.creative.spine.models import (
    ArtifactRecord,
    CompiledIntent,
    CreativeGraph,
    EvidenceRecord,
    Intent,
    IntentResolver,
    PlannedOperation,
    RulesIntentResolver,
)
from nexus_ai_agent.creative.studio.bus import CommandBus
from nexus_ai_agent.creative.studio.models import (
    COMMAND_SCHEMA_VERSION,
    ActorIdentity,
    CommandProvenance,
    CommandResult,
    TypedCommand,
)


@dataclass(frozen=True)
class SpineRun:
    """Everything one intent produced: the plan, the result, the artifact, evidence."""

    intent: Intent
    compiled: CompiledIntent
    result: CommandResult
    artifact: ArtifactRecord
    evidence: EvidenceRecord


class CreativeExecutionSpine:
    """Intent-first execution over one bus and one creative graph.

    The spine is the *only* component that compiles an intent into commands; it
    reuses the bus's own registry so a plan can never target an operation the
    bus would reject as unknown.

    When an ``actor`` is supplied the spine emits schema-2 commands (explicit
    actor + provenance), which is what a bus configured with a trusted
    :class:`~nexus_ai_agent.creative.studio.authorization.ProjectAuthorizer`
    requires. Without an actor it emits legacy claim-less commands, which the
    bus only accepts under its deprecated implicit-local-trust path.
    """

    def __init__(
        self,
        bus: CommandBus,
        graph: CreativeGraph,
        *,
        actor: ActorIdentity | None = None,
        resolver: IntentResolver | None = None,
        compiler: CapabilityCompiler | None = None,
        planner: IntentPlanner | None = None,
    ) -> None:
        self._bus = bus
        self._graph = graph
        self._actor = actor
        self._resolver = resolver or RulesIntentResolver()
        self._compiler = compiler or RulesCapabilityCompiler()
        self._planner = planner or GraphIntentPlanner()

    @property
    def graph(self) -> CreativeGraph:
        return self._graph

    def resolve(self, text: str) -> Intent:
        """Free text -> structured intent, using the configured resolver."""
        return self._resolver.resolve(text, project_id=self._bus.project.project_id)

    def execute_intent(self, intent: Intent) -> SpineRun:
        """Compile, plan, dispatch and record one intent end to end."""
        if intent.project_id != self._bus.project.project_id:
            raise ValueError("intent targets a different project than the bus")

        compiled = self._compiler.compile(intent, self._bus.registry)
        intent_node = self._graph.add_node(
            "intent",
            label=intent.goal,
            data={"intent_id": intent.intent_id, "constraints": list(intent.constraints)},
        )
        planned = self._planner.plan(self._graph, intent_node, compiled)
        if not planned:
            raise ValueError(
                "intent compiled to no operations; the rules compiler does not understand it"
            )

        results: list[CommandResult] = []
        last_node = intent_node.node_id
        for step in planned:
            result = self._bus.dispatch(self._command(step, intent))
            results.append(result)
            last_node = self._graph.add_node(
                "artifact",
                label=f"{step.operation}@{result.state_revision}",
                data={
                    "operation": step.operation,
                    "transaction_id": result.transaction_id,
                    "state_revision": result.state_revision,
                    "output": result.output,
                },
                parents=(last_node,),
            ).node_id

        final = results[-1]
        artifact_node = self._graph.node(last_node)
        artifact = ArtifactRecord(
            kind="studio-state",
            content_hash=final.state_hash,
            produced_by_transaction=final.transaction_id,
            state_revision=final.state_revision,
            node_id=artifact_node.node_id,
            output=dict(final.output),
        )
        evidence = EvidenceRecord(
            artifact_id=artifact.artifact_id,
            command_id=self._command_id(planned[-1], intent),
            operation=planned[-1].operation,
            transaction_id=final.transaction_id,
            state_hash=final.state_hash,
            detail={"operations": list(compiled.operations), "steps": len(results)},
        )
        self._graph.add_node(
            "evidence",
            label=evidence.evidence_id,
            data=evidence.model_dump(mode="json"),
            parents=(artifact_node.node_id,),
        )
        return SpineRun(
            intent=intent,
            compiled=compiled,
            result=final,
            artifact=artifact,
            evidence=evidence,
        )

    def run(self, text: str) -> SpineRun:
        """Convenience: resolve *text* then execute the resulting intent."""
        return self.execute_intent(self.resolve(text))

    @staticmethod
    def _command_id(step: PlannedOperation, intent: Intent) -> str:
        return f"cmd_{intent.intent_id}_{step.operation.replace('.', '_')}"

    def _command(self, step: PlannedOperation, intent: Intent) -> TypedCommand:
        fields: dict[str, object] = {
            "command_id": self._command_id(step, intent),
            "operation": step.operation,
            "target": {"project_id": intent.project_id, **step.target},
            "input": dict(step.input),
            "trace_id": intent.intent_id,
        }
        if self._actor is not None:
            fields["schema_version"] = COMMAND_SCHEMA_VERSION
            fields["actor"] = self._actor
            fields["provenance"] = CommandProvenance(
                source="agent",
                source_id=self._actor.actor_id,
                reason=intent.goal[:500],
            )
        return TypedCommand(**fields)  # type: ignore[arg-type]
