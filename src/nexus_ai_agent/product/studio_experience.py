"""Presentation contracts for the Nagar Studio product surface.

The adapter translates user-facing intent and existing runtime facts into
stable UI-shaped data. It is intentionally not a compiler, command bus, job
queue, or persistence layer. Missing backend facts remain ``None``/empty and
are exposed as gaps rather than replaced with optimistic values.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from math import isfinite
from typing import Any

from nexus_ai_agent.application.ports.job_queue import JobStatus
from nexus_ai_agent.jobs.lifecycle import parse_job_status


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
        if not isinstance(self.goal, str):
            raise TypeError("intent goal must be a string")
        if not self.goal.strip():
            raise ValueError("intent goal must not be empty")
        if self.duration_seconds is not None and (
            not isinstance(self.duration_seconds, (int, float))
            or isinstance(self.duration_seconds, bool)
            or not isfinite(self.duration_seconds)
            or self.duration_seconds <= 0
        ):
            raise ValueError("duration_seconds must be a finite positive number")
        for name in ("constraints", "assets", "output_requirements"):
            values = getattr(self, name)
            if not isinstance(values, tuple) or not all(
                isinstance(value, str) and value.strip() for value in values
            ):
                raise TypeError(f"intent {name} must be a tuple of non-empty strings")
        for name in ("style", "audience", "aspect_ratio"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"intent {name} must be a non-empty string when provided")


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
    contract_gap: str | None = None


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
    if not isinstance(intent_id, str) or not intent_id.strip():
        raise ValueError("intent_id must be a non-empty string")
    if not isinstance(raw_plan, Mapping):
        raise TypeError("raw_plan must be a mapping produced by the compiler")
    raw_scenes = raw_plan.get("scenes", ())
    scenes: list[SceneView] = []
    if isinstance(raw_scenes, (list, tuple)):
        for index, raw in enumerate(raw_scenes):
            if not isinstance(raw, Mapping):
                raise ValueError(f"scene {index} must be a mapping")
            scene_id = raw.get("scene_id", raw.get("id"))
            label = raw.get("label", raw.get("name"))
            if not isinstance(scene_id, str) or not scene_id.strip():
                raise ValueError(f"scene {index} is missing compiler scene_id")
            if not isinstance(label, str) or not label.strip():
                raise ValueError(f"scene {scene_id!r} is missing compiler label")
            scenes.append(
                SceneView(
                    scene_id=scene_id,
                    label=label,
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
        contract_gap=(
            "plan_empty"
            if not scenes and not _text_tuple(raw_plan.get("operations")) and not output
            else None
        ),
    )


def present_execution(job_id: str, raw: Mapping[str, Any]) -> ExecutionView:
    """Project a durable job row/result without raising on legacy statuses."""
    if not isinstance(job_id, str) or not job_id.strip():
        raise ValueError("job_id must be a non-empty string")
    if not isinstance(raw, Mapping):
        raise TypeError("raw execution must be a mapping from the durable queue")
    raw_status = raw.get("status", raw.get("execution_status"))
    if isinstance(raw_status, JobStatus):
        status = raw_status.value
    else:
        status_text = str(raw_status or "")
        try:
            status = parse_job_status(status_text).value
        except (TypeError, ValueError):
            status = status_text or "unknown"
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
    requested_status = str(evidence.get("status") or "not_available")
    physical = evidence.get("physical_identity")
    has_measured_identity = (
        isinstance(physical, Mapping)
        and isinstance(physical.get("sha256"), str)
        and isinstance(physical.get("size_bytes"), int)
    )
    status = (
        "verified"
        if requested_status == "verified" and has_measured_identity
        else "unverified"
        if requested_status == "verified"
        else requested_status
    )
    evidence_gap = (
        None
        if status == "verified"
        else "verification_evidence_missing"
        if requested_status == "verified"
        else "artifact_verification_missing"
        if not evidence
        else None
    )
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


def present_lineage(raw: Mapping[str, Any]) -> LineageView:
    """Map explicit lineage links; never infer a missing relationship."""
    if not isinstance(raw, Mapping):
        raise TypeError("raw lineage must be a mapping from a durable result")
    intent_id = _optional_text(raw.get("intent_id"))
    plan_reference = _optional_text(raw.get("plan_reference"))
    job_id = _optional_text(raw.get("job_id"))
    artifact_id = _optional_text(raw.get("artifact_id"))
    revision = raw.get("revision") if isinstance(raw.get("revision"), int) else None
    values: tuple[tuple[str, object], ...] = (
        ("intent_id", intent_id),
        ("plan_reference", plan_reference),
        ("job_id", job_id),
        ("artifact_id", artifact_id),
        ("revision", revision),
    )
    missing = tuple(name for name, value in values if value is None)
    return LineageView(
        intent_id=intent_id,
        plan_reference=plan_reference,
        job_id=job_id,
        artifact_id=artifact_id,
        revision=revision,
        missing_links=missing,
    )


__all__ = [
    "ArtifactPassport",
    "ExecutionView",
    "IntentDraft",
    "LineageView",
    "PlanPreview",
    "SceneView",
    "present_artifact",
    "present_execution",
    "present_lineage",
    "present_plan",
]
