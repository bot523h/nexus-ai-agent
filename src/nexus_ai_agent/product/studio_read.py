"""Project-scoped, read-only composition for the Studio surface.

This module consumes application truth through a narrow read port. It does not
query storage, mutate jobs, execute commands, or calculate verification.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any, Protocol

from nexus_ai_agent.application.ports.job_queue import JobStatus
from nexus_ai_agent.product.studio_experience import (
    ArtifactPassport,
    ExecutionView,
    LineageView,
    PlanPreview,
    present_artifact,
    present_execution,
    present_lineage,
    present_plan,
)


class ProjectJobReadPort(Protocol):
    """Minimal existing application read capability required by Studio."""

    async def get_job(self, job_id: str) -> dict[str, Any] | None: ...


class ProjectReadDenied(PermissionError):
    """The caller is not allowed to inspect the requested project."""


@dataclass(frozen=True)
class StudioProjectView:
    project_id: str
    job_id: str | None
    project: Mapping[str, Any]
    intent: Mapping[str, Any]
    plan: PlanPreview | None
    execution: ExecutionView | None
    artifact: ArtifactPassport | None
    evidence: Mapping[str, Any]
    lineage: LineageView
    contract_gaps: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["plan"] = asdict(self.plan) if self.plan is not None else None
        result["execution"] = asdict(self.execution) if self.execution is not None else None
        result["artifact"] = asdict(self.artifact) if self.artifact is not None else None
        result["lineage"] = asdict(self.lineage)
        return result


class StudioReadService:
    """Compose one authorized project's durable read facts into a UI view."""

    def __init__(self, jobs: ProjectJobReadPort) -> None:
        self._jobs = jobs

    async def read_job(self, *, project_id: str, job_id: str) -> StudioProjectView:
        if not project_id.strip() or not job_id.strip():
            raise ValueError("project_id and job_id must be non-empty")
        row = await self._jobs.get_job(job_id)
        if row is None:
            raise ProjectReadDenied("project read is not authorized")
        input_data = row.get("input_data")
        if not isinstance(input_data, Mapping):
            raise ProjectReadDenied("project read is not authorized")
        row_project = input_data.get("project_id")
        if row_project != project_id:
            # Do not reveal whether the job exists under another project.
            raise ProjectReadDenied("project read is not authorized")

        result = row.get("result")
        result_map = result if isinstance(result, Mapping) else {}
        intent = input_data.get("intent")
        intent_map = intent if isinstance(intent, Mapping) else {}
        plan_raw = input_data.get("plan")
        plan = (
            present_plan(str(intent_map.get("intent_id")), plan_raw)
            if isinstance(plan_raw, Mapping) and intent_map.get("intent_id")
            else None
        )
        execution = present_execution(job_id, row)
        artifact = present_artifact(
            result_map,
            source_intent_id=str(intent_map.get("intent_id"))
            if intent_map.get("intent_id")
            else None,
        )
        evidence = result_map.get("artifact_verification")
        evidence_map = dict(evidence) if isinstance(evidence, Mapping) else {}
        lineage_raw = {
            "intent_id": intent_map.get("intent_id"),
            "plan_reference": plan.compiler_reference if plan else None,
            "job_id": job_id,
            "artifact_id": artifact.artifact_id,
            "revision": input_data.get("revision"),
        }
        lineage = present_lineage(lineage_raw)
        gaps = list(lineage.missing_links)
        if execution.status not in {item.value for item in JobStatus}:
            gaps.append("execution_status_unknown")
        if plan is None:
            gaps.append("plan")
        if artifact.evidence_gap:
            gaps.append(artifact.evidence_gap)
        if not evidence_map:
            gaps.append("evidence")
        return StudioProjectView(
            project_id=project_id,
            job_id=job_id,
            project={"project_id": project_id},
            intent=dict(intent_map),
            plan=plan,
            execution=execution,
            artifact=artifact,
            evidence=evidence_map,
            lineage=lineage,
            contract_gaps=tuple(dict.fromkeys(gaps)),
        )


__all__ = ["ProjectJobReadPort", "ProjectReadDenied", "StudioProjectView", "StudioReadService"]
