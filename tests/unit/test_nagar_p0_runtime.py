from __future__ import annotations

from nexus_ai_agent.nagar.p0 import EvidenceKind, SemanticSegment
from nexus_ai_agent.nagar.p0.runtime import canonical_command_input, preview_nel


def segment(**changes: object) -> SemanticSegment:
    values: dict[str, object] = {
        "segment_id": "seg-intro",
        "project_id": "project-1",
        "tenant_id": "tenant-1",
        "asset_id": "asset-1",
        "timeline_revision": 3,
        "label": "intro",
        "start_pts": 0,
        "end_pts": 120,
        "timebase_num": 1,
        "timebase_den": 30,
        "evidence_kind": EvidenceKind.DETERMINISTIC_CHECK,
        "confidence": 0.95,
        "uncertainty": 0.01,
        "source_model": "scene-detector.v1",
    }
    values.update(changes)
    return SemanticSegment.model_validate(values)


SOURCE = """NEL 1
PROJECT project-1
INTENT "tighten_intro"
SELECT semantic_segment("intro")
TRIM duration_delta=-2.0s
REQUIRE evidence("scene-detector.v1")
PREVIEW
"""


def test_preview_resolves_semantic_time_and_maps_to_canonical_operation_input() -> None:
    preview = preview_nel(
        SOURCE,
        project_id="project-1",
        tenant_id="tenant-1",
        timeline_revision=3,
        segments=[segment()],
    )
    assert preview.status == "PREVIEW_READY"
    assert preview.job_created is False
    assert canonical_command_input(preview) == {
        "clip_asset_id": "asset-1",
        "in_point_us": 0,
        "out_point_us": 60,
        "semantic_segment_id": "seg-intro",
        "timeline_revision": 3,
    }


def test_stale_semantic_segment_never_creates_job() -> None:
    preview = preview_nel(
        SOURCE,
        project_id="project-1",
        tenant_id="tenant-1",
        timeline_revision=4,
        segments=[segment()],
    )
    assert preview.status == "NEEDS_CLARIFICATION"
    assert not preview.job_created


def test_ambiguous_segments_require_clarification() -> None:
    preview = preview_nel(
        SOURCE,
        project_id="project-1",
        tenant_id="tenant-1",
        timeline_revision=3,
        segments=[segment(segment_id="a"), segment(segment_id="b")],
    )
    assert preview.status == "NEEDS_CLARIFICATION"
    assert not preview.job_created
