"""Tests for the deterministic render-plan bridge (task-165 — mission §9).

The plan is the minimal missing step between the pure packs' ``Project`` state
and the one canonical lane (``LaneIR → filtergraph → argv → FFmpeg``).  These
tests pin its contract:

* determinism (same project → identical plan hash);
* trim/effects mapping fidelity (``color.adjust_exposure`` → ``ExposureOp``);
* honest unmapped-effect reporting (no silent drops);
* the proven LaneIR limitation: multi-clip tracks need concat (``concat_required``);
* integration: a single-clip plan compiles through the *existing* lane compiler
  into a deterministic argv — no parallel pipeline.
"""

from __future__ import annotations

import pytest

from nexus_ai_agent.creative.rendering.compiler import compile_lane
from nexus_ai_agent.creative.rendering.ir import ExposureOp, LaneProfile, TrimOp
from nexus_ai_agent.creative.rendering.plan import (
    PlanError,
    compile_execution_plan,
    segment_lane_ir,
)
from nexus_ai_agent.creative.studio.models import (
    AssetRecord,
    Clip,
    EffectLayerRef,
    MediaRef,
    Timeline,
    TimeRangeUS,
    Track,
    new_project,
)


def _media(asset_id: str, duration_us: int) -> MediaRef:
    return MediaRef(
        asset_id=asset_id,
        content_sha256=f"sha256:{asset_id}",
        media_kind="video",
        duration_us=duration_us,
    )


def _clip(
    clip_id: str,
    media: MediaRef,
    source: tuple[int, int],
    timeline: tuple[int, int],
    effects: list[EffectLayerRef] | None = None,
) -> Clip:
    return Clip(
        clip_id=clip_id,
        media_ref=media,
        source_range=TimeRangeUS(start_us=source[0], end_us=source[1]),
        timeline_range=TimeRangeUS(start_us=timeline[0], end_us=timeline[1]),
        effects=effects or [],
    )


def _project(
    tracks: list[Track], *, duration_us: int, assets: list[object] | None = None
) -> object:
    timeline = Timeline(timeline_id="tl", duration_us=duration_us, tracks=tracks)
    project = new_project("p", "Plan Project", timeline)
    return project.model_copy(update={"assets": assets or []})


def test_plan_is_deterministic_and_parameter_sensitive() -> None:
    hero = _media("hero", 8_000_000)
    track = Track(
        track_id="video_01",
        name="V",
        kind="video",
        clips=[_clip("c1", hero, (1_000_000, 5_000_000), (0, 4_000_000))],
    )
    project = _project([track], duration_us=4_000_000)
    first = compile_execution_plan(project, track_id="video_01")  # type: ignore[arg-type]
    second = compile_execution_plan(project, track_id="video_01")  # type: ignore[arg-type]
    assert first.plan_hash == second.plan_hash
    assert first.plan_hash.startswith("sha256:")
    assert first.segments[0].source_in_us == 1_000_000
    assert first.segments[0].source_out_us == 5_000_000
    assert first.segments[0].timeline_start_us == 0
    assert first.concat_required is False


def test_trim_window_maps_to_the_source_range() -> None:
    hero = _media("hero", 8_000_000)
    track = Track(
        track_id="video_01",
        name="V",
        kind="video",
        clips=[_clip("c1", hero, (2_000_000, 6_500_000), (0, 4_500_000))],
    )
    plan = compile_execution_plan(_project([track], duration_us=4_500_000), track_id="video_01")  # type: ignore[arg-type]
    segment = plan.segments[0]
    assert segment.trim_us == 4_500_000
    assert (segment.source_in_us, segment.source_out_us) == (2_000_000, 6_500_000)


def test_exposure_effects_map_and_unmapped_effects_are_reported() -> None:
    hero = _media("hero", 8_000_000)
    effects = [
        EffectLayerRef(
            operation="color.adjust_exposure",
            parameters={"exposure_ev": 0.5, "contrast": 1.2, "temperature_k": 5600, "tint": -5.0},
            range=TimeRangeUS(start_us=0, end_us=4_000_000),
        ),
        EffectLayerRef(
            operation="portrait.smooth_skin",
            parameters={"strength": 0.3},
            range=TimeRangeUS(start_us=0, end_us=4_000_000),
        ),
    ]
    track = Track(
        track_id="video_01",
        name="V",
        kind="video",
        clips=[_clip("c1", hero, (0, 4_000_000), (0, 4_000_000), effects)],
    )
    plan = compile_execution_plan(_project([track], duration_us=4_000_000), track_id="video_01")  # type: ignore[arg-type]
    exposure_ops = [op for op in plan.segments[0].ops if isinstance(op, ExposureOp)]
    assert len(exposure_ops) == 1
    assert exposure_ops[0].exposure_ev == 0.5
    assert exposure_ops[0].contrast == 1.2
    assert exposure_ops[0].temperature_k == 5600
    assert exposure_ops[0].tint == -5.0
    assert plan.segments[0].unmapped_effects == ("portrait.smooth_skin",)
    assert plan.unmapped_effects == ("c1:portrait.smooth_skin",)


def test_multi_clip_track_sets_concat_required_the_proven_lane_limitation() -> None:
    hero = _media("hero", 8_000_000)
    broll = _media("broll", 4_000_000)
    track = Track(
        track_id="video_01",
        name="V",
        kind="video",
        clips=[
            _clip("c1", hero, (0, 2_000_000), (0, 2_000_000)),
            _clip("c2", broll, (0, 2_000_000), (3_000_000, 5_000_000)),
        ],
    )
    plan = compile_execution_plan(_project([track], duration_us=5_000_000), track_id="video_01")  # type: ignore[arg-type]
    assert len(plan.segments) == 2
    assert plan.concat_required is True  # one LaneIR = one main source (documented)
    assert [segment.timeline_start_us for segment in plan.segments] == [0, 3_000_000]


def test_unknown_track_and_overlapping_clips_fail_closed() -> None:
    hero = _media("hero", 8_000_000)
    track = Track(
        track_id="video_01",
        name="V",
        kind="video",
        clips=[
            _clip("a", hero, (0, 3_000_000), (0, 3_000_000)),
            _clip("b", hero, (0, 3_000_000), (2_000_000, 5_000_000)),
        ],
    )
    project = _project([track], duration_us=5_000_000)
    with pytest.raises(PlanError):
        compile_execution_plan(project, track_id="ghost")  # type: ignore[arg-type]
    with pytest.raises(PlanError):
        compile_execution_plan(project, track_id="video_01")  # type: ignore[arg-type]


def test_single_segment_compiles_through_the_one_canonical_lane() -> None:
    """Plan → LaneIR → compiler → argv: the existing pipeline, no parallel path."""
    hero = _media("hero", 8_000_000)
    effects = [
        EffectLayerRef(
            operation="color.adjust_exposure",
            parameters={"exposure_ev": 1.0},
            range=TimeRangeUS(start_us=0, end_us=4_000_000),
        )
    ]
    track = Track(
        track_id="video_01",
        name="V",
        kind="video",
        clips=[_clip("c1", hero, (1_000_000, 5_000_000), (0, 4_000_000), effects)],
    )
    project = _project(
        [track],
        duration_us=4_000_000,
        assets=[
            AssetRecord(
                asset_id="hero",
                media_kind="video",
                content_sha256="sha256:hero",
                duration_us=8_000_000,
            )
        ],
    )
    plan = compile_execution_plan(project, track_id="video_01")  # type: ignore[arg-type]

    lane = segment_lane_ir(
        plan,
        plan.segments[0],
        {"hero": "/media/hero.mp4"},
        profile=LaneProfile(),
        project=project,
    )
    assert lane.main.asset_id == "hero"
    assert isinstance(lane.ops[0], TrimOp)
    assert lane.ops[0].in_us == 1_000_000  # type: ignore[union-attr]
    compiled = compile_lane(lane)
    argv_a = compiled.argv("out.mp4")
    argv_b = compiled.argv("out.mp4")
    assert argv_a == argv_b  # deterministic end to end
    assert "-filter_complex" in argv_a
    assert "/media/hero.mp4" in argv_a


def test_segment_lane_ir_requires_the_project_for_binding() -> None:
    hero = _media("hero", 8_000_000)
    track = Track(
        track_id="video_01",
        name="V",
        kind="video",
        clips=[_clip("c1", hero, (0, 4_000_000), (0, 4_000_000))],
    )
    plan = compile_execution_plan(_project([track], duration_us=4_000_000), track_id="video_01")  # type: ignore[arg-type]
    with pytest.raises(PlanError):
        segment_lane_ir(plan, plan.segments[0], {"hero": "/media/hero.mp4"})
