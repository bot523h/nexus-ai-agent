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

Rollback is *transaction-scoped*: the run records the transaction id the bus
returned for each of its own steps, and undoes only while the newest editable
transaction is still one of them. A concurrent edit committed by another actor
between two steps is therefore never rewound -- the rollback fails closed
instead.
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
    SpineRollbackError,
)
from nexus_ai_agent.creative.studio.bus import CommandBus
from nexus_ai_agent.creative.studio.models import (
    COMMAND_SCHEMA_VERSION,
    ActorIdentity,
    CommandProvenance,
    CommandResult,
    TypedCommand,
    UndoConflictError,
)


@dataclass(frozen=True)
class SpineRun:
    """Everything one intent produced: the plan, the result, the artifact, evidence."""

    intent: Intent
    compiled: CompiledIntent
    result: CommandResult
    artifact: ArtifactRecord
    evidence: EvidenceRecord


DEFAULT_SPINE_ACTOR = ActorIdentity(kind="agent", actor_id="creative-spine")


class CreativeExecutionSpine:
    """Intent-first execution over one bus and one creative graph.

    The spine is the *only* component that compiles an intent into commands; it
    reuses the bus's own registry so a plan can never target an operation the
    bus would reject as unknown.

    When an ``actor`` is supplied (or defaulted to ``DEFAULT_SPINE_ACTOR``) the
    spine emits schema-2 commands (explicit actor + provenance), which is what
    a bus configured with a trusted
    :class:`~nexus_ai_agent.creative.studio.authorization.ProjectAuthorizer`
    requires. Passing ``actor=None`` emits claim-less commands, which the bus
    refuses fail-closed under STOP-C.
    """

    def __init__(
        self,
        bus: CommandBus,
        graph: CreativeGraph,
        *,
        actor: ActorIdentity | None = DEFAULT_SPINE_ACTOR,
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
        # In-process exactly-once cache: a *successful* run is replayed for a
        # duplicate delivery of the same intent instead of mutating twice. A
        # failed run is never cached, so a retry is a genuine retry.
        self._completed: dict[str, SpineRun] = {}

    @property
    def graph(self) -> CreativeGraph:
        return self._graph

    def resolve(self, text: str) -> Intent:
        """Free text -> structured intent, using the configured resolver."""
        return self._resolver.resolve(text, project_id=self._bus.project.project_id)

    def execute_intent(self, intent: Intent) -> SpineRun:
        """Compile, plan, dispatch and record one intent end to end.

        The run is transactional: the plan is dispatched step by step, and if a
        later step is refused the steps already applied are undone through the
        bus's own ``system.undo`` path before the error propagates. Each undo
        names the transaction id the bus returned for that step, and the bus's
        stage-9 identity gate refuses unless the named transaction is still the
        newest editable one -- a check that is atomic with the state swap, so a
        concurrent edit by another actor is never rewound.

        When nothing interleaves, the rollback restores the content identity
        (``state_hash``) the run began with and retracts every artifact node, so
        a failed run leaves the project content unchanged. When a foreign actor
        commits between two steps the run *cannot* undo its own step without
        rewinding that foreign work, so the rollback fails closed with
        :class:`SpineRollbackError` and the run's own already-applied step
        remains in the project -- loudly, never silently, and the foreign edit is
        untouched (the surviving content is last-writer-wins). No artifact node
        is ever left for work that did not complete.
        """
        if intent.project_id != self._bus.project.project_id:
            raise ValueError("intent targets a different project than the bus")

        cached = self._completed.get(intent.intent_id)
        if cached is not None:
            if cached.intent != intent:
                raise ValueError(
                    f"intent_id {intent.intent_id!r} was already executed with different content"
                )
            return cached

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
        artifact_nodes: list[str] = []
        committed_transactions: list[str] = []
        last_node = intent_node.node_id
        try:
            for step_index, step in enumerate(planned):
                result = self._bus.dispatch(self._command(step, intent, step_index))
                results.append(result)
                committed_transactions.append(result.transaction_id)
                # The bus has committed the step but the graph node is written
                # after; at this instant the run is two stores out of step. A
                # process crash exactly here is *recoverable*, not a silent
                # divergence: the bus transaction is real, the artifact node is
                # simply absent, and a retry re-derives it (the spine's in-process
                # idempotency cache is empty after a crash). Awaiting a durable
                # graph is tracked by task-186's documented durability trigger.
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
                # Retract immediately if anything after the commit fails, so no
                # code path can leave an orphan artifact node behind.
                artifact_nodes.append(last_node)
        except Exception:
            # All-or-nothing: a plan is only meaningful as a whole, so undo every
            # step this run already committed. Undo goes through the bus, so it is
            # authorized exactly like any other command -- the spine has no private
            # write path, not even to roll back. The rollback is scoped to the
            # transactions *this run* produced, so a foreign edit interleaved
            # between two steps is left untouched (the run fails closed instead).
            self._rollback(intent, committed_transactions, artifact_nodes)
            raise

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
            command_id=self._command_id(planned[-1], intent, len(planned) - 1),
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
        run = SpineRun(
            intent=intent,
            compiled=compiled,
            result=final,
            artifact=artifact,
            evidence=evidence,
        )
        self._completed[intent.intent_id] = run
        return run

    def run(self, text: str) -> SpineRun:
        """Convenience: resolve *text* then execute the resulting intent."""
        return self.execute_intent(self.resolve(text))

    def _rollback(
        self, intent: Intent, committed_transactions: list[str], artifact_nodes: list[str]
    ) -> None:
        """Undo the run's OWN committed steps through the bus, newest first.

        The authoritative guard is now the *bus contract*: each undo names the
        transaction it means to rewind (``system.undo(transaction_id=...)``), and
        the identity gate runs inside the stage-9 handler under the same lock as
        the state swap. If a concurrent actor committed an edit in between, the
        named transaction is no longer the newest editable one, the bus refuses
        with :class:`UndoConflictError`, and the rollback fails closed with a
        :class:`SpineRollbackError` -- the foreign edit is never rewound. This is
        stronger than a caller-side check, which cannot be atomic (the lock is
        released between the read and the dispatch).

        Artifact nodes the failed run wrote are retracted even when the rollback
        cannot complete (newest first, so every removal is a leaf removal), so a
        partial rollback never leaves an orphan artifact/evidence node in the
        graph. The undo commands carry the same actor/provenance as the forward
        commands, so a run only rolls back what it was authorized to do.

        The bus's ``state_revision`` is monotonic by design and is *not* rewound;
        content identity is carried by ``state_hash``, which the undo restores
        exactly, so a failed run leaves the project's content unchanged.
        """
        try:
            while committed_transactions:
                index = len(committed_transactions) - 1
                self._bus.dispatch(self._undo_command(intent, index, committed_transactions[-1]))
                committed_transactions.pop()
        except Exception as exc:
            self._retract_artifacts(artifact_nodes)
            if isinstance(exc, SpineRollbackError):
                raise
            if isinstance(exc, UndoConflictError):
                raise SpineRollbackError(
                    "refusing to roll back: the newest editable transaction is not "
                    "the run's own (a concurrent edit interleaved with the failed run)"
                ) from exc
            raise SpineRollbackError(f"failed to roll back the plan: {exc}") from exc
        self._retract_artifacts(artifact_nodes)

    def _retract_artifacts(self, artifact_nodes: list[str]) -> None:
        """Retract the artifact nodes a failed run wrote (newest first)."""
        for node_id in reversed(artifact_nodes):
            self._graph.remove_node(node_id)

    @staticmethod
    def _command_id(step: PlannedOperation, intent: Intent, step_index: int) -> str:
        # The step index keeps ids unique even when a plan repeats an operation
        # (e.g. two timeline.mark steps), so the bus's idempotency fingerprint can
        # never confuse two distinct steps of one plan.
        return f"cmd_{intent.intent_id}_{step_index}_{step.operation.replace('.', '_')}"

    def _undo_command(self, intent: Intent, index: int, transaction_id: str) -> TypedCommand:
        fields: dict[str, object] = {
            "command_id": f"cmd_{intent.intent_id}_undo_{index}",
            "operation": "system.undo",
            "target": {"project_id": intent.project_id},
            # Name the transaction this undo is for; the bus refuses unless it is
            # still the newest editable one, so a foreign edit is never rewound.
            "input": {"transaction_id": transaction_id},
            "trace_id": intent.intent_id,
        }
        if self._actor is not None:
            fields["schema_version"] = COMMAND_SCHEMA_VERSION
            fields["actor"] = self._actor
            fields["provenance"] = CommandProvenance(
                source="agent",
                source_id=self._actor.actor_id,
                reason=f"rollback of failed intent {intent.intent_id}"[:500],
            )
        return TypedCommand(**fields)  # type: ignore[arg-type]

    def _command(self, step: PlannedOperation, intent: Intent, step_index: int) -> TypedCommand:
        fields: dict[str, object] = {
            "command_id": self._command_id(step, intent, step_index),
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
