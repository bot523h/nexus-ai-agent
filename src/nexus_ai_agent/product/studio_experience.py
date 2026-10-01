"""Presentation contracts for the Nagar Studio product surface.

The adapter translates user-facing intent and existing runtime facts into
stable UI-shaped data. It is intentionally not a compiler, command bus, job
queue, or persistence layer. Missing backend facts remain ``None``/empty and
are exposed as gaps rather than replaced with optimistic values.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from nexus_ai_agent.application.ports.job_queue import JobStatus


@dataclass(frozen=True)
class IntentDraft:
    """User-authored intent before it becomes an execution contract."""

    goal: str
    constraints: tuple[str, ...] = ()
    assets: tuple[str, ...] = ()
    output_requirements: tuple[str, ...] = ()
    style: str | None = None
    audience: str | None = None
    duration_seconds: float | None = None
    aspect_ratio: str | None = None

    def __post_init__(self) -> None:
        if not self.goal.strip():
            raise ValueError("intent goal must not be empty")
        if self.duration_seconds is not None and self.duration_seconds <= 0:
            raise ValueError("duration_seconds must be positive")


@dataclass(frozen=True)
class SceneView:
    """A scene as returned by the creative compiler/plan producer."""

    scene_id: str
    label: str
    start_seconds: float | None = None
    end_seconds: float | None = None
    operations: tuple[str, ...] = ()
    asset_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlanPreview:
    """Read-only plan preview; no execution authority is implied."""

    intent_id: str
    scenes: tuple[SceneView, ...] = ()
    operations: tuple[str, ...] = ()
    output: Mapping[str, Any] = field(default_factory=dict)
    constraints: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()
    estimated_work: str | None = None
    verification_points: tuple[str, ...] = ()
    compiler_reference: str | None = None


@dataclass(frozen=True)
class ExecutionView:
    """Execution status projected from the canonical durable job state."""

    job_id: str
    status: str
    operation_id: str | None = None
    failure_reason: str | None = None
    artifact_id: str | None = None


@dataclass(frozen=True)
class ArtifactPassport:
    """Artifact identity plus only the evidence actually persisted."""

    artifact_id: str | None
    path: str | None
    kind: str | None
    sha256: str | None
    size_bytes: int | None
    verification_status: str
    verification_evidence: Mapping[str, Any]
    source_intent_id: str | None = None
    project_id: str | None = None
    revision: int | None = None
    evidence_gap: str | None = None


@dataclass(frozen=True)
class LineageView:
    """Trace links available to the UI; absent links are explicit gaps."""

    intent_id: str | None
    plan_reference: str | None
    job_id: str | None
    artifact_id: str | None
    revision: int | None
    missing_links: tuple[str, ...] = ()


def _text_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item) for item in value if str(item).strip())


def present_plan(intent_id: str, raw_plan: Mapping[str, Any]) -> PlanPreview:
    """Adapt a compiler-produced plan without reimplementing compilation."""
    raw_scenes = raw_plan.get("scenes", ())
    scenes: list[SceneView] = []
    if isinstance(raw_scenes, (list, tuple)):
        for index, raw in enumerate(raw_scenes):
            if not isinstance(raw, Mapping):
                continue
            scenes.append(
                SceneView(
                    scene_id=str(raw.get("scene_id") or raw.get("id") or f"scene-{index + 1}"),
                    label=str(raw.get("label") or raw.get("name") or f"Scene {index + 1}"),
                    start_seconds=_number(raw.get("start_seconds", raw.get("start"))),
                    end_seconds=_number(raw.get("end_seconds", raw.get("end"))),
                    operations=_text_tuple(raw.get("operations")),
                    asset_ids=_text_tuple(raw.get("asset_ids", raw.get("assets"))),
                )
            )
    output = raw_plan.get("output")
    return PlanPreview(
        intent_id=intent_id,
        scenes=tuple(scenes),
        operations=_text_tuple(raw_plan.get("operations")),
        output=dict(output) if isinstance(output, Mapping) else {},
        constraints=_text_tuple(raw_plan.get("constraints")),
        risks=_text_tuple(raw_plan.get("risks", raw_plan.get("uncertainty"))),
        estimated_work=str(raw_plan["estimated_work"]) if raw_plan.get("estimated_work") else None,
        verification_points=_text_tuple(raw_plan.get("verification_points")),
        compiler_reference=(
            str(raw_plan["compiler_reference"]) if raw_plan.get("compiler_reference") else None
        ),
    )


def present_execution(job_id: str, raw: Mapping[str, Any]) -> ExecutionView:
    """Project a durable job row/result; reject unknown statuses."""
    raw_status = raw.get("status", raw.get("execution_status"))
    if isinstance(raw_status, JobStatus):
        status = raw_status.value
    else:
        status = str(raw_status or "")
        allowed = {item.value for item in JobStatus}
        if status not in allowed:
            raise ValueError(f"unknown durable job status: {status!r}")
    return ExecutionView(
        job_id=job_id,
        status=status,
        operation_id=_optional_text(raw.get("operation_id", raw.get("operation"))),
        failure_reason=_optional_text(raw.get("failure_reason", raw.get("error"))),
        artifact_id=_optional_text(raw.get("artifact_id", raw.get("output_asset_id"))),
    )


def present_artifact(
    raw: Mapping[str, Any], *, source_intent_id: str | None = None, revision: int | None = None
) -> ArtifactPassport:
    """Present a verified artifact result; never infer verification success."""
    verification = raw.get("artifact_verification")
    evidence = dict(verification) if isinstance(verification, Mapping) else {}
    status = str(evidence.get("status") or "not_available")
    evidence_gap = None if evidence else "artifact_verification_missing"
    return ArtifactPassport(
        artifact_id=_optional_text(raw.get("artifact_id", raw.get("output_asset_id"))),
        path=_optional_text(raw.get("artifact_path", raw.get("output_path"))),
        kind=_optional_text(raw.get("artifact_kind")),
        sha256=_optional_text(raw.get("sha256", raw.get("content_sha256"))),
        size_bytes=raw.get("size_bytes") if isinstance(raw.get("size_bytes"), int) else None,
        verification_status=status,
        verification_evidence=evidence,
        source_intent_id=source_intent_id,
        project_id=_optional_text(evidence.get("logical_identity", {}).get("project_id"))
        if isinstance(evidence.get("logical_identity"), Mapping)
        else None,
        revision=revision,
        evidence_gap=evidence_gap,
    )


def _optional_text(value: object) -> str | None:
    if value is None or not str(value).strip():
        return None
    return str(value)


def _number(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


__all__ = [
    "ArtifactPassport",
    "ExecutionView",
    "IntentDraft",
    "LineageView",
    "PlanPreview",
    "SceneView",
    "present_artifact",
    "present_execution",
    "present_plan",
]
