"""Intent -> capability compilation and graph planning.

The compiler is the *one* place that decides which registry operations an intent
needs. It is a port (:class:`CapabilityCompiler`) so a future LLM compiler drops
in without touching the spine, and it validates every chosen operation against
the authoritative :class:`CapabilityRegistry` -- a compiler can never invent an
operation the registry does not expose.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from nexus_ai_agent.creative.spine.models import (
    CompilationError,
    CompiledIntent,
    CreativeGraph,
    GraphNode,
    Intent,
    PlannedOperation,
)
from nexus_ai_agent.creative.studio.capabilities import CapabilityRegistry
from nexus_ai_agent.creative.studio.models import UnknownOperationError


@runtime_checkable
class CapabilityCompiler(Protocol):
    """Port: a structured intent -> the registry operations that satisfy it."""

    def compile(self, intent: Intent, registry: CapabilityRegistry) -> CompiledIntent: ...


@runtime_checkable
class IntentPlanner(Protocol):
    """Port: record the plan into the graph and return the operations to run."""

    def plan(
        self, graph: CreativeGraph, intent_node: GraphNode, compiled: CompiledIntent
    ) -> tuple[PlannedOperation, ...]: ...


#: Deterministic keyword -> operation rules. Persian and English markers, kept
#: deliberately small: this is the rules baseline, not the understanding layer.
_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("پخش", "play", "پلی", "اجرا کن"), "media.play"),
    (("توقف", "pause", "نگه دار", "متوقف"), "media.pause"),
    (("علامت", "مارک", "mark", "نقطه گذاری", "نقطهگذاری"), "timeline.mark"),
    (("بازگردان", "undo", "لغو کن", "عقب"), "system.undo"),
    (("برش", "split", "ببر", "دو نیم"), "timeline.split_at_playhead"),
)

#: Operations whose inputs the rules compiler cannot safely invent (they need an
#: explicit target clip). Recognised on purpose so the gap is loud, not silent.
_NEEDS_TARGET = frozenset({"timeline.split_at_playhead"})


class RulesCapabilityCompiler:
    """Deterministic compiler over a small keyword table.

    It is honest about its limits: a recognised operation whose inputs it cannot
    supply without guessing (``timeline.split_at_playhead`` needs an explicit
    target clip) raises :class:`CompilationError` rather than emitting a command
    that would fail later. Unrecognised intents compile to an empty plan -- the
    spine then reports "no operations" instead of pretending to act.
    """

    def compile(self, intent: Intent, registry: CapabilityRegistry) -> CompiledIntent:
        lowered = intent.goal.lower()
        chosen: list[str] = []
        for markers, operation in _RULES:
            if any(marker in lowered for marker in markers):
                chosen.append(operation)

        plan: list[PlannedOperation] = []
        for operation in chosen:
            try:
                registry.get_spec(operation)
            except UnknownOperationError as exc:
                raise CompilationError(
                    f"compiler chose an operation the registry does not expose: {operation!r}"
                ) from exc
            if operation in _NEEDS_TARGET:
                raise CompilationError(
                    f"{operation} needs an explicit target clip; the rules compiler will not "
                    "guess one (provide a structured intent)"
                )
            plan.append(self._planned(operation, intent))

        return CompiledIntent(
            intent_id=intent.intent_id,
            operations=tuple(p.operation for p in plan),
            plan=tuple(plan),
            rationale=f"matched {len(plan)} operation(s) from the rules table",
        )

    def _planned(self, operation: str, intent: Intent) -> PlannedOperation:
        if operation == "timeline.mark":
            label = intent.goal.strip()[:120] or "mark"
            return PlannedOperation(
                operation=operation,
                input={"at": "اینجا", "label": label},
                description="mark the playhead with the intent's label",
            )
        return PlannedOperation(
            operation=operation,
            input={},
            description=f"satisfy the intent via {operation}",
        )


class GraphIntentPlanner:
    """Records a ``capability`` node per planned operation, parented on the intent."""

    def plan(
        self, graph: CreativeGraph, intent_node: GraphNode, compiled: CompiledIntent
    ) -> tuple[PlannedOperation, ...]:
        for planned in compiled.plan:
            graph.add_node(
                "capability",
                label=planned.operation,
                data={"operation": planned.operation, "input": planned.input},
                parents=(intent_node.node_id,),
            )
        return compiled.plan
