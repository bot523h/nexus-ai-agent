"""Execution-semantics truth table (task-176, session 2 P0).

Every registered operation gets exactly one execution class
(``STATE_ONLY`` / ``EFFECT_DESCRIPTION`` / ``EXECUTABLE`` /
``RENDERED_ARTIFACT``), and this suite keeps the table honest:

* coverage is exact — a new operation without a classification fails loudly;
* ``EXECUTABLE`` is derived from the plan bridge's ``EFFECT_TO_LANE_OP`` map,
  never hand-claimed;
* every ``RENDERED_ARTIFACT`` is dispatched and its document bytes asserted;
* nothing state-only is ever reported as executable (no fake execution).
"""

from __future__ import annotations

import json

from nexus_ai_agent.creative.execution import (
    MISSING_RENDER_PRIMITIVES,
    RENDERED_ARTIFACT_OPS,
    ExecutionClass,
    classify_operation,
    execution_summary,
    execution_table,
    missing_primitives,
)
from nexus_ai_agent.creative.packs.caption import TranscriptRef, TranscriptSegment
from nexus_ai_agent.creative.packs.caption.operations import build_caption_registry
from nexus_ai_agent.creative.packs.delivery.operations import build_delivery_registry
from nexus_ai_agent.creative.packs.runtime import build_runtime_registry
from nexus_ai_agent.creative.rendering.plan import EFFECT_TO_LANE_OP
from nexus_ai_agent.creative.studio.bus import CommandBus
from nexus_ai_agent.creative.studio.models import (
    AssetRecord,
    PermissionLevel,
    Project,
    Timeline,
    TypedCommand,
    UnknownOperationError,
    new_project,
)


def _project_with_assets() -> Project:
    assets = [
        AssetRecord(
            asset_id="clip_master_01",
            media_kind="video",
            content_sha256="sha256:videoclip001",
            duration_us=5_000_000,
        ),
        AssetRecord(
            asset_id="audio_voice_01",
            media_kind="audio",
            content_sha256="sha256:audiotrack001",
            duration_us=10_000_000,
        ),
    ]
    timeline = Timeline(timeline_id="tl", duration_us=10_000_000)
    project = new_project("p_exec_01", "Execution semantics", timeline)
    return project.model_copy(update={"assets": assets})


def _transcript() -> TranscriptRef:
    return TranscriptRef(
        transcript_id="tr_exec_01",
        source_asset_id="audio_voice_01",
        language="en",
        duration_us=4_000_000,
        segments=(
            TranscriptSegment(start_us=0, end_us=2_000_000, text="hello world"),
            TranscriptSegment(start_us=2_000_000, end_us=4_000_000, text="second line"),
        ),
    )


def test_table_covers_exactly_the_runtime_registry() -> None:
    registry = build_runtime_registry()
    table = execution_table(registry=registry)
    assert [record.operation_id for record in table] == registry.list_operations()
    assert len({record.operation_id for record in table}) == len(table)


def test_summary_counts_are_internally_consistent() -> None:
    summary = execution_summary()
    class_total = sum(summary[member.value] for member in ExecutionClass)
    assert summary["registered"] == class_total
    assert summary["currently_executable"] == (
        summary[ExecutionClass.EXECUTABLE.value] + summary[ExecutionClass.RENDERED_ARTIFACT.value]
    )
    assert summary["registered"] > 0


def test_executable_is_derived_from_the_lane_twin_map() -> None:
    table = execution_table()
    by_id = {record.operation_id: record for record in table}
    for operation_id, twin in EFFECT_TO_LANE_OP.items():
        record = by_id[operation_id]
        assert record.execution_class is ExecutionClass.EXECUTABLE
        assert record.lane_twin == twin
        assert record.currently_executable is True
        assert record.requires_media_engine is True
        assert record.missing_primitive is None
    for record in table:
        if record.execution_class is ExecutionClass.EXECUTABLE:
            assert EFFECT_TO_LANE_OP[record.operation_id] == record.lane_twin


def test_state_only_and_effect_descriptions_are_never_executable() -> None:
    for record in execution_table():
        if record.execution_class in (
            ExecutionClass.STATE_ONLY,
            ExecutionClass.EFFECT_DESCRIPTION,
        ):
            assert record.currently_executable is False, record.operation_id
            assert record.artifact_producing is False, record.operation_id
            assert record.lane_twin is None, record.operation_id
        if record.execution_class is ExecutionClass.EFFECT_DESCRIPTION:
            assert record.requires_media_engine is True, record.operation_id
            assert record.missing_primitive, record.operation_id
        if record.execution_class is ExecutionClass.STATE_ONLY:
            assert record.requires_media_engine is False, record.operation_id
            assert record.missing_primitive is None, record.operation_id


def test_render_master_4k_is_a_spec_not_a_render() -> None:
    record = classify_operation("delivery.render_master_4k")
    assert record.execution_class is ExecutionClass.EFFECT_DESCRIPTION
    assert record.currently_executable is False
    assert record.missing_primitive == "master encode orchestration"


def test_slideshow_render_records_an_attestation_it_never_encodes() -> None:
    record = classify_operation("slideshow.render")
    assert record.execution_class is ExecutionClass.STATE_ONLY
    assert record.currently_executable is False


def test_missing_primitives_cover_every_effect_description() -> None:
    table = execution_table()
    effect_ids = {
        record.operation_id
        for record in table
        if record.execution_class is ExecutionClass.EFFECT_DESCRIPTION
    }
    assert set(MISSING_RENDER_PRIMITIVES) == effect_ids
    assert missing_primitives()  # the handoff list is non-empty and sorted
    assert [op for op, _ in missing_primitives()] == sorted(effect_ids)


def test_srt_emission_carries_real_document_bytes() -> None:
    project = _project_with_assets()
    bus = CommandBus(project, registry=build_caption_registry())
    result = bus.dispatch(
        TypedCommand(
            command_id="cmd-srt",
            operation="caption.generate_srt",
            input={"transcript": _transcript().model_dump(mode="json")},
        )
    )
    caption_asset = result.output["caption_asset"]
    assert "hello world" in caption_asset["content"]
    assert "00:00:00,000" in caption_asset["content"]
    assert result.output["srt_sha256"]
    record = classify_operation("caption.generate_srt")
    assert record.execution_class is ExecutionClass.RENDERED_ARTIFACT
    assert record.currently_executable is True
    assert record.requires_media_engine is False


def test_ass_emission_carries_real_document_bytes() -> None:
    project = _project_with_assets()
    bus = CommandBus(project, registry=build_caption_registry())
    result = bus.dispatch(
        TypedCommand(
            command_id="cmd-ass",
            operation="caption.generate_ass_rtl",
            input={"transcript": _transcript().model_dump(mode="json")},
        )
    )
    caption_asset = result.output["caption_asset"]
    assert "hello world" in caption_asset["content"]
    assert "[Events]" in caption_asset["content"]
    record = classify_operation("caption.generate_ass_rtl")
    assert record.execution_class is ExecutionClass.RENDERED_ARTIFACT


def test_export_otio_emission_carries_a_real_otio_document() -> None:
    project = _project_with_assets()
    bus = CommandBus(project, registry=build_delivery_registry(), allow_experimental=True)
    result = bus.dispatch(
        TypedCommand(command_id="cmd-otio", operation="delivery.export_otio", input={})
    )
    document = json.loads(result.output["otio_json"])
    assert document["OTIO_SCHEMA"].startswith("Timeline.")
    assert "tracks" in document
    record = classify_operation("delivery.export_otio")
    assert record.execution_class is ExecutionClass.RENDERED_ARTIFACT
    assert record.currently_executable is True
    assert "delivery.export_otio" in RENDERED_ARTIFACT_OPS


def test_requires_pack_and_determinism_come_from_the_spec() -> None:
    registry = build_runtime_registry()
    for record in execution_table(registry=registry):
        spec = registry.get_spec(record.operation_id)
        assert record.requires_pack == tuple(spec.required_packs)
        assert record.deterministic is bool(spec.deterministic)
    wave1 = classify_operation("media.play")
    assert wave1.requires_pack == ()
    assert wave1.execution_class is ExecutionClass.STATE_ONLY
    assert classify_operation("timeline.mark").execution_class is ExecutionClass.STATE_ONLY
    assert classify_operation("system.undo").execution_class is ExecutionClass.STATE_ONLY


def test_permission_levels_are_visible_alongside_execution_classes() -> None:
    registry = build_runtime_registry()
    gaze = registry.get_spec("portrait.correct_gaze")
    assert gaze.permission_level is PermissionLevel.CONFIRMATION
    assert (
        classify_operation("portrait.correct_gaze").execution_class
        is ExecutionClass.EFFECT_DESCRIPTION
    )


def test_unknown_operations_raise() -> None:
    try:
        classify_operation("nope.unknown_op")
    except UnknownOperationError:
        return
    raise AssertionError("classify_operation must reject unregistered operations")


def test_every_lane_twin_declares_its_engine_filters() -> None:
    """Session 3: no EXECUTABLE twin without a TWIN_ENGINE_FILTERS entry.

    The title twin's ``drawtext`` requirement is the reason this exists —
    the imageio wheel lacks it, and the table (not tribal knowledge) must
    say which engine each twin needs.
    """
    from nexus_ai_agent.creative.execution import TWIN_ENGINE_FILTERS
    from nexus_ai_agent.creative.rendering.plan import EFFECT_TO_LANE_OP

    assert set(EFFECT_TO_LANE_OP.values()) <= set(TWIN_ENGINE_FILTERS)
    assert TWIN_ENGINE_FILTERS["title"] == ("drawtext",)
    assert "drawtext" in classify_operation("motion.add_title").notes
    assert "lut3d" in classify_operation("color.apply_lut").notes
    assert "subtitles" in classify_operation("caption.burn_in").notes
