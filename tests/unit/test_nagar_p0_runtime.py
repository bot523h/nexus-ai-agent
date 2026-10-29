from __future__ import annotations

import pytest
from pydantic import ValidationError

from nexus_ai_agent.creative.packs.edit.models import TrimInput
from nexus_ai_agent.nagar.p0 import EvidenceKind, SemanticSegment, compile_nel
from nexus_ai_agent.nagar.p0.runtime import canonical_command_input, preview_nel


def segment(**changes: object) -> SemanticSegment:
    values: dict[str, object] = {
        "segment_id": "seg-intro",
        "project_id": "project-1",
        "tenant_id": "tenant-1",
        "asset_id": "src",
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


def test_preview_converts_pts_to_microseconds_and_maps_only_trim_schema() -> None:
    preview = preview_nel(
        SOURCE,
        project_id="project-1",
        tenant_id="tenant-1",
        timeline_revision=3,
        segments=[segment()],
        source_duration_us=4_000_000,
    )
    assert preview.status == "PREVIEW_READY"
    assert preview.job_created is False
    payload = canonical_command_input(preview)
    assert payload == {"clip_asset_id": "src", "in_point_us": 0, "out_point_us": 2_000_000}
    assert TrimInput.model_validate(payload).out_point_us == 2_000_000
    assert "semantic_segment_id" not in payload
    assert "timeline_revision" not in payload


def test_nonzero_start_and_additional_timebase_are_converted_exactly() -> None:
    source = SOURCE.replace("duration_delta=-2.0s", "duration_delta=-0.5s")
    preview = preview_nel(
        source,
        project_id="project-1",
        tenant_id="tenant-1",
        timeline_revision=3,
        segments=[segment(start_pts=100, end_pts=300, timebase_num=1, timebase_den=25)],
        source_duration_us=20_000_000,
    )
    assert preview.status == "PREVIEW_READY"
    assert canonical_command_input(preview) == {
        "clip_asset_id": "src",
        "in_point_us": 4_000_000,
        "out_point_us": 11_500_000,
    }


def test_rounding_is_deterministic_at_microsecond_boundary() -> None:
    source = SOURCE.replace("duration_delta=-2.0s", "duration_delta=-0.0000005s")
    preview = preview_nel(
        source,
        project_id="project-1",
        tenant_id="tenant-1",
        timeline_revision=3,
        segments=[segment(end_pts=1, timebase_num=1, timebase_den=3)],
        source_duration_us=1_000_000,
    )
    assert preview.status == "PREVIEW_READY"
    assert canonical_command_input(preview)["out_point_us"] == 333_332


def test_positive_delta_requires_and_respects_authoritative_source_bounds() -> None:
    source = SOURCE.replace("duration_delta=-2.0s", "duration_delta=1.0s")
    missing = preview_nel(
        source,
        project_id="project-1",
        tenant_id="tenant-1",
        timeline_revision=3,
        segments=[segment()],
    )
    assert missing.status == "NEEDS_CLARIFICATION"
    exceeded = preview_nel(
        source,
        project_id="project-1",
        tenant_id="tenant-1",
        timeline_revision=3,
        segments=[segment()],
        source_duration_us=4_500_000,
    )
    assert exceeded.status == "REJECTED"
    assert exceeded.diagnostics == ("source_bounds_exceeded",)

    below_zero = preview_nel(
        SOURCE.replace("duration_delta=-2.0s", "duration_delta=-5.0s"),
        project_id="project-1",
        tenant_id="tenant-1",
        timeline_revision=3,
        segments=[segment()],
        source_duration_us=4_000_000,
    )
    assert below_zero.status == "REJECTED"
    assert below_zero.diagnostics == ("converted coordinate must be non-negative",)


def test_missing_evidence_changes_runtime_result() -> None:
    missing = preview_nel(
        SOURCE.replace("scene-detector.v1", "unavailable.v1"),
        project_id="project-1",
        tenant_id="tenant-1",
        timeline_revision=3,
        segments=[segment()],
        source_duration_us=4_000_000,
    )
    assert missing.status == "NEEDS_CLARIFICATION"
    assert "missing_evidence:unavailable.v1" in missing.diagnostics


def test_stale_and_ambiguous_segments_never_create_jobs() -> None:
    stale = preview_nel(
        SOURCE,
        project_id="project-1",
        tenant_id="tenant-1",
        timeline_revision=4,
        segments=[segment()],
    )
    ambiguous = preview_nel(
        SOURCE,
        project_id="project-1",
        tenant_id="tenant-1",
        timeline_revision=3,
        segments=[segment(segment_id="a"), segment(segment_id="b")],
    )
    assert stale.status == "NEEDS_CLARIFICATION"
    assert ambiguous.status == "NEEDS_CLARIFICATION"
    assert not stale.job_created and not ambiguous.job_created


def test_invalid_segment_and_extra_canonical_fields_fail_closed() -> None:
    with pytest.raises(ValidationError):
        segment(start_pts=10, end_pts=10)
    with pytest.raises(ValidationError):
        TrimInput.model_validate(
            {"clip_asset_id": "src", "in_point_us": 0, "out_point_us": 1, "timeline_revision": 3}
        )


def test_duplicate_directives_fail_closed() -> None:
    duplicate = SOURCE.replace('INTENT "tighten_intro"', 'INTENT "tighten_intro"\nINTENT "other"')
    result = compile_nel(duplicate)
    assert result.ast is None
    assert any(item.code == "duplicate_statement" for item in result.diagnostics)


def test_trim_pause_intent_is_not_silently_mapped_to_keep_only_the_pause() -> None:
    source = SOURCE.replace('INTENT "tighten_intro"', 'INTENT "shorten pause"')
    result = preview_nel(
        source,
        project_id="project-1",
        tenant_id="tenant-1",
        timeline_revision=3,
        segments=[segment(end_pts=12_800, timebase_num=1, timebase_den=1_000)],
        source_duration_us=60_000_000,
    )
    assert result.status == "REJECTED"
    assert "unsupported_intent" in result.diagnostics


def test_zero_delta_is_rejected_as_noop() -> None:
    result = preview_nel(
        SOURCE.replace("duration_delta=-2.0s", "duration_delta=0.0s"),
        project_id="project-1",
        tenant_id="tenant-1",
        timeline_revision=3,
        segments=[segment()],
        source_duration_us=4_000_000,
    )
    assert result.status == "REJECTED"
    assert result.diagnostics == ("noop_trim",)


def test_unverified_preserve_constraint_requires_clarification() -> None:
    result = preview_nel(
        SOURCE.replace("PREVIEW\n", "PRESERVE speech_continuity=true\nPREVIEW\n"),
        project_id="project-1",
        tenant_id="tenant-1",
        timeline_revision=3,
        segments=[segment()],
        source_duration_us=4_000_000,
    )
    assert result.status == "NEEDS_CLARIFICATION"
    assert result.diagnostics == ("unsupported_constraint:speech_continuity",)


def test_source_model_name_alone_is_not_evidence() -> None:
    source = SOURCE.replace('REQUIRE evidence("scene-detector.v1")', 'REQUIRE evidence("unlisted-model")')
    result = preview_nel(
        source,
        project_id="project-1",
        tenant_id="tenant-1",
        timeline_revision=3,
        segments=[segment()],
        source_duration_us=4_000_000,
    )
    assert result.status == "NEEDS_CLARIFICATION"
    assert result.diagnostics == ("missing_evidence:unlisted-model",)
