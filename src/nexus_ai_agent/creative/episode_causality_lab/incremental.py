"""Incremental continuity analysis (Milestone D).

The lab analyses a story by replaying its events in declared order. When one
event changes, a naive engine re-replays everything. This module does better
*without ever risking a wrong answer*:

1. The **prefix** of events before the first changed position is *not* replayed
   at all: the exact post-prefix world state, ``last_writer`` map and findings
   come from the previous run's checkpoints.
2. The **suffix** from that checkpoint is replayed with the real
   :class:`~nexus_ai_agent.creative.episode_causality_lab.causal.CausalEngine`,
   seeded with the prefix's ``last_writer`` map so its dependency output is
   identical to a full replay.

Because the transition is a pure function of state, a suffix replayed from a
correct prefix checkpoint is *provably* identical to a full replay. The
``incremental_result == full_recomputation_result`` invariant therefore holds by
construction, and the test suite checks it against the full oracle on seeded
random mutations.

A change that is not a pure in-place content edit — a reorder, an insert, a
delete, a change to the initial facts or the closed-world policy, or an
incomplete checkpoint list — falls back to :func:`full_recompute` and says so.
Falling back is always safe; the incremental path is only taken when exactness
is guaranteed.
"""

from __future__ import annotations

from dataclasses import dataclass

from .causal import CausalEngine, CausalReport, Finding, WorldState, _sorted_events
from .models import Event, StoryDocument
from .statuses import TriValue

__all__ = ["IncrementalResult", "IncrementalEngine", "full_recompute"]


def full_recompute(doc: StoryDocument, *, engine: CausalEngine | None = None) -> CausalReport:
    """The oracle: a complete replay from the initial state."""
    return (engine or CausalEngine()).analyze(doc)


@dataclass
class IncrementalResult:
    report: CausalReport
    mode: str  # "incremental" | "full_fallback"
    suffix_start: int
    reason: str

    @property
    def exact(self) -> bool:
        return True  # both modes are exact; the mode only records how it was reached


class IncrementalEngine:
    """Re-analyse a mutated story from a cached previous report.

    The engine holds no authoritative state of its own — it is a cache over the
    pure :class:`CausalEngine`.
    """

    def __init__(self, *, engine: CausalEngine | None = None) -> None:
        self._engine = engine or CausalEngine()

    def update(
        self,
        previous: StoryDocument | None,
        previous_report: CausalReport | None,
        current: StoryDocument,
    ) -> IncrementalResult:
        """Return the report for ``current``, incremental when provably exact."""
        if previous is None or previous_report is None:
            return self._full(current, "no previous analysis to reuse")

        if (
            previous.initial_facts != current.initial_facts
            or previous.closed_world != current.closed_world
        ):
            return self._full(current, "initial facts or closed-world policy changed")

        prev_events = _sorted_events(previous)
        cur_events = _sorted_events(current)
        if [e.id for e in prev_events] != [e.id for e in cur_events]:
            return self._full(current, "event set or order changed")

        if len(previous_report.checkpoints) != len(prev_events):
            return self._full(current, "cached checkpoints are incomplete")

        first_change = self._first_change(prev_events, cur_events)
        if first_change is None:
            return IncrementalResult(
                report=previous_report,
                mode="incremental",
                suffix_start=len(cur_events),
                reason="no change detected; previous report reused",
            )

        if first_change == 0:
            state = self._initial_state(current)
            prefix_findings: list[Finding] = []
            prefix_deps: dict[str, tuple[str, ...]] = {}
            prefix_checkpoints: tuple[object, ...] = ()
            last_writer: dict[tuple[str, str, str], str] = {}
        else:
            _, snapshot, lw_snapshot = previous_report.checkpoints[first_change - 1]
            state = WorldState.from_snapshot(snapshot, current.closed_world)
            prefix_ids = {e.id for e in cur_events[:first_change]}
            prefix_findings = [
                f
                for f in previous_report.findings
                if f.event_ids and f.event_ids[0] in prefix_ids
            ]
            prefix_deps = {
                e.id: previous_report.dependencies.get(e.id, ()) for e in cur_events[:first_change]
            }
            prefix_checkpoints = previous_report.checkpoints[:first_change]
            last_writer = dict(lw_snapshot)

        order_findings = self._engine._order_findings(current, cur_events)  # noqa: SLF001
        suffix_findings, suffix_deps, suffix_order, suffix_checkpoints = self._engine.replay(
            cur_events[first_change:], state, last_writer
        )
        findings = order_findings + prefix_findings + suffix_findings
        findings.sort(key=Finding.sort_key)
        report = CausalReport(
            status=self._engine._overall(findings),  # noqa: SLF001
            findings=tuple(findings),
            final_state=state,
            event_order=tuple(e.id for e in cur_events[:first_change]) + tuple(suffix_order),
            fact_rows=state.items(),
            dependencies={**prefix_deps, **suffix_deps},
            checkpoints=tuple(prefix_checkpoints) + tuple(suffix_checkpoints),  # type: ignore[arg-type]
        )
        return IncrementalResult(
            report=report,
            mode="incremental",
            suffix_start=first_change,
            reason=f"replayed the suffix from the checkpoint at index {first_change}",
        )

    def _full(self, doc: StoryDocument, reason: str) -> IncrementalResult:
        return IncrementalResult(
            report=full_recompute(doc, engine=self._engine),
            mode="full_fallback",
            suffix_start=0,
            reason=reason,
        )

    def _first_change(self, prev: list[Event], cur: list[Event]) -> int | None:
        for i, (a, b) in enumerate(zip(prev, cur, strict=False)):
            if a != b:
                return i
        return None

    def _initial_state(self, doc: StoryDocument) -> WorldState:
        facts: dict[tuple[str, str, str], TriValue] = {}
        for declared in doc.initial_facts:
            facts[declared.key.as_tuple()] = declared.value
        return WorldState(facts, doc.closed_world)
