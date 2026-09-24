"""Plan mappings for the session-3 effect twins: title, LUT, subtitle.

Session 2 mapped ``color.adjust_exposure``; session 3 maps the three effects
whose lane twins are proven by real encodes (``motion.add_title``,
``color.apply_lut``, ``caption.burn_in``).  The contract pinned here:

* twin present + asset staged → the exact lane op, parameters threaded;
* twin present + asset missing → typed :class:`PlanError` naming the clip
  (never an unmapped entry, never a fake op);
* no twin → ``unmapped_effects`` (the honest path, unchanged);
* segments own freshly constructed ops — compiling twice, or mutating one
  plan's ops, cannot leak state into another (E1 isolation).
"""

from __future__ import annotations

import pytest

from nexus_ai_agent.creative.luts import library_path
from nexus_ai_agent.creative.rendering.ir import (
    ExposureOp,
    LutOp,
    SubtitleOp,
    TitleOp,
)
from nexus_ai_agent.creative.rendering.plan import (
    EFFECT_TO_LANE_OP,
    PlanError,
    compile_execution_plan,
)
from nexus_ai_agent.creative.studio.models import (
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


def _effect(operation: str, parameters: dict[str, object]) -> EffectLayerRef:
    return EffectLayerRef(
        operation=operation,
        parameters=parameters,
        range=TimeRangeUS(start_us=0, end_us=4_000_000),
    )


def _clip(clip_id: str, effects: list[EffectLayerRef]) -> Clip:
    return Clip(
        clip_id=clip_id,
        media_ref=_media("hero", 8_000_000),
        source_range=TimeRangeUS(start_us=1_000_000, end_us=5_000_000),
        timeline_range=TimeRangeUS(start_us=0, end_us=4_000_000),
        effects=effects,
    )


def _project(clips: list[Clip]) -> object:
    track = Track(track_id="video_01", name="V", kind="video", clips=clips)
    timeline = Timeline(timeline_id="tl", duration_us=4_000_000, tracks=[track])
    return new_project("p", "Twin Project", timeline)


def _compile(clips: list[Clip], **kwargs: object) -> object:
    return compile_execution_plan(_project(clips), track_id="video_01", **kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# the twin map itself
# ---------------------------------------------------------------------------


def test_twin_map_has_exactly_the_proven_twins() -> None:
    assert EFFECT_TO_LANE_OP == {
        "color.adjust_exposure": "exposure",
        "motion.add_title": "title",
        "color.apply_lut": "lut",
        "caption.burn_in": "subtitle",
    }


# ---------------------------------------------------------------------------
# title
# ---------------------------------------------------------------------------


def test_title_effect_maps_with_explicit_parameters() -> None:
    plan = _compile(
        [
            _clip(
                "c1",
                [
                    _effect(
                        "motion.add_title",
                        {
                            "text": "سلام",
                            "font_size": 96,
                            "color": "#FF0000",
                            "position": "center",
                            "start_us": 500_000,
                            "end_us": 2_000_000,
                        },
                    )
                ],
            )
        ]
    )
    (op,) = plan.segments[0].ops
    assert isinstance(op, TitleOp)
    assert op.text == "سلام"
    assert (op.font_size, op.color, op.position) == (96, "#FF0000", "center")
    assert (op.start_us, op.end_us) == (500_000, 2_000_000)
    assert plan.unmapped_effects == ()


def test_title_effect_defaults_cover_the_whole_segment() -> None:
    plan = _compile([_clip("c1", [_effect("motion.add_title", {"text": "hi"})])])
    (op,) = plan.segments[0].ops
    assert isinstance(op, TitleOp)
    assert (op.start_us, op.end_us) == (0, None)


def test_title_without_text_is_a_plan_error_naming_the_clip() -> None:
    with pytest.raises(PlanError, match="c1.*motion.add_title.*text"):
        _compile([_clip("c1", [_effect("motion.add_title", {"font_size": 10})])])


# ---------------------------------------------------------------------------
# LUT
# ---------------------------------------------------------------------------


def test_lut_effect_maps_when_staged() -> None:
    staged = {"warm": str(library_path("warm"))}
    plan = _compile(
        [_clip("c1", [_effect("color.apply_lut", {"lut_name": "warm", "intensity": 0.5})])],
        lut_paths=staged,
    )
    (op,) = plan.segments[0].ops
    assert isinstance(op, LutOp)
    assert (op.lut_name, op.lut_path, op.intensity) == ("warm", staged["warm"], 0.5)
    assert plan.unmapped_effects == ()


def test_lut_effect_defaults_to_full_intensity() -> None:
    staged = {"warm": str(library_path("warm"))}
    plan = _compile(
        [_clip("c1", [_effect("color.apply_lut", {"lut_name": "warm"})])], lut_paths=staged
    )
    (op,) = plan.segments[0].ops
    assert isinstance(op, LutOp)
    assert op.intensity == 1.0


def test_unstaged_lut_is_a_plan_error_not_an_unmapped_entry() -> None:
    with pytest.raises(PlanError, match="c9.*warm.*not staged"):
        _compile(
            [_clip("c9", [_effect("color.apply_lut", {"lut_name": "warm"})])],
            lut_paths={"identity": str(library_path("identity"))},
        )


def test_lut_without_name_is_a_plan_error() -> None:
    with pytest.raises(PlanError, match="lut_name"):
        _compile(
            [_clip("c1", [_effect("color.apply_lut", {})])],
            lut_paths={"warm": str(library_path("warm"))},
        )


# ---------------------------------------------------------------------------
# subtitle
# ---------------------------------------------------------------------------


def test_subtitle_effect_maps_when_staged(tmp_path: object) -> None:
    from pathlib import Path

    staged_file = Path(str(tmp_path)) / "cap.srt"
    staged_file.write_text("1\n00:00:00,000 --> 00:00:01,000\nHi\n")
    plan = _compile(
        [_clip("c1", [_effect("caption.burn_in", {"caption_asset_id": "cap1"})])],
        subtitle_files={"cap1": str(staged_file)},
    )
    (op,) = plan.segments[0].ops
    assert isinstance(op, SubtitleOp)
    assert op.subtitle_path == str(staged_file)
    assert plan.unmapped_effects == ()


def test_unstaged_caption_is_a_plan_error_not_an_unmapped_entry() -> None:
    with pytest.raises(PlanError, match="c7.*cap9.*no staged subtitle file"):
        _compile(
            [_clip("c7", [_effect("caption.burn_in", {"caption_asset_id": "cap9"})])],
            subtitle_files={},
        )


# ---------------------------------------------------------------------------
# mixed segments + E1 isolation
# ---------------------------------------------------------------------------


def test_mixed_segment_maps_twins_and_reports_the_rest() -> None:
    staged = {"warm": str(library_path("warm"))}
    plan = _compile(
        [
            _clip(
                "c1",
                [
                    _effect("color.adjust_exposure", {"exposure_ev": 1.0}),
                    _effect("motion.add_title", {"text": "t"}),
                    _effect("color.apply_lut", {"lut_name": "warm"}),
                    _effect("color.auto_balance", {}),
                ],
            )
        ],
        lut_paths=staged,
    )
    kinds = [type(op).__name__ for op in plan.segments[0].ops]
    assert kinds == ["ExposureOp", "TitleOp", "LutOp"]
    assert plan.unmapped_effects == ("c1:color.auto_balance",)


def test_segment_compilation_is_referentially_isolated() -> None:
    """E1: two compiles share no mutable op state; the project is untouched."""
    clip_b = Clip(
        clip_id="c2",
        media_ref=_media("hero", 8_000_000),
        source_range=TimeRangeUS(start_us=1_000_000, end_us=5_000_000),
        timeline_range=TimeRangeUS(start_us=4_000_000, end_us=8_000_000),
        effects=[_effect("motion.add_title", {"text": "same"})],
    )
    track = Track(
        track_id="v",
        name="V",
        kind="video",
        clips=[_clip("c1", [_effect("motion.add_title", {"text": "same"})]), clip_b],
    )
    timeline = Timeline(timeline_id="tl", duration_us=8_000_000, tracks=[track])
    proj = new_project("p", "Iso", timeline)
    before = proj.model_dump(mode="json")

    first = compile_execution_plan(proj, track_id="v")
    second = compile_execution_plan(proj, track_id="v")
    assert first.plan_hash == second.plan_hash
    # Distinct objects per compile and per segment — no aliasing anywhere.
    assert first.segments[0].ops[0] is not second.segments[0].ops[0]
    assert first.segments[0].ops[0] is not first.segments[1].ops[0]
    assert isinstance(first.segments[0].ops[0], TitleOp)
    assert isinstance(first.segments[1].ops[0], TitleOp)
    # The project state is byte-identical after compiling twice.
    assert proj.model_dump(mode="json") == before
    assert isinstance(first.segments[0].ops[0], ExposureOp) is False
