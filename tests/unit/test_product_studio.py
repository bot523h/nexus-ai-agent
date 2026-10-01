from __future__ import annotations

import pytest

from nexus_ai_agent.application.ports.job_queue import JobStatus
from nexus_ai_agent.product.studio_experience import (
    IntentDraft,
    present_artifact,
    present_execution,
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


def test_execution_view_accepts_only_canonical_durable_statuses() -> None:
    view = present_execution(
        "job-1", {"status": JobStatus.VERIFYING, "operation": "slideshow.render"}
    )
    assert view.status == "verifying"
    assert view.operation_id == "slideshow.render"
    with pytest.raises(ValueError, match="unknown durable job status"):
        present_execution("job-2", {"status": "running-ish"})


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
