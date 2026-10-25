from __future__ import annotations

import pytest

from nexus_ai_agent.application.ports.job_queue import JobStatus
from nexus_ai_agent.product.studio_experience import (
    IntentDraft,
    present_artifact,
    present_execution,
    present_lineage,
    present_plan,
)


def test_intent_draft_is_user_facing_and_validates_goal() -> None:
    intent = IntentDraft(
        goal="Create a cinematic 30-second product video",
        constraints=("No voiceover",),
        duration_seconds=30,
        aspect_ratio="16:9",
    )
    assert intent.goal.startswith("Create")
    with pytest.raises(ValueError, match="goal"):
        IntentDraft(goal=" ")
    with pytest.raises(TypeError, match="tuple"):
        IntentDraft(goal="ok", constraints=["mutable"])  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="finite"):
        IntentDraft(goal="ok", duration_seconds=float("inf"))


def test_plan_preview_consumes_compiler_facts_without_compiling() -> None:
    preview = present_plan(
        "intent-1",
        {
            "compiler_reference": "creative-plan-42",
            "scenes": [
                {"id": "s1", "name": "Opening", "start": 0, "end": 3, "operations": ["media.play"]}
            ],
            "operations": ["media.play"],
            "risks": ["Source image duration not measured"],
            "verification_points": ["probe published video"],
        },
    )
    assert preview.intent_id == "intent-1"
    assert preview.compiler_reference == "creative-plan-42"
    assert preview.scenes[0].start_seconds == 0.0
    assert preview.risks == ("Source image duration not measured",)


def test_plan_preview_rejects_malformed_scene_instead_of_inventing_metadata() -> None:
    with pytest.raises(ValueError, match="scene_id"):
        present_plan("intent-1", {"scenes": [{"label": "Opening"}]})
    empty = present_plan("intent-2", {})
    assert empty.contract_gap == "plan_empty"


def test_execution_view_normalizes_legacy_and_preserves_unknown_statuses() -> None:
    view = present_execution(
        "job-1", {"status": JobStatus.VERIFYING, "operation": "slideshow.render"}
    )
    assert view.status == "verifying"
    assert view.operation_id == "slideshow.render"
    assert present_execution("job-2", {"status": "running"}).status == "processing"
    assert present_execution("job-3", {"status": "running-ish"}).status == "running-ish"


def test_artifact_passport_does_not_claim_verification_when_evidence_is_missing() -> None:
    passport = present_artifact(
        {"output_path": "/workspace/output.mp4", "content_sha256": "sha256:abc"},
        source_intent_id="intent-1",
    )
    assert passport.path == "/workspace/output.mp4"
    assert passport.sha256 == "sha256:abc"
    assert passport.verification_status == "not_available"
    assert passport.evidence_gap == "artifact_verification_missing"


def test_artifact_passport_preserves_measured_lineage_and_evidence() -> None:
    passport = present_artifact(
        {
            "artifact_id": "asset-2",
            "artifact_path": "/workspace/output.mp4",
            "artifact_kind": "video",
            "sha256": "sha256:abc",
            "size_bytes": 123,
            "artifact_verification": {
                "status": "verified",
                "logical_identity": {"project_id": "shot-key-1"},
                "physical_identity": {"sha256": "sha256:abc", "size_bytes": 123},
                "probe": {"duration_us": 2_000_000},
            },
        },
        source_intent_id="intent-1",
        revision=2,
    )
    assert passport.verification_status == "verified"
    assert passport.project_id == "shot-key-1"
    assert passport.revision == 2
    assert passport.verification_evidence["probe"]["duration_us"] == 2_000_000
    assert passport.evidence_gap is None


def test_status_only_verified_record_is_not_evidence_backed() -> None:
    passport = present_artifact(
        {"artifact_verification": {"status": "verified"}, "output_path": "/tmp/out.mp4"}
    )
    assert passport.verification_status == "unverified"
    assert passport.evidence_gap == "verification_evidence_missing"


def test_lineage_reports_only_explicit_links_and_missing_fields() -> None:
    lineage = present_lineage({"intent_id": "intent-1", "artifact_id": "asset-1"})
    assert lineage.intent_id == "intent-1"
    assert lineage.artifact_id == "asset-1"
    assert lineage.plan_reference is None
    assert lineage.missing_links == ("plan_reference", "job_id", "revision")


def test_lineage_does_not_infer_artifact_from_job_or_intent() -> None:
    lineage = present_lineage({"intent_id": "intent-1", "job_id": "job-1"})
    assert lineage.artifact_id is None
    assert "artifact_id" in lineage.missing_links
