"""The ATLAS analysis facade: one deterministic entry point over the lab.

``analyze_story`` runs the causal, temporal and (optionally) planning passes and
folds their typed statuses into one honest overall status. The fold is
fail-closed:

* any ``FAIL`` -> ``FAIL``;
* else any ``NEEDS_REVIEW``/``UNKNOWN``/``INFEASIBLE`` -> ``NEEDS_REVIEW`` (an
  ``INFEASIBLE`` plan is surfaced, but not as a continuity failure);
* else any ``RESOURCE_LIMIT`` -> ``RESOURCE_LIMIT``;
* else ``PASS``.

Absence of data never becomes ``PASS``: an unknown fact, an unverified shot
evidence reference, or an ambiguous order all keep the report out of ``PASS``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .causal import CausalEngine, CausalReport
from .models import StoryDocument
from .plan import PlanRequest, PlanResult, plan_edit
from .statuses import SolverStatus, Status
from .suggest import Suggestion, suggest_repairs
from .temporal import TemporalResult, check_consistency

__all__ = ["AnalysisReport", "analyze_story", "report_to_dict"]

_SEVERITY = {
    Status.PASS: 0,
    SolverStatus.PROVEN_OPTIMAL: 0,
    Status.RESOURCE_LIMIT: 1,
    SolverStatus.RESOURCE_LIMIT: 1,
    Status.FEASIBLE_NOT_PROVEN_OPTIMAL: 2,
    Status.UNKNOWN: 3,
    Status.NEEDS_REVIEW: 3,
    SolverStatus.INFEASIBLE: 3,
    Status.INFEASIBLE: 3,
    Status.FAIL: 4,
}


@dataclass
class AnalysisReport:
    fingerprint: str
    status: Status
    causal: CausalReport
    temporal: TemporalResult
    plan: PlanResult | None
    suggestions: tuple[Suggestion, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def passed(self) -> bool:
        return self.status is Status.PASS


def analyze_story(
    doc: StoryDocument,
    *,
    plan_request: PlanRequest | None = None,
    engine: CausalEngine | None = None,
) -> AnalysisReport:
    """Run the full analysis; deterministic for a given input."""
    eng = engine or CausalEngine()
    causal = eng.analyze(doc)

    extra_nodes = tuple(sorted({e.id for e in doc.events}))
    temporal = check_consistency(doc.temporal_constraints, extra_nodes=extra_nodes)

    plan: PlanResult | None = None
    if plan_request is not None:
        plan = plan_edit(plan_request)

    notes: list[str] = []
    if temporal.status is Status.UNKNOWN:
        notes.append("temporal consistency could not be decided")
    if temporal.status is Status.RESOURCE_LIMIT:
        notes.append(temporal.reason)
    if plan is not None and plan.status is SolverStatus.RESOURCE_LIMIT:
        notes.append(plan.explanation)
    if plan is not None and plan.status is SolverStatus.FEASIBLE_NOT_PROVEN_OPTIMAL:
        notes.append(plan.explanation)

    overall = _fold(causal.status, temporal.status, plan.status if plan else None)
    suggestions = suggest_repairs(causal.findings, temporal)
    return AnalysisReport(
        fingerprint=doc.fingerprint(),
        status=overall,
        causal=causal,
        temporal=temporal,
        plan=plan,
        suggestions=suggestions,
        notes=tuple(notes),
    )


def _fold(causal: Status, temporal: Status, plan: SolverStatus | None) -> Status:
    statuses: list[Status] = [causal, temporal]
    if plan is not None:
        statuses.append(Status(plan.value))
    worst = max(statuses, key=lambda s: _SEVERITY[s])
    if worst is Status.FAIL:
        return Status.FAIL
    if worst in (Status.NEEDS_REVIEW, Status.UNKNOWN, Status.INFEASIBLE):
        return Status.NEEDS_REVIEW
    if worst is Status.RESOURCE_LIMIT:
        return Status.RESOURCE_LIMIT
    if worst is Status.FEASIBLE_NOT_PROVEN_OPTIMAL:
        return Status.RESOURCE_LIMIT
    return Status.PASS


def report_to_dict(report: AnalysisReport) -> dict[str, Any]:
    """A deterministic, JSON-serialisable projection of the report."""
    out: dict[str, Any] = {
        "fingerprint": report.fingerprint,
        "status": report.status.value,
        "causal": {
            "status": report.causal.status.value,
            "event_order": list(report.causal.event_order),
            "findings": [
                {
                    "code": f.code,
                    "status": f.status.value,
                    "message": f.message,
                    "source": {"path": f.source.path, "line": f.source.line},
                    "event_ids": list(f.event_ids),
                }
                for f in report.causal.findings
            ],
            "facts": [
                {
                    "subject": key.subject_id,
                    "relation": key.relation.value,
                    "target": key.target_id,
                    "value": value.value,
                }
                for key, value in report.causal.fact_rows
            ],
        },
        "temporal": {
            "status": report.temporal.status.value,
            "node_count": report.temporal.node_count,
            "edge_count": report.temporal.edge_count,
            "relaxations": report.temporal.relaxations,
            "reason": report.temporal.reason,
            "witness": (
                {
                    "nodes": list(report.temporal.witness.nodes),
                    "constraint_ids": list(report.temporal.witness.constraint_ids),
                    "total_weight": report.temporal.witness.total_weight,
                }
                if report.temporal.witness is not None
                else None
            ),
        },
        "suggestions": [s.as_row() for s in report.suggestions],
        "notes": list(report.notes),
    }
    if report.plan is not None:
        out["plan"] = {
            "status": report.plan.status.value,
            "optimal": report.plan.optimal,
            "total_frames": report.plan.total_frames,
            "total_preference": report.plan.total_preference,
            "explanation": report.plan.explanation,
            "steps": [
                {
                    "candidate_id": s.candidate_id,
                    "asset_ref": s.asset_ref,
                    "covers": list(s.covers),
                    "frames": s.frames,
                    "evidence_status": s.evidence_status.value,
                    "evidence_verified": s.evidence_verified,
                }
                for s in report.plan.steps
            ],
            "rejected": [
                {"candidate_id": r.candidate_id, "reason": r.reason} for r in report.plan.rejected
            ],
        }
    return out
