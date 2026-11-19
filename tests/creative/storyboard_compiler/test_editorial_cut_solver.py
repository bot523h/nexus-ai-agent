"""Tests for J/L-Cut editorial timing, eye-trace continuity, SHA-256 manifests, and purity."""

from __future__ import annotations

import ast
import dataclasses
import json
import math
import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

import nexus_ai_agent.creative.storyboard_compiler as sb
from nexus_ai_agent.creative.storyboard_compiler import (
    DEFAULT_MAX_EYE_TRACE_SACCADE,
    J_CUT_MAX_SEC,
    J_CUT_MIN_SEC,
    L_CUT_MAX_SEC,
    L_CUT_MIN_SEC,
    MANIFEST_SCHEMA_VERSION,
    CutType,
    EditorialCutError,
    PacingProfile,
    ScreenLookDirection,
    StoryboardCompilerConfig,
    build_compiled_manifest,
    compile_storyboard,
    parse_bilingual_script,
    solve_editorial_timeline,
    solve_eye_trace_transition,
    solve_script_cinematography,
    verify_editorial_timeline,
    verify_manifest_integrity,
    verify_manifest_integrity_strict,
)

DIALOGUE_AND_REACTION_SCRIPT = """\
داخلی. کافه نادری - شب - بارانی

قطرات باران آرام روی شیشهٔ بخارگرفته می‌لغزند.

سارا
(زمزمه)
فکر نمی‌کردم امشب به این قرار بیایی، خیلی دیر کردی.

امیر
(قاطع)
باید قبل از سفر آخرین حرف‌هایمان را رو در رو می‌زدیم.

سارا
(غمگین)
تمام این سال‌ها منتظر یک توضیح ساده بودم اما سکوت کردی.

امیر
(با خشم)
سکوت کردم چون جان تو در خطر بود! چرا نمی‌فهمی؟!

سارا به فنجان سرد قهوه خیره می‌شود.

سارا
(آرام)
می‌خواستم حقیقت را از زبان خودت بشنوم نه از غریبه‌ها.

امیر
(آرام)
حالا تمام مدارک اینجاست و همهٔ واقعیت روشن شده است.

امیر
(قاطع)
تصمیم نهایی با خود توست، من دیگر اصراری نمی‌کنم.

خارجی. خیابان جمهوری - شب - بارانی

سارا
(با تردید)
صبر کن... هنوز یک سوال مهم باقی مانده است.

امیر
(با لبخند)
تمام شب وقت داریم، هر چه می‌خواهی بپرس.
"""


# --------------------------------------------------------------------------- #
# J-Cut, L-Cut & Strict Non-Overlap Invariants
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("breath_gap", [0.0, 0.04])
def test_jl_cuts_obey_exact_window_and_zero_illegal_audio_overlap(breath_gap: float) -> None:
    cfg = StoryboardCompilerConfig(breath_gap_sec=breath_gap, allow_scene_prelap=True)
    manifest = compile_storyboard(DIALOGUE_AND_REACTION_SCRIPT, config=cfg)
    plan = manifest.editorial

    assert plan.verification.compliant is True
    assert plan.verification.video_contiguous is True
    assert plan.verification.audio_overlap_free is True
    assert plan.verification.jl_bounds_valid is True
    assert plan.verification.eye_trace_valid is True
    assert plan.verification.max_audio_overlap_sec == 0.0

    # Must produce real J-Cuts, L-Cuts, and Straight Cuts across this multi-beat dialogue.
    assert plan.j_cut_count >= 3
    assert plan.l_cut_count >= 3
    assert plan.straight_cut_count >= 1
    assert plan.j_cut_count + plan.l_cut_count + plan.straight_cut_count == len(plan.transitions)

    events = plan.events
    assert events[0].video_in_sec == 0.0
    assert events[0].audio_in_sec == 0.0
    assert events[0].entry_cut_type is None
    assert events[-1].exit_cut_type is None
    assert events[-1].video_out_sec == pytest.approx(plan.total_duration_sec, abs=1e-6)
    assert events[-1].audio_out_sec == pytest.approx(plan.total_duration_sec, abs=1e-6)

    # Verify pairwise interior-disjointness of ALL audio segments (i < j => out_i <= in_j).
    for i in range(len(events)):
        assert events[i].video_duration_sec > 0.0
        assert events[i].audio_duration_sec > 0.0
        for j in range(i + 1, len(events)):
            assert events[i].audio_out_sec <= events[j].audio_in_sec + 1e-9

    # Verify exact J-Cut [0.25, 0.60] pre-lap and L-Cut [0.25, 0.60] post-lap geometry.
    for tr in plan.transitions:
        out_ev = events[tr.transition_index]
        in_ev = events[tr.transition_index + 1]
        assert out_ev.video_out_sec == pytest.approx(tr.video_cut_sec, abs=1e-6)
        assert in_ev.video_in_sec == pytest.approx(tr.video_cut_sec, abs=1e-6)

        if tr.cut_type == CutType.J_CUT:
            prelap = tr.video_cut_sec - in_ev.audio_in_sec
            assert J_CUT_MIN_SEC - 1e-6 <= prelap <= J_CUT_MAX_SEC + 1e-6
            assert tr.j_cut_prelap_sec == pytest.approx(prelap, abs=1e-6)
            assert tr.l_cut_postlap_sec == 0.0
            assert tr.audio_offset_sec == pytest.approx(-prelap, abs=1e-6)
            assert out_ev.audio_out_sec == pytest.approx(in_ev.audio_in_sec - breath_gap, abs=1e-6)
        elif tr.cut_type == CutType.L_CUT:
            postlap = out_ev.audio_out_sec - tr.video_cut_sec
            assert L_CUT_MIN_SEC - 1e-6 <= postlap <= L_CUT_MAX_SEC + 1e-6
            assert tr.l_cut_postlap_sec == pytest.approx(postlap, abs=1e-6)
            assert tr.j_cut_prelap_sec == 0.0
            assert tr.audio_offset_sec == pytest.approx(postlap, abs=1e-6)
            assert in_ev.audio_in_sec == pytest.approx(out_ev.audio_out_sec + breath_gap, abs=1e-6)
        else:
            assert tr.j_cut_prelap_sec == 0.0
            assert tr.l_cut_postlap_sec == 0.0
            assert in_ev.audio_in_sec == pytest.approx(tr.video_cut_sec, abs=1e-6)
            assert out_ev.audio_out_sec <= in_ev.audio_in_sec + 1e-9


def test_balanced_volley_scene_prelap_disabled_micro_shots_and_pacing_profiles() -> None:
    # Balanced tension conversational exchange exercises alternating J/L and tension deltas.
    volley = """\
INT. OFFICE - DAY
ALEX
We received the message this morning from headquarters.

BETH
They asked us to review the contract before Friday.

ALEX
Every single clause looks reasonable to our legal team.

BETH
Then we should sign it and notify the board today.

ALEX
(whispering)
Keep the appendix confidential until the press release drops.

BETH
Understood, nobody outside this room will see those numbers.

EXT. COURTYARD - DAY
Rain taps softly.

The fountain shimmers in the daylight.

INT. LOBBY - DAY
ALEX
No!

BETH
Go!
"""
    cfg = StoryboardCompilerConfig(allow_scene_prelap=False)
    manifest = compile_storyboard(volley, config=cfg)
    assert manifest.editorial.verification.compliant is True
    # Scene boundaries must all be STRAIGHT_CUT when allow_scene_prelap is False.
    for tr in manifest.editorial.transitions:
        if tr.scene_boundary:
            assert tr.cut_type == CutType.STRAIGHT_CUT
    # Final ultra-brief "No!" -> "Go!" safely degrades to STRAIGHT_CUT with zero overlap.
    assert manifest.editorial.transitions[-1].cut_type == CutType.STRAIGHT_CUT

    # Test KINETIC, DYNAMIC, CONVERSATIONAL, and CONTEMPLATIVE pacing profiles.
    kinetic = compile_storyboard("INT. RING - NIGHT\nA\nGo!\n\nB\nNow!\n\nA\nHit!")
    assert kinetic.editorial.pacing_profile == PacingProfile.KINETIC

    dynamic = compile_storyboard(
        "INT. HALL - DAY\nA\nOpen the front door quickly.\n\nB\nWait right behind me now."
    )
    assert dynamic.editorial.pacing_profile == PacingProfile.DYNAMIC

    contemplative = compile_storyboard(
        "EXT. VALLEY - DAWN\n"
        "A lone traveler walks slowly across the wide golden meadow as morning wind bends the "
        "tall grass beside the silent river and distant mountains glow beneath pale clouds."
    )
    assert contemplative.editorial.pacing_profile == PacingProfile.CONTEMPLATIVE
    assert contemplative.editorial.transitions == ()
    assert contemplative.editorial.mean_eye_trace_score == 1.0


def test_tension_delta_selection_in_dialogue() -> None:
    script = """\
INT. LIBRARY - DAY
ANA
Good morning friend, please take a seat beside the window.

BEN
(firmly)
We must close the archive before noon today without delay.

ANA
All right, I will gather the remaining folders right away.
"""
    m = compile_storyboard(script)
    assert m.editorial.transitions[0].cut_type == CutType.J_CUT
    assert m.editorial.transitions[1].cut_type == CutType.L_CUT


# --------------------------------------------------------------------------- #
# Eye-Trace Continuity & Guided Euclidean-Ball Reframing
# --------------------------------------------------------------------------- #


def test_eye_trace_continuity_enforces_max_saccade_and_eyeline_matching() -> None:
    # Reverse shot on identical upper third (1/3 -> 2/3): d = 1/3 <= 0.38, matched eyelines.
    smooth = solve_eye_trace_transition(
        transition_index=0,
        from_shot_index=0,
        to_shot_index=1,
        exit_focus_uv=(1.0 / 3.0, 1.0 / 3.0),
        entry_focus_uv=(2.0 / 3.0, 1.0 / 3.0),
        out_look=ScreenLookDirection.RIGHT,
        in_look=ScreenLookDirection.LEFT,
        max_saccade_uv=DEFAULT_MAX_EYE_TRACE_SACCADE,
    )
    assert smooth.reframe_applied is False
    assert smooth.eyeline_matched is True
    assert smooth.effective_saccade_uv == pytest.approx(1.0 / 3.0, abs=1e-5)
    assert 0.7 < smooth.continuity_score <= 1.0

    # Extreme corner-to-corner jump (0.10, 0.10) -> (0.90, 0.90): clamped to exact radius.
    jarring = solve_eye_trace_transition(
        transition_index=1,
        from_shot_index=1,
        to_shot_index=2,
        exit_focus_uv=(0.10, 0.10),
        entry_focus_uv=(0.90, 0.90),
        out_look=ScreenLookDirection.RIGHT,
        in_look=ScreenLookDirection.RIGHT,
        max_saccade_uv=0.30,
    )
    assert jarring.reframe_applied is True
    assert jarring.raw_saccade_uv > 1.1
    assert jarring.effective_saccade_uv == pytest.approx(0.30, abs=1e-5)
    assert jarring.eyeline_matched is False

    # Same-side look without vertical jump still reports eyeline mismatch when axis intact.
    same_look = solve_eye_trace_transition(
        transition_index=2,
        from_shot_index=2,
        to_shot_index=3,
        exit_focus_uv=(0.4, 0.33),
        entry_focus_uv=(0.5, 0.33),
        out_look=ScreenLookDirection.LEFT,
        in_look=ScreenLookDirection.LEFT,
    )
    assert same_look.eyeline_matched is False

    # Tight saccade threshold across an atmospheric establishing -> close-up cut reframes cleanly.
    tight_cfg = StoryboardCompilerConfig(max_eye_trace_saccade=0.22)
    m = compile_storyboard(DIALOGUE_AND_REACTION_SCRIPT, config=tight_cfg)
    assert m.eye_trace_compliant is True
    assert any(tr.eye_trace.reframe_applied for tr in m.editorial.transitions)
    for tr in m.editorial.transitions:
        assert tr.eye_trace.effective_saccade_uv <= 0.22 + 1e-5

    with pytest.raises(EditorialCutError, match="max_saccade_uv"):
        solve_eye_trace_transition(
            transition_index=0,
            from_shot_index=0,
            to_shot_index=1,
            exit_focus_uv=(0.5, 0.5),
            entry_focus_uv=(0.5, 0.5),
            max_saccade_uv=0.01,
        )
    with pytest.raises(EditorialCutError, match="finite"):
        solve_eye_trace_transition(
            transition_index=0,
            from_shot_index=0,
            to_shot_index=1,
            exit_focus_uv=(float("nan"), 0.5),
            entry_focus_uv=(0.5, 0.5),
        )


# --------------------------------------------------------------------------- #
# Editorial Verifier Tamper Detection & Refusals
# --------------------------------------------------------------------------- #


def test_editorial_verifier_catches_all_tampered_timeline_defects() -> None:
    base = compile_storyboard(
        "INT. ROOM - NIGHT\n"
        "A\n(sadly)\nI waited by the window for three long hours.\n\n"
        "B\n(angrily)\nThe bridge collapsed before I could cross it!\n\n"
        "B\nNobody else made it out tonight."
    ).editorial
    assert base.verification.compliant is True

    # Empty events list.
    empty_rep = verify_editorial_timeline(dataclasses.replace(base, events=(), transitions=()))
    assert empty_rep.compliant is False and not empty_rep.video_contiguous

    # Tamper audio overlap, video gap, out-of-bounds audio, non-positive durations, bad J/L/S cuts.
    ev0, ev1, ev2 = base.events
    bad_ev0 = dataclasses.replace(
        ev0,
        video_in_sec=0.1,
        video_out_sec=0.05,
        audio_in_sec=-0.2,
        audio_out_sec=ev1.audio_in_sec + 0.5,
    )
    bad_ev1 = dataclasses.replace(ev1, audio_out_sec=ev1.audio_in_sec)
    bad_ev2 = dataclasses.replace(ev2, audio_in_sec=ev2.video_in_sec + 0.2, video_out_sec=99.0)
    bad_eye = dataclasses.replace(base.transitions[0].eye_trace, effective_saccade_uv=0.95)
    bad_tr0 = dataclasses.replace(base.transitions[0], cut_type=CutType.L_CUT, eye_trace=bad_eye)
    bad_tr1 = dataclasses.replace(base.transitions[1], cut_type=CutType.STRAIGHT_CUT)
    tampered = dataclasses.replace(
        base, events=(bad_ev0, bad_ev1, bad_ev2), transitions=(bad_tr0, bad_tr1)
    )
    rep = verify_editorial_timeline(tampered)
    assert rep.compliant is False
    assert rep.video_contiguous is False
    assert rep.audio_overlap_free is False
    assert rep.jl_bounds_valid is False
    assert rep.eye_trace_valid is False
    assert rep.max_audio_overlap_sec > 0.0

    # Also catch an out-of-range J-Cut specifically.
    bad_j = dataclasses.replace(
        base,
        transitions=(
            dataclasses.replace(base.transitions[0], cut_type=CutType.J_CUT),
            base.transitions[1],
        ),
        events=(ev0, dataclasses.replace(ev1, audio_in_sec=ev1.video_in_sec - 0.95), ev2),
    )
    assert verify_editorial_timeline(bad_j).jl_bounds_valid is False


def test_editorial_solver_and_manifest_input_refusals() -> None:
    parsed = parse_bilingual_script("INT. ROOM - DAY\nA\nHello world.")
    plans = solve_script_cinematography(parsed)
    editorial = solve_editorial_timeline(plans)

    with pytest.raises(EditorialCutError, match="no shots"):
        solve_editorial_timeline(())
    with pytest.raises(EditorialCutError, match="finite"):
        solve_editorial_timeline(plans, j_cut_min_sec=float("nan"))
    with pytest.raises(EditorialCutError, match="J-Cut"):
        solve_editorial_timeline(plans, j_cut_min_sec=0.10)
    with pytest.raises(EditorialCutError, match="L-Cut"):
        solve_editorial_timeline(plans, l_cut_max_sec=0.90)
    with pytest.raises(EditorialCutError, match="breath_gap_sec"):
        solve_editorial_timeline(plans, breath_gap_sec=0.5)
    with pytest.raises(EditorialCutError, match="max_eye_trace_saccade"):
        solve_editorial_timeline(plans, max_eye_trace_saccade=2.0)

    bad_shot = dataclasses.replace(plans[0].shots[0], duration_sec=0.0)
    bad_plan = dataclasses.replace(plans[0], shots=(bad_shot,))
    with pytest.raises(EditorialCutError, match="duration"):
        solve_editorial_timeline((bad_plan,))

    with pytest.raises(EditorialCutError, match="scene count"):
        build_compiled_manifest(parsed, (), editorial)
    with pytest.raises(EditorialCutError, match="shot count"):
        build_compiled_manifest(parsed, plans, dataclasses.replace(editorial, events=()))


# --------------------------------------------------------------------------- #
# SHA-256 Manifest Determinism, Tamper Evidence & Strict Dependency Purity
# --------------------------------------------------------------------------- #


def test_compiled_manifest_sha256_is_deterministic_and_tamper_evident() -> None:
    m1 = compile_storyboard(DIALOGUE_AND_REACTION_SCRIPT)
    m2 = compile_storyboard(DIALOGUE_AND_REACTION_SCRIPT.replace("\n", "\r\n").replace("ی", "ي"))

    assert m1.schema_version == MANIFEST_SCHEMA_VERSION
    assert m1.axis_180_compliant is True
    assert m1.editorial_compliant is True
    assert m1.eye_trace_compliant is True
    assert m1.total_scenes == 2
    assert m1.total_shots == len(m1.editorial.events)
    assert verify_manifest_integrity(m1) is True
    assert verify_manifest_integrity(m2) is True

    # Identical canonical output and SHA-256 across normalized CRLF / Arabic-Yeh equivalents.
    assert m1.manifest_sha256 == m2.manifest_sha256
    assert m1.to_canonical_json() == m2.to_canonical_json()

    decoded = json.loads(m1.to_canonical_json())
    assert decoded["manifest_sha256"] == m1.manifest_sha256
    assert decoded["total_shots"] == m1.total_shots

    # Mutating one hex character, invalid hex string, or a single timeline float fails check.
    flipped = ("0" if m1.manifest_sha256[0] != "0" else "1") + m1.manifest_sha256[1:]
    assert verify_manifest_integrity(dataclasses.replace(m1, manifest_sha256=flipped)) is False
    assert verify_manifest_integrity(dataclasses.replace(m1, script_sha256="not-a-hash")) is False

    mutated_edit = dataclasses.replace(m1.editorial, total_duration_sec=m1.total_duration_sec + 1.0)
    assert verify_manifest_integrity(dataclasses.replace(m1, editorial=mutated_edit)) is False

    mutated_shot = dataclasses.replace(m1.cinematography[0].shots[0], axis_cross_z=99.0)
    mutated_plan = dataclasses.replace(
        m1.cinematography[0], shots=(mutated_shot, *m1.cinematography[0].shots[1:])
    )
    assert (
        verify_manifest_integrity(
            dataclasses.replace(m1, cinematography=(mutated_plan, *m1.cinematography[1:]))
        )
        is False
    )

    mutated_script = dataclasses.replace(m1.script, total_duration_sec=999.0)
    assert verify_manifest_integrity(dataclasses.replace(m1, script=mutated_script)) is False

    # F1: Tampering with ANY of the 5 top-level summary fields on CompiledStoryboardManifest
    # (even if an adversary also recomputes manifest_sha256!) MUST return False.
    from nexus_ai_agent.creative.storyboard_compiler.editorial_cut_solver import _compute_digests

    summary_tamper_cases = [
        dataclasses.replace(m1, axis_180_compliant=not m1.axis_180_compliant),
        dataclasses.replace(m1, editorial_compliant=not m1.editorial_compliant),
        dataclasses.replace(m1, eye_trace_compliant=not m1.eye_trace_compliant),
        dataclasses.replace(m1, total_shots=m1.total_shots + 1),
        dataclasses.replace(m1, total_duration_sec=m1.total_duration_sec + 0.5),
    ]
    for tampered in summary_tamper_cases:
        # 1) Direct field mutation fails immediately.
        assert verify_manifest_integrity(tampered) is False
        # 2) Even if manifest_sha256 is recomputed over the tampered summary field,
        # live re-derivation from script/shots/timeline still rejects it.
        _, _, _, forged_manifest_sha, _ = _compute_digests(
            tampered.schema_version,
            tampered.script,
            tampered.cinematography,
            tampered.editorial,
            total_scenes=tampered.total_scenes,
            total_shots=tampered.total_shots,
            total_duration_sec=tampered.total_duration_sec,
            axis_180_compliant=tampered.axis_180_compliant,
            editorial_compliant=tampered.editorial_compliant,
            eye_trace_compliant=tampered.eye_trace_compliant,
        )
        forged = dataclasses.replace(tampered, manifest_sha256=forged_manifest_sha)
        assert verify_manifest_integrity(forged) is False


def _reseal_tampered_manifest(
    manifest: sb.CompiledStoryboardManifest,
) -> sb.CompiledStoryboardManifest:
    """Helper simulating an adversary who recomputes all 4 SHA-256 hashes after tampering."""
    from nexus_ai_agent.creative.storyboard_compiler.editorial_cut_solver import _compute_digests

    s_sha, c_sha, e_sha, m_sha, _ = _compute_digests(
        manifest.schema_version,
        manifest.script,
        manifest.cinematography,
        manifest.editorial,
        total_scenes=manifest.total_scenes,
        total_shots=manifest.total_shots,
        total_duration_sec=manifest.total_duration_sec,
        axis_180_compliant=manifest.axis_180_compliant,
        editorial_compliant=manifest.editorial_compliant,
        eye_trace_compliant=manifest.eye_trace_compliant,
    )
    return dataclasses.replace(
        manifest,
        script_sha256=s_sha,
        cinematography_sha256=c_sha,
        editorial_sha256=e_sha,
        manifest_sha256=m_sha,
    )


def test_editorial_transition_cardinality_and_chain_tamper_detection() -> None:
    # F1-R2: Deleting all transitions, dropping one middle transition, or swapping two
    # transitions in a multi-shot timeline MUST fail both verify_editorial_timeline
    # (compliant=False and eye_trace_valid=False) and verify_manifest_integrity.
    manifest = compile_storyboard(DIALOGUE_AND_REACTION_SCRIPT)
    plan = manifest.editorial
    assert len(plan.events) >= 4
    assert len(plan.transitions) == len(plan.events) - 1

    # Case 1: Empty transitions=() on a multi-shot timeline.
    empty_tr_plan = dataclasses.replace(plan, transitions=())
    rep_empty = verify_editorial_timeline(empty_tr_plan)
    assert rep_empty.compliant is False
    assert rep_empty.eye_trace_valid is False
    assert rep_empty.jl_bounds_valid is False
    assert any("transition count mismatch" in v for v in rep_empty.violations)
    assert (
        verify_manifest_integrity(
            _reseal_tampered_manifest(dataclasses.replace(manifest, editorial=empty_tr_plan))
        )
        is False
    )

    # Case 2: Dropping a middle transition (transitions[:1] + transitions[2:]).
    dropped_tr_plan = dataclasses.replace(
        plan,
        transitions=(plan.transitions[0], *plan.transitions[2:]),
    )
    rep_dropped = verify_editorial_timeline(dropped_tr_plan)
    assert rep_dropped.compliant is False
    assert rep_dropped.eye_trace_valid is False
    assert any("transition count mismatch" in v for v in rep_dropped.violations)
    assert (
        verify_manifest_integrity(
            _reseal_tampered_manifest(dataclasses.replace(manifest, editorial=dropped_tr_plan))
        )
        is False
    )

    # Case 3: Swapping the order of two transitions (preserving count == len(events) - 1).
    swapped_tr_plan = dataclasses.replace(
        plan,
        transitions=(plan.transitions[1], plan.transitions[0], *plan.transitions[2:]),
    )
    rep_swapped = verify_editorial_timeline(swapped_tr_plan)
    assert rep_swapped.compliant is False
    assert rep_swapped.eye_trace_valid is False
    assert any("broken shot chain" in v for v in rep_swapped.violations)
    assert (
        verify_manifest_integrity(
            _reseal_tampered_manifest(dataclasses.replace(manifest, editorial=swapped_tr_plan))
        )
        is False
    )


def test_deep_scene_graph_report_and_trajectory_endpoint_integrity() -> None:
    # F1-R1: Verify scene-by-scene bijection, full cached report matching, and trajectory
    # boundary pose + zero velocity/acceleration checks inside verify_manifest_integrity.
    manifest = compile_storyboard(DIALOGUE_AND_REACTION_SCRIPT)
    assert verify_manifest_integrity(manifest) is True

    # 1. Moving the last shot of scene 0 into scene 1 (total_shots stays identical!) fails.
    p0, p1 = manifest.cinematography[0], manifest.cinematography[1]
    moved_p0 = dataclasses.replace(p0, shots=p0.shots[:-1])
    moved_p1 = dataclasses.replace(p1, shots=(p0.shots[-1], *p1.shots))
    moved_manifest = _reseal_tampered_manifest(
        dataclasses.replace(manifest, cinematography=(moved_p0, moved_p1))
    )
    assert verify_manifest_integrity(moved_manifest) is False

    # 2. Tampering cached axis_report.reflected_shot_indices (leaving compliant=True) fails.
    forged_axis_rep = dataclasses.replace(p0.axis_report, reflected_shot_indices=(0,))
    bad_axis_plan = dataclasses.replace(p0, axis_report=forged_axis_rep)
    assert (
        verify_manifest_integrity(
            _reseal_tampered_manifest(
                dataclasses.replace(manifest, cinematography=(bad_axis_plan, p1))
            )
        )
        is False
    )

    # 3. Tampering cached editorial.verification.max_saccade_jump (leaving compliant=True) fails.
    forged_edit_ver = dataclasses.replace(
        manifest.editorial.verification,
        max_saccade_jump=manifest.editorial.verification.max_saccade_jump + 0.25,
    )
    bad_edit = dataclasses.replace(manifest.editorial, verification=forged_edit_ver)
    assert (
        verify_manifest_integrity(
            _reseal_tampered_manifest(dataclasses.replace(manifest, editorial=bad_edit))
        )
        is False
    )

    # 4. Corrupting final trajectory sample pose away from end_pose OR non-zero endpoint velocity.
    sh0 = p0.shots[0]
    shifted_end_sample = dataclasses.replace(
        sh0.trajectory.samples[-1],
        linear_velocity=(0.05, 0.0, 0.0),
    )
    bad_traj = dataclasses.replace(
        sh0.trajectory,
        samples=(*sh0.trajectory.samples[:-1], shifted_end_sample),
    )
    bad_sh0 = dataclasses.replace(sh0, trajectory=bad_traj)
    bad_traj_plan = dataclasses.replace(p0, shots=(bad_sh0, *p0.shots[1:]))
    assert (
        verify_manifest_integrity(
            _reseal_tampered_manifest(
                dataclasses.replace(manifest, cinematography=(bad_traj_plan, p1))
            )
        )
        is False
    )


def _reseal_with_recomputed_reports(
    manifest: sb.CompiledStoryboardManifest,
) -> sb.CompiledStoryboardManifest:
    """Strongest adversary: recomputes cached reports, compliance flags, AND all 4 SHA-256s."""
    rebuilt_plans = tuple(
        dataclasses.replace(plan, axis_report=sb.verify_180_degree_rule(plan.shots, plan.axis))
        for plan in manifest.cinematography
    )
    all_shots = [sh for plan in rebuilt_plans for sh in plan.shots]
    live_edit = verify_editorial_timeline(manifest.editorial, shots=all_shots)
    rebuilt_editorial = dataclasses.replace(manifest.editorial, verification=live_edit)
    updated = dataclasses.replace(
        manifest,
        cinematography=rebuilt_plans,
        editorial=rebuilt_editorial,
        axis_180_compliant=all(p.axis_report.compliant for p in rebuilt_plans),
        editorial_compliant=live_edit.compliant,
        eye_trace_compliant=live_edit.eye_trace_valid,
    )
    return _reseal_tampered_manifest(updated)


def test_f7_manifest_rejects_tampered_axis_trace_original_position_and_distance() -> None:
    manifest = compile_storyboard(DIALOGUE_AND_REACTION_SCRIPT)
    p0, p1 = manifest.cinematography[0], manifest.cinematography[1]
    sh0, sh1 = p0.shots[0], p0.shots[1]

    # Case 1: Tamper start_axis_trace.original_position while keeping poses/sides untouched.
    orig = sh0.start_axis_trace.original_position
    bad_start_trace = dataclasses.replace(
        sh0.start_axis_trace,
        original_position=(orig[0] + 0.40, orig[1], orig[2]),
    )
    bad_p0_pos = dataclasses.replace(
        p0,
        shots=(dataclasses.replace(sh0, start_axis_trace=bad_start_trace), *p0.shots[1:]),
    )
    assert sb.verify_180_degree_rule(bad_p0_pos.shots, bad_p0_pos.axis).compliant is False
    tampered_pos = dataclasses.replace(manifest, cinematography=(bad_p0_pos, p1))
    assert verify_manifest_integrity(_reseal_tampered_manifest(tampered_pos)) is False
    assert verify_manifest_integrity(_reseal_with_recomputed_reports(tampered_pos)) is False

    # Case 2: Tamper end_axis_trace.original_signed_distance_m and reseal.
    bad_end_trace = dataclasses.replace(
        sh1.end_axis_trace,
        original_signed_distance_m=sh1.end_axis_trace.original_signed_distance_m + 0.75,
    )
    bad_p0_dist = dataclasses.replace(
        p0,
        shots=(sh0, dataclasses.replace(sh1, end_axis_trace=bad_end_trace), *p0.shots[2:]),
    )
    assert sb.verify_180_degree_rule(bad_p0_dist.shots, bad_p0_dist.axis).compliant is False
    tampered_dist = dataclasses.replace(manifest, cinematography=(bad_p0_dist, p1))
    assert verify_manifest_integrity(_reseal_tampered_manifest(tampered_dist)) is False
    assert verify_manifest_integrity(_reseal_with_recomputed_reports(tampered_dist)) is False


def test_f8_manifest_rejects_forged_eye_trace_saccade_and_derived_cut_fields() -> None:
    manifest = compile_storyboard(DIALOGUE_AND_REACTION_SCRIPT)
    ed = manifest.editorial
    tr0 = ed.transitions[0]

    # Case 1: True Euclidean jump between exit_focus_uv=(0.15, 0.30) and
    # adjusted_entry_focus_uv=(0.80, 0.30) is 0.65 (> max_eye_trace_saccade_limit),
    # while attacker forges effective_saccade_uv=0.05!
    forged_et = dataclasses.replace(
        tr0.eye_trace,
        exit_focus_uv=(0.15, 0.30),
        raw_entry_focus_uv=(0.80, 0.30),
        adjusted_entry_focus_uv=(0.80, 0.30),
        raw_saccade_uv=0.05,
        effective_saccade_uv=0.05,
        vertical_eyeline_delta=0.0,
        reframe_applied=False,
    )
    ev0_forged = dataclasses.replace(ed.events[0], exit_focus_uv=(0.15, 0.30))
    ev1_forged = dataclasses.replace(ed.events[1], entry_focus_uv=(0.80, 0.30))
    ed_saccade_lie = dataclasses.replace(
        ed,
        events=(ev0_forged, ev1_forged, *ed.events[2:]),
        transitions=(dataclasses.replace(tr0, eye_trace=forged_et), *ed.transitions[1:]),
    )
    rep_saccade = verify_editorial_timeline(ed_saccade_lie)
    assert rep_saccade.compliant is False
    assert rep_saccade.eye_trace_valid is False
    tampered_saccade = dataclasses.replace(manifest, editorial=ed_saccade_lie)
    assert verify_manifest_integrity(_reseal_tampered_manifest(tampered_saccade)) is False
    assert verify_manifest_integrity(_reseal_with_recomputed_reports(tampered_saccade)) is False

    # Case 2: Keep UV coordinates legal (<= limit), but forge effective_saccade_uv=0.05.
    ed_cached_lie = dataclasses.replace(
        ed,
        transitions=(
            dataclasses.replace(
                tr0,
                eye_trace=dataclasses.replace(tr0.eye_trace, effective_saccade_uv=0.05),
            ),
            *ed.transitions[1:],
        ),
    )
    rep_cached_lie = verify_editorial_timeline(ed_cached_lie)
    assert rep_cached_lie.compliant is False
    assert rep_cached_lie.eye_trace_valid is False
    assert (
        verify_manifest_integrity(
            _reseal_tampered_manifest(dataclasses.replace(manifest, editorial=ed_cached_lie))
        )
        is False
    )

    # Case 3: Tamper j_cut_prelap_sec (plus audio_offset_sec / scene_boundary) and reseal.
    j_idx = next(idx for idx, tr in enumerate(ed.transitions) if tr.cut_type == CutType.J_CUT)
    j_tr = ed.transitions[j_idx]
    forged_j_tr = dataclasses.replace(j_tr, j_cut_prelap_sec=j_tr.j_cut_prelap_sec + 0.12)
    ed_bad_j = dataclasses.replace(
        ed,
        transitions=(*ed.transitions[:j_idx], forged_j_tr, *ed.transitions[j_idx + 1 :]),
    )
    rep_bad_j = verify_editorial_timeline(ed_bad_j)
    assert rep_bad_j.compliant is False
    assert rep_bad_j.jl_bounds_valid is False
    tampered_j = dataclasses.replace(manifest, editorial=ed_bad_j)
    assert verify_manifest_integrity(_reseal_tampered_manifest(tampered_j)) is False
    assert verify_manifest_integrity(_reseal_with_recomputed_reports(tampered_j)) is False


def test_f9_manifest_rejects_perturbed_interior_minimum_jerk_sample() -> None:
    manifest = compile_storyboard(DIALOGUE_AND_REACTION_SCRIPT)
    p0, p1 = manifest.cinematography[0], manifest.cinematography[1]
    # Pick a shot and perturb ONLY its middle sample (tau ~ 0.5) while keeping valid SE(3)
    # and staying strictly on the valid side of the 180-degree axis!
    sh = p0.shots[0]
    samples = list(sh.trajectory.samples)
    mid_idx = len(samples) // 2
    mid_smp = samples[mid_idx]

    # 1. Perturb interior translation by 5cm along Z (preserves horizontal 180-degree side & SE(3)).
    t_orig = mid_smp.pose.translation
    pose_shifted = sb.CameraPoseSE3.from_rt(
        mid_smp.pose.rotation_matrix,
        (t_orig[0], t_orig[1], t_orig[2] + 0.05),
    )
    samples_t = list(samples)
    samples_t[mid_idx] = dataclasses.replace(mid_smp, pose=pose_shifted)
    bad_sh_t = dataclasses.replace(
        sh,
        trajectory=dataclasses.replace(sh.trajectory, samples=tuple(samples_t)),
    )
    bad_p0_t = dataclasses.replace(p0, shots=(bad_sh_t, *p0.shots[1:]))
    # Confirm 180-degree rule & SE(3) membership still pass on bad_p0_t!
    assert sb.verify_180_degree_rule(bad_p0_t.shots, bad_p0_t.axis).compliant is True
    tampered_t = dataclasses.replace(manifest, cinematography=(bad_p0_t, p1))
    assert verify_manifest_integrity(_reseal_tampered_manifest(tampered_t)) is False
    assert verify_manifest_integrity(_reseal_with_recomputed_reports(tampered_t)) is False

    # 2. Perturb interior SO(3) rotation by 2 degrees via exponential map (exact SO(3)).
    delta_r = sb.so3_exp((0.0, 0.0, math.radians(2.0)))
    pose_rotated = sb.CameraPoseSE3.from_rt(
        mid_smp.pose.rotation_matrix @ delta_r,
        mid_smp.pose.translation,
    )
    samples_r = list(samples)
    samples_r[mid_idx] = dataclasses.replace(mid_smp, pose=pose_rotated)
    bad_sh_r = dataclasses.replace(
        sh,
        trajectory=dataclasses.replace(sh.trajectory, samples=tuple(samples_r)),
    )
    bad_p0_r = dataclasses.replace(p0, shots=(bad_sh_r, *p0.shots[1:]))
    assert sb.verify_180_degree_rule(bad_p0_r.shots, bad_p0_r.axis).compliant is True
    tampered_r = dataclasses.replace(manifest, cinematography=(bad_p0_r, p1))
    assert verify_manifest_integrity(_reseal_tampered_manifest(tampered_r)) is False
    assert verify_manifest_integrity(_reseal_with_recomputed_reports(tampered_r)) is False


def test_f10_manifest_rejects_shot_to_event_and_trajectory_duration_mismatch() -> None:
    manifest = compile_storyboard(DIALOGUE_AND_REACTION_SCRIPT)
    p0, p1 = manifest.cinematography[0], manifest.cinematography[1]
    sh0, sh1 = p0.shots[0], p0.shots[1]

    # Case 1: Zero-sum duration swap (+0.15s on shot 0, -0.15s on shot 1) so total_duration_sec
    # stays identical, while editorial events retain original durations -> F10 rejects!
    swapped_p0 = dataclasses.replace(
        p0,
        shots=(
            dataclasses.replace(sh0, duration_sec=round(sh0.duration_sec + 0.15, 6)),
            dataclasses.replace(sh1, duration_sec=round(sh1.duration_sec - 0.15, 6)),
            *p0.shots[2:],
        ),
    )
    tampered_swap = dataclasses.replace(manifest, cinematography=(swapped_p0, p1))
    assert verify_manifest_integrity(_reseal_tampered_manifest(tampered_swap)) is False

    # Case 2: Trajectory duration_sec desynchronized from shot.duration_sec -> F10 rejects!
    desync_traj_sh0 = dataclasses.replace(
        sh0,
        trajectory=dataclasses.replace(sh0.trajectory, duration_sec=sh0.duration_sec + 0.20),
    )
    desync_p0 = dataclasses.replace(p0, shots=(desync_traj_sh0, *p0.shots[1:]))
    tampered_traj_dur = dataclasses.replace(manifest, cinematography=(desync_p0, p1))
    assert verify_manifest_integrity(_reseal_tampered_manifest(tampered_traj_dur)) is False


def test_wave4_f8_radial_projection_eyeline_and_camera_framing_binding() -> None:
    # 1. Radial projection forgery: compile with tight r_max=0.22 so reframe_applied=True,
    #    then move adjusted_entry_focus_uv INSIDE the allowed ball (to exit_focus_uv, eff=0.0)
    #    with consistent effective_saccade_uv, vertical_eyeline_delta, continuity_score, and
    #    events[k+1].entry_focus_uv! Exact radial projection check MUST reject!
    tight_manifest = compile_storyboard(
        DIALOGUE_AND_REACTION_SCRIPT,
        config=StoryboardCompilerConfig(max_eye_trace_saccade=0.22),
    )
    assert verify_manifest_integrity(tight_manifest) is True
    ed = tight_manifest.editorial
    ref_idx = next(i for i, tr in enumerate(ed.transitions) if tr.eye_trace.reframe_applied)
    ref_tr = ed.transitions[ref_idx]
    exit_pt = ref_tr.eye_trace.exit_focus_uv

    # Forge adjusted_entry_focus_uv = exit_focus_uv (distance 0.0 <= 0.22, inside ball!).
    forged_inside_et = dataclasses.replace(
        ref_tr.eye_trace,
        adjusted_entry_focus_uv=exit_pt,
        effective_saccade_uv=0.0,
        vertical_eyeline_delta=0.0,
        continuity_score=1.0,
    )
    evs_inside = list(ed.events)
    evs_inside[ref_idx + 1] = dataclasses.replace(
        evs_inside[ref_idx + 1],
        entry_focus_uv=exit_pt,
    )
    trs_inside = list(ed.transitions)
    trs_inside[ref_idx] = dataclasses.replace(ref_tr, eye_trace=forged_inside_et)
    mean_eye_inside = round(
        sum(t.eye_trace.continuity_score for t in trs_inside) / len(trs_inside), 6
    )
    ed_inside = dataclasses.replace(
        ed,
        events=tuple(evs_inside),
        transitions=tuple(trs_inside),
        mean_eye_trace_score=mean_eye_inside,
    )
    rep_inside = verify_editorial_timeline(ed_inside)
    assert rep_inside.compliant is False
    assert rep_inside.eye_trace_valid is False
    tampered_inside = dataclasses.replace(tight_manifest, editorial=ed_inside)
    assert verify_manifest_integrity(_reseal_tampered_manifest(tampered_inside)) is False
    assert verify_manifest_integrity(_reseal_with_recomputed_reports(tampered_inside)) is False

    # 2. Independent eyeline_matched re-computation:
    #    (a) Flip eyeline_matched alone; (b) also spoof event.look_direction to try to match it!
    base = compile_storyboard(DIALOGUE_AND_REACTION_SCRIPT)
    base_ed = base.editorial
    tr0 = base_ed.transitions[0]
    flipped_et = dataclasses.replace(
        tr0.eye_trace,
        eyeline_matched=not tr0.eye_trace.eyeline_matched,
    )
    ed_flip = dataclasses.replace(
        base_ed,
        transitions=(dataclasses.replace(tr0, eye_trace=flipped_et), *base_ed.transitions[1:]),
    )
    rep_flip = verify_editorial_timeline(ed_flip)
    assert rep_flip.compliant is False
    assert rep_flip.eye_trace_valid is False
    assert (
        verify_manifest_integrity(
            _reseal_with_recomputed_reports(dataclasses.replace(base, editorial=ed_flip))
        )
        is False
    )

    # Even if attacker ALSO mutates EditorialShotEvent.look_direction to match flipped_et,
    # cross-verification against CameraShotSpec.framing.look_direction rejects it!
    spoof_evs = list(base_ed.events)
    spoof_evs[1] = dataclasses.replace(spoof_evs[1], look_direction=spoof_evs[0].look_direction)
    ed_spoof_look = dataclasses.replace(ed_flip, events=tuple(spoof_evs))
    all_base_shots = [sh for p in base.cinematography for sh in p.shots]
    assert verify_editorial_timeline(ed_spoof_look, shots=all_base_shots).compliant is False
    assert (
        verify_manifest_integrity(
            _reseal_with_recomputed_reports(dataclasses.replace(base, editorial=ed_spoof_look))
        )
        is False
    )

    # 3. Decoupling editorial focus UVs from CameraShotSpec.framing (projected_start_uv / end_uv):
    #    Keep editorial timeline 100% internally valid, but decouple raw_entry_focus_uv (and
    #    events[0].entry_focus_uv) from shot.framing.projected_start_uv!
    fake_raw_in = (0.45, 0.34)
    recomputed_tr0_et = solve_eye_trace_transition(
        transition_index=0,
        from_shot_index=0,
        to_shot_index=1,
        exit_focus_uv=base_ed.events[0].exit_focus_uv,
        entry_focus_uv=fake_raw_in,
        out_look=all_base_shots[0].framing.look_direction,
        in_look=all_base_shots[1].framing.look_direction,
        axis_jumped=all_base_shots[1].intentional_axis_jump,
        max_saccade_uv=base_ed.max_eye_trace_saccade_limit,
    )
    evs_decoupled = list(base_ed.events)
    evs_decoupled[1] = dataclasses.replace(
        evs_decoupled[1], entry_focus_uv=recomputed_tr0_et.adjusted_entry_focus_uv
    )
    trs_decoupled = (
        dataclasses.replace(tr0, eye_trace=recomputed_tr0_et),
        *base_ed.transitions[1:],
    )
    mean_decoupled = round(
        sum(t.eye_trace.continuity_score for t in trs_decoupled) / len(trs_decoupled), 6
    )
    ed_decoupled = dataclasses.replace(
        base_ed,
        events=tuple(evs_decoupled),
        transitions=trs_decoupled,
        mean_eye_trace_score=mean_decoupled,
    )
    ed_decoupled = dataclasses.replace(
        ed_decoupled,
        verification=verify_editorial_timeline(ed_decoupled, shots=all_base_shots),
    )
    assert ed_decoupled.verification.compliant is True
    assert (
        verify_manifest_integrity(
            _reseal_with_recomputed_reports(dataclasses.replace(base, editorial=ed_decoupled))
        )
        is False
    )

    # Also test decoupling events[0].entry_focus_uv from shots[0].framing.projected_start_uv.
    evs_first_decoupled = list(base_ed.events)
    evs_first_decoupled[0] = dataclasses.replace(evs_first_decoupled[0], entry_focus_uv=(0.5, 0.5))
    ed_first_decoupled = dataclasses.replace(base_ed, events=tuple(evs_first_decoupled))
    assert (
        verify_manifest_integrity(
            _reseal_with_recomputed_reports(dataclasses.replace(base, editorial=ed_first_decoupled))
        )
        is False
    )


def test_wave4_f9_and_f7_scalar_derivatives_angular_jerk_totals_and_move_spoof() -> None:
    manifest = compile_storyboard(DIALOGUE_AND_REACTION_SCRIPT)
    p0, p1 = manifest.cinematography[0], manifest.cinematography[1]
    # Pick a shot and tamper ONLY scalar derivatives / jerk / aggregate trajectory metrics
    # WITHOUT touching any SE(3) pose!
    sh = p0.shots[0]
    samples = list(sh.trajectory.samples)
    mid = len(samples) // 2
    s_mid = samples[mid]

    sample_mutations = [
        dataclasses.replace(s_mid, ds_dtau=s_mid.ds_dtau + 0.15),
        dataclasses.replace(s_mid, d2s_dtau2=s_mid.d2s_dtau2 + 0.25),
        dataclasses.replace(s_mid, d3s_dtau3=s_mid.d3s_dtau3 + 1.50),
        dataclasses.replace(
            s_mid,
            linear_jerk=(
                s_mid.linear_jerk[0] + 0.20,
                s_mid.linear_jerk[1],
                s_mid.linear_jerk[2],
            ),
        ),
        dataclasses.replace(
            s_mid,
            angular_jerk=(
                s_mid.angular_jerk[0],
                s_mid.angular_jerk[1],
                s_mid.angular_jerk[2] + 0.20,
            ),
        ),
    ]
    for bad_smp in sample_mutations:
        mut_samples = list(samples)
        mut_samples[mid] = bad_smp
        bad_sh = dataclasses.replace(
            sh,
            trajectory=dataclasses.replace(sh.trajectory, samples=tuple(mut_samples)),
        )
        bad_p0 = dataclasses.replace(p0, shots=(bad_sh, *p0.shots[1:]))
        tampered = dataclasses.replace(manifest, cinematography=(bad_p0, p1))
        assert verify_manifest_integrity(_reseal_tampered_manifest(tampered)) is False
        assert verify_manifest_integrity(_reseal_with_recomputed_reports(tampered)) is False

    # Tamper total_translation_m and total_rotation_deg without changing poses or samples.
    for bad_traj in (
        dataclasses.replace(
            sh.trajectory, total_translation_m=sh.trajectory.total_translation_m + 0.05
        ),
        dataclasses.replace(
            sh.trajectory, total_rotation_deg=sh.trajectory.total_rotation_deg + 0.50
        ),
    ):
        bad_sh = dataclasses.replace(sh, trajectory=bad_traj)
        bad_p0 = dataclasses.replace(p0, shots=(bad_sh, *p0.shots[1:]))
        tampered = dataclasses.replace(manifest, cinematography=(bad_p0, p1))
        assert verify_manifest_integrity(_reseal_tampered_manifest(tampered)) is False
        assert verify_manifest_integrity(_reseal_with_recomputed_reports(tampered)) is False

    # F7 move_type spoofing at manifest level even with recomputed reports & 4 SHA-256s.
    alt_move = (
        sb.CameraMoveType.STATIC
        if sh.trajectory.move_type != sb.CameraMoveType.STATIC
        else sb.CameraMoveType.DOLLY
    )
    spoof_move_sh = dataclasses.replace(
        sh,
        trajectory=dataclasses.replace(sh.trajectory, move_type=alt_move),
    )
    spoof_p0 = dataclasses.replace(p0, shots=(spoof_move_sh, *p0.shots[1:]))
    tampered_move = dataclasses.replace(manifest, cinematography=(spoof_p0, p1))
    assert verify_manifest_integrity(_reseal_with_recomputed_reports(tampered_move)) is False


def test_end_to_end_compilation_of_english_persian_and_bilingual_scripts() -> None:
    english_10_shot = """\
INT. INTERROGATION ROOM - NIGHT - RAIN

DETECTIVE COLE
(sternly)
State your full name for the recording before we begin tonight.

MAYA
(whispering)
Maya Rahimi, architect, and I already gave my statement downstairs.

DETECTIVE COLE
(sarcastically)
Your statement conveniently skipped the twenty minutes before midnight.

MAYA
(hesitantly)
The rain knocked out the streetlights... I lost track of time.

DETECTIVE COLE
(angrily)
Security footage puts your coat at the harbor gate at eleven fifty!

MAYA
(fearfully)
Someone stole that coat from my studio earlier this evening!

DETECTIVE COLE
(urgently)
Give me one witness who saw you leave the building before ten!

MAYA
(sadly)
The night watchman saw me lock the front door and walk home.

DETECTIVE COLE
(firmly)
We will verify his logbook the moment dawn breaks tomorrow.

MAYA
(happily)
Please check it right now, because every word I spoke is true.
"""
    bilingual_mix = """\
داخلی. استودیو تدوین / EDIT SUITE - GOLDEN_HOUR - DUST

SARA
(whispering)
این سکانس باید با یک برش نرم شروع شود before the music enters.

سرگرد رضا
(قاطع)
Agreed, keep the camera steady و خط فرضی را حفظ کن!

EXT. ROOFTOP GARDEN - DAWN - SNOW

SARA (با لبخند): We nailed every single frame روی زمان دقیق!

سرگرد رضا (happily): Print the final reel همین امشب!
"""
    for raw in (english_10_shot, DIALOGUE_AND_REACTION_SCRIPT, bilingual_mix):
        manifest = compile_storyboard(raw)
        assert manifest.axis_180_compliant is True
        assert manifest.editorial_compliant is True
        assert manifest.eye_trace_compliant is True
        assert verify_manifest_integrity(manifest) is True
        assert manifest.editorial.j_cut_count >= 1
        assert manifest.editorial.l_cut_count >= 1

        # Cross-verify 180-degree rule, SO(3), and minimum-jerk zero endpoints on every shot.
        for plan in manifest.cinematography:
            assert plan.axis_report.compliant is True
            for shot in plan.shots:
                r = shot.pose.rotation_matrix
                assert math.isclose(float(r[0] @ r[1]), 0.0, abs_tol=1e-12)
                assert math.isclose(
                    float(
                        r[0, 0] * (r[1, 1] * r[2, 2] - r[1, 2] * r[2, 1])
                        - r[0, 1] * (r[1, 0] * r[2, 2] - r[1, 2] * r[2, 0])
                        + r[0, 2] * (r[1, 0] * r[2, 1] - r[1, 1] * r[2, 0])
                    ),
                    1.0,
                    abs_tol=1e-12,
                )
                assert shot.trajectory.samples[0].linear_velocity == (0.0, 0.0, 0.0)
                assert shot.trajectory.samples[-1].linear_velocity == (0.0, 0.0, 0.0)
                assert shot.trajectory.samples[0].linear_acceleration == (0.0, 0.0, 0.0)
                assert shot.trajectory.samples[-1].linear_acceleration == (0.0, 0.0, 0.0)


# --------------------------------------------------------------------------- #
# Wave-5: F8 saccade-policy upper bound/pin and F7/R1 character-blocking binds
# --------------------------------------------------------------------------- #


def _forge_editorial_policy(
    plan: sb.EditorialTimelinePlan,
    limit: float,
) -> sb.EditorialTimelinePlan:
    """Rebuild a whole timeline under a forged saccade limit, bypassing solver bound checks.

    This is the exact maneuver ChatGPT's audit used: declare a huge (or tiny) policy limit, let
    the raw entry focus stand where the projector would have reframed it, keep every derived
    eye-trace field self-consistent with the forged limit, and re-seal the digests afterwards.
    """
    from nexus_ai_agent.creative.storyboard_compiler.editorial_cut_solver import (
        _are_eyelines_complementary,
        _radial_eye_trace_projection,
    )

    events = list(plan.events)
    transitions: list[sb.CutTransition] = []
    for k, tr in enumerate(plan.transitions):
        et = tr.eye_trace
        out_ev, in_ev = events[k], events[k + 1]
        reframe, projected = _radial_eye_trace_projection(
            et.exit_focus_uv, et.raw_entry_focus_uv, limit
        )
        adj_u, adj_v = projected if reframe else et.raw_entry_focus_uv
        adjusted = (round(adj_u, 6), round(adj_v, 6))
        effective = math.hypot(adjusted[0] - et.exit_focus_uv[0], adjusted[1] - et.exit_focus_uv[1])
        raw = math.hypot(
            et.raw_entry_focus_uv[0] - et.exit_focus_uv[0],
            et.raw_entry_focus_uv[1] - et.exit_focus_uv[1],
        )
        vert_delta = abs(adjusted[1] - et.exit_focus_uv[1])
        matched = _are_eyelines_complementary(
            out_ev.look_direction, in_ev.look_direction, vert_delta, in_ev.intentional_axis_jump
        )
        transitions.append(
            dataclasses.replace(
                tr,
                eye_trace=dataclasses.replace(
                    et,
                    adjusted_entry_focus_uv=adjusted,
                    raw_saccade_uv=round(raw, 6),
                    effective_saccade_uv=round(effective, 6),
                    vertical_eyeline_delta=round(vert_delta, 6),
                    reframe_applied=reframe,
                    eyeline_matched=matched,
                    continuity_score=round(max(0.0, min(1.0, 1.0 - effective / math.sqrt(2.0))), 6),
                ),
            )
        )
        events[k + 1] = dataclasses.replace(in_ev, entry_focus_uv=adjusted)

    mean_eye = (
        round(sum(tr.eye_trace.continuity_score for tr in transitions) / len(transitions), 6)
        if transitions
        else 1.0
    )
    return dataclasses.replace(
        plan,
        events=tuple(events),
        transitions=tuple(transitions),
        max_eye_trace_saccade_limit=round(limit, 6),
        mean_eye_trace_score=mean_eye,
    )


@pytest.mark.parametrize("legal_limit", [0.05, 0.22, DEFAULT_MAX_EYE_TRACE_SACCADE, 0.50])
def test_wave5_policy_forger_is_faithful_for_in_policy_limits(legal_limit: float) -> None:
    """Control: the forging helper only trips the new bound, never the geometry itself."""
    manifest = compile_storyboard(DIALOGUE_AND_REACTION_SCRIPT)
    forged = _forge_editorial_policy(manifest.editorial, legal_limit)
    report = verify_editorial_timeline(forged, expected_max_saccade_limit=legal_limit)
    assert report.compliant is True
    assert report.eye_trace_valid is True
    assert forged.max_eye_trace_saccade_limit == round(legal_limit, 6)


@pytest.mark.parametrize("forged_limit", [0.51, 0.85, 2.0, math.sqrt(2.0), 0.049, 0.0, -1.0])
def test_f8_wave5_out_of_policy_saccade_limit_fails_editorial_and_manifest(
    forged_limit: float,
) -> None:
    """F8 closure: inflating (or shrinking) the saccade policy can never launder an eye jump."""
    manifest = compile_storyboard(DIALOGUE_AND_REACTION_SCRIPT)
    if forged_limit > 0.0:
        forged = _forge_editorial_policy(manifest.editorial, forged_limit)
    else:
        # Degenerate non-positive thresholds never reach the projector (policy fails first),
        # which is itself the invariant that keeps verification exception-free.
        forged = dataclasses.replace(manifest.editorial, max_eye_trace_saccade_limit=forged_limit)

    report = verify_editorial_timeline(forged)
    assert report.compliant is False
    assert report.eye_trace_valid is False
    assert any("eye-trace saccade policy violation" in v for v in report.violations)

    tampered = dataclasses.replace(
        manifest,
        editorial=forged,
        editorial_compliant=True,
        eye_trace_compliant=True,
    )
    # Adversary recomputes all 4 SHA-256 digests while keeping the compliance flags True...
    assert verify_manifest_integrity(_reseal_tampered_manifest(tampered)) is False
    # ...and the strongest variant that also recomputes every cached verification report.
    assert verify_manifest_integrity(_reseal_with_recomputed_reports(tampered)) is False


def test_f8_wave5_solver_refuses_out_of_policy_saccade_limits() -> None:
    """Both solver entry points pin the limit into [0.05, 0.50]."""
    plans = solve_script_cinematography(parse_bilingual_script(DIALOGUE_AND_REACTION_SCRIPT))
    for bad in (0.0, 0.049, 0.51, 0.85, 2.0, math.sqrt(2.0), float("nan"), float("inf")):
        with pytest.raises(EditorialCutError, match="max_saccade_uv"):
            solve_eye_trace_transition(
                transition_index=0,
                from_shot_index=0,
                to_shot_index=1,
                exit_focus_uv=(0.15, 0.30),
                entry_focus_uv=(0.85, 0.30),
                max_saccade_uv=bad,
            )
        with pytest.raises(EditorialCutError, match="max_eye_trace_saccade"):
            solve_editorial_timeline(plans, max_eye_trace_saccade=bad)

    for good in (0.05, 0.38, 0.50):
        ok = solve_eye_trace_transition(
            transition_index=0,
            from_shot_index=0,
            to_shot_index=1,
            exit_focus_uv=(0.15, 0.30),
            entry_focus_uv=(0.85, 0.30),
            max_saccade_uv=good,
        )
        assert ok.effective_saccade_uv <= good + 1e-6
        assert ok.reframe_applied is True


def test_f8_wave5_explicit_policy_pin_enforces_exact_declared_limit() -> None:
    """``expected_max_saccade_limit`` pins the canonical policy exactly."""
    manifest = compile_storyboard(DIALOGUE_AND_REACTION_SCRIPT)
    pinned = verify_editorial_timeline(
        manifest.editorial, expected_max_saccade_limit=DEFAULT_MAX_EYE_TRACE_SACCADE
    )
    assert pinned.compliant is True
    assert pinned.eye_trace_valid is True

    mismatched = verify_editorial_timeline(
        manifest.editorial, expected_max_saccade_limit=DEFAULT_MAX_EYE_TRACE_SACCADE + 0.1
    )
    assert mismatched.compliant is False
    assert mismatched.eye_trace_valid is False
    assert any("pinned policy" in v for v in mismatched.violations)

    assert (
        verify_manifest_integrity(
            manifest, expected_max_saccade_limit=DEFAULT_MAX_EYE_TRACE_SACCADE
        )
        is True
    )
    assert (
        verify_manifest_integrity(
            manifest, expected_max_saccade_limit=DEFAULT_MAX_EYE_TRACE_SACCADE + 0.1
        )
        is False
    )

    parsed = parse_bilingual_script(DIALOGUE_AND_REACTION_SCRIPT)
    plans = solve_script_cinematography(parsed)
    editorial = solve_editorial_timeline(plans, max_eye_trace_saccade=0.30)
    sealed = build_compiled_manifest(parsed, plans, editorial, expected_max_saccade_limit=0.30)
    assert sealed.eye_trace_compliant is True
    assert verify_manifest_integrity(sealed, expected_max_saccade_limit=0.30) is True
    relaxed = build_compiled_manifest(parsed, plans, editorial, expected_max_saccade_limit=0.12)
    assert relaxed.eye_trace_compliant is False
    assert verify_manifest_integrity(relaxed) is False


def test_f7_wave5_manifest_rejects_forged_character_blocking() -> None:
    """F7/R1 closure: blocking, subject labels, and subject positions are bound to the script."""
    manifest = compile_storyboard(DIALOGUE_AND_REACTION_SCRIPT)
    p0, p1 = manifest.cinematography[0], manifest.cinematography[1]
    scene0 = manifest.script.scenes[0]
    shift = (0.25, 0.15, 0.05)

    def moved(pos: tuple[float, float, float]) -> tuple[float, float, float]:
        return (pos[0] + shift[0], pos[1] + shift[1], pos[2] + shift[2])

    # Case 1: re-stage BOTH the ledger and every shot subject so internal consistency holds.
    forged_pairs = tuple((name, moved(pos)) for name, pos in p0.character_positions)
    forged_shots = tuple(
        dataclasses.replace(sh, subject_position=moved(sh.subject_position)) for sh in p0.shots
    )
    bad_p0 = dataclasses.replace(p0, character_positions=forged_pairs, shots=forged_shots)
    tampered = dataclasses.replace(manifest, cinematography=(bad_p0, p1))
    assert (
        sb.verify_scene_character_blocking(
            bad_p0.shots,
            bad_p0.axis,
            scene=scene0,
            character_positions=forged_pairs,
        )
        is False
    )
    assert (
        sb.verify_180_degree_rule(
            bad_p0.shots, bad_p0.axis, scene=scene0, character_positions=forged_pairs
        ).compliant
        is False
    )
    # cinematography_sha256 now covers character_positions, so the raw digest already differs...
    resealed = _reseal_tampered_manifest(tampered)
    assert resealed.cinematography_sha256 != manifest.cinematography_sha256
    assert verify_manifest_integrity(tampered) is False
    # ...and full re-sealing plus report recomputation still cannot pass.
    assert verify_manifest_integrity(resealed) is False
    assert verify_manifest_integrity(_reseal_with_recomputed_reports(tampered)) is False

    # Case 2: mutate only a shot's subject_position (fake eyeline anchor).
    drift_shot = dataclasses.replace(
        p0.shots[0], subject_position=moved(p0.shots[0].subject_position)
    )
    drift_p0 = dataclasses.replace(p0, shots=(drift_shot, *p0.shots[1:]))
    drift = dataclasses.replace(manifest, cinematography=(drift_p0, p1))
    assert verify_manifest_integrity(_reseal_tampered_manifest(drift)) is False
    assert verify_manifest_integrity(_reseal_with_recomputed_reports(drift)) is False

    # Case 3: swap the two speakers' subject_label fields (shot count and poses untouched).
    swapped_shots = (
        dataclasses.replace(p0.shots[0], subject_label=p0.shots[1].subject_label),
        dataclasses.replace(p0.shots[1], subject_label=p0.shots[0].subject_label),
        *p0.shots[2:],
    )
    swapped_p0 = dataclasses.replace(p0, shots=swapped_shots)
    swapped = dataclasses.replace(manifest, cinematography=(swapped_p0, p1))
    assert (
        sb.verify_scene_character_blocking(
            swapped_p0.shots,
            swapped_p0.axis,
            scene=scene0,
            character_positions=swapped_p0.character_positions,
        )
        is False
    )
    assert verify_manifest_integrity(_reseal_tampered_manifest(swapped)) is False
    assert verify_manifest_integrity(_reseal_with_recomputed_reports(swapped)) is False

    # Case 4: tamper the blocking ledger alone (no re-seal at all).
    ledger_only = dataclasses.replace(
        p0, character_positions=tuple((n, moved(p)) for n, p in p0.character_positions)
    )
    assert (
        verify_manifest_integrity(dataclasses.replace(manifest, cinematography=(ledger_only, p1)))
        is False
    )

    # Case 5: rename a dialogue event's speaker to another cast member.
    ed = manifest.editorial
    first_dialogue = next(i for i, ev in enumerate(ed.events) if ev.speaker is not None)
    original_speaker = ed.events[first_dialogue].speaker
    other = next(name for name in dict(p0.character_positions) if name != original_speaker)
    bad_events = (
        *ed.events[:first_dialogue],
        dataclasses.replace(ed.events[first_dialogue], speaker=other),
        *ed.events[first_dialogue + 1 :],
    )
    speaker_tampered = dataclasses.replace(
        manifest, editorial=dataclasses.replace(ed, events=bad_events)
    )
    assert verify_manifest_integrity(_reseal_tampered_manifest(speaker_tampered)) is False
    assert verify_manifest_integrity(_reseal_with_recomputed_reports(speaker_tampered)) is False

    # Case 6: dropping a scene plan must return False instead of raising while zipping scenes.
    short = dataclasses.replace(manifest, cinematography=(p0,))
    assert verify_manifest_integrity(_reseal_tampered_manifest(short)) is False

    # Case 7: slide the whole 180-degree axis outward (a legal AxisOfAction) while keeping the
    # declared blocking and shots -- bind_axis canonicality rejects it even though the geometry
    # replay of verify_180_degree_rule(shots, axis) alone would happily accept the new line.
    slid_axis = dataclasses.replace(p0.axis, p1=(-1.5, 0.0, 1.6), p2=(1.5, 0.0, 1.6))
    axis_p0 = dataclasses.replace(p0, axis=slid_axis)
    axis_tampered = dataclasses.replace(manifest, cinematography=(axis_p0, p1))
    assert (
        sb.verify_scene_character_blocking(
            axis_p0.shots,
            slid_axis,
            scene=scene0,
            character_positions=axis_p0.character_positions,
            bind_axis=True,
        )
        is False
    )
    assert verify_manifest_integrity(_reseal_tampered_manifest(axis_tampered)) is False
    assert verify_manifest_integrity(_reseal_with_recomputed_reports(axis_tampered)) is False


def test_f7_wave5_canonical_character_positions_are_hash_bound() -> None:
    """_serialize_cinematography must include canonical blocking for every scene plan."""
    manifest = compile_storyboard(DIALOGUE_AND_REACTION_SCRIPT)
    from nexus_ai_agent.creative.storyboard_compiler.editorial_cut_solver import (
        _compute_digests,
        _serialize_cinematography,
    )

    payload = _serialize_cinematography(manifest.cinematography)
    first = payload["scene_plans"][0]
    assert [name for name, _ in first["character_positions"]] == sorted(
        name for name, _ in first["character_positions"]
    )
    canonical = sb.compute_canonical_character_positions(
        manifest.script.scenes[0],
        axis=manifest.cinematography[0].axis,
        initial_axis_side=manifest.cinematography[0].axis.initial_side,
    )
    sealed_positions = {name: tuple(pos) for name, pos in first["character_positions"]}
    assert set(sealed_positions) == set(canonical)
    for name, pos in sealed_positions.items():
        for axis_idx in range(3):
            assert abs(pos[axis_idx] - canonical[name][axis_idx]) <= 1e-6

    def digest_of(plans: tuple[sb.SceneCinematographyPlan, ...]) -> str:
        return _compute_digests(
            MANIFEST_SCHEMA_VERSION,
            manifest.script,
            plans,
            manifest.editorial,
            total_scenes=manifest.total_scenes,
            total_shots=sum(len(p.shots) for p in plans),
            total_duration_sec=manifest.total_duration_sec,
            axis_180_compliant=True,
            editorial_compliant=True,
            eye_trace_compliant=True,
        )[1]

    p0, p1 = manifest.cinematography[0], manifest.cinematography[1]
    nudged = dataclasses.replace(
        p0,
        character_positions=(
            (
                p0.character_positions[0][0],
                (p0.character_positions[0][1][0] + 1e-4, *p0.character_positions[0][1][1:]),
            ),
            *p0.character_positions[1:],
        ),
    )
    assert digest_of((nudged, p1)) != digest_of((p0, p1))


def test_wave5_saccade_policy_window_is_canonical_and_exported() -> None:
    """The policy window itself is part of the public contract and pins the default."""
    assert sb.MIN_EYE_TRACE_SACCADE_LOWER_BOUND == 0.05
    assert sb.MAX_EYE_TRACE_SACCADE_UPPER_BOUND == 0.50
    assert sb.MIN_EYE_TRACE_SACCADE_LOWER_BOUND <= DEFAULT_MAX_EYE_TRACE_SACCADE
    assert DEFAULT_MAX_EYE_TRACE_SACCADE <= sb.MAX_EYE_TRACE_SACCADE_UPPER_BOUND
    # 0.50 comfortably covers the widest legal Rule-of-Thirds reverse (sqrt(2)/3 = 0.4714)
    # while staying far below the unit-square diagonal that would neutralize reframing.
    assert sb.MAX_EYE_TRACE_SACCADE_UPPER_BOUND < math.sqrt(2.0) / 2.0
    # F5 geometry, stated exactly: the maximum reachable distance in the UV unit square is the
    # corner-to-corner diagonal, and 0.50 is a conservative cinematic ceiling of ~35% of it --
    # not a geometric threshold (only limit >= sqrt(2) would make reframing unreachable).
    diagonal = math.hypot(1.0 - 0.0, 1.0 - 0.0)
    assert diagonal == pytest.approx(math.sqrt(2.0))
    assert sb.MAX_EYE_TRACE_SACCADE_UPPER_BOUND < diagonal
    assert sb.MAX_EYE_TRACE_SACCADE_UPPER_BOUND / diagonal == pytest.approx(0.353553, abs=1e-6)
    # Widest legal framing moves stay admissible under the ceiling.
    assert abs(2.0 / 3.0 - 1.0 / 3.0) < sb.MAX_EYE_TRACE_SACCADE_UPPER_BOUND
    assert math.sqrt(2.0) / 3.0 < sb.MAX_EYE_TRACE_SACCADE_UPPER_BOUND
    assert StoryboardCompilerConfig().max_eye_trace_saccade == DEFAULT_MAX_EYE_TRACE_SACCADE
    manifest = compile_storyboard(DIALOGUE_AND_REACTION_SCRIPT)
    assert manifest.editorial.max_eye_trace_saccade_limit == DEFAULT_MAX_EYE_TRACE_SACCADE


def test_f7_wave5_bijection_rejects_consistently_restaged_axis_slide() -> None:
    """A slid 180-degree axis with a fully re-derived stage is still refused (bind_axis)."""
    from nexus_ai_agent.creative.storyboard_compiler.editorial_cut_solver import (
        _scene_shot_bijection_valid,
    )

    manifest = compile_storyboard(DIALOGUE_AND_REACTION_SCRIPT)
    p0 = manifest.cinematography[0]
    scene0 = manifest.script.scenes[0]
    slid_axis = dataclasses.replace(p0.axis, p1=(-1.5, 0.0, 1.6), p2=(1.5, 0.0, 1.6))
    ledger = dict(p0.character_positions)
    restaged = {
        name: ((-1.5, 0.0, 1.6) if pos[0] < 0.0 else (1.5, 0.0, 1.6))
        for name, pos in ledger.items()
    }

    def rebind(label: str) -> tuple[float, float, float]:
        return restaged.get(label, (0.0, 0.0, 1.6))

    slid_shots = tuple(
        dataclasses.replace(sh, subject_position=rebind(sh.subject_label)) for sh in p0.shots
    )
    slid_plan = dataclasses.replace(
        p0,
        axis=slid_axis,
        character_positions=tuple((name, restaged[name]) for name in ledger),
        shots=slid_shots,
    )
    # Internally consistent under the slid axis -- only canonical-axis binding can see through it.
    assert (
        sb.verify_scene_character_blocking(
            slid_plan.shots,
            slid_axis,
            scene=scene0,
            character_positions=slid_plan.character_positions,
            bind_axis=False,
        )
        is True
    )
    assert (
        sb.verify_scene_character_blocking(
            slid_plan.shots,
            slid_axis,
            scene=scene0,
            character_positions=slid_plan.character_positions,
            bind_axis=True,
        )
        is False
    )
    tampered = dataclasses.replace(
        manifest, cinematography=(slid_plan, *manifest.cinematography[1:])
    )
    assert _scene_shot_bijection_valid(tampered) is False
    assert _scene_shot_bijection_valid(manifest) is True
    assert verify_manifest_integrity(_reseal_tampered_manifest(tampered)) is False
    assert verify_manifest_integrity(_reseal_with_recomputed_reports(tampered)) is False


# --------------------------------------------------------------------------- #
# Wave-6: script provenance replay, builder/verifier axis symmetry, policy fidelity
# --------------------------------------------------------------------------- #


def test_f1_wave6_manifest_rejects_fully_forged_script_ast_even_when_resealed() -> None:
    """F1 closure: the AST must be the deterministic parse of its own hashed source text.

    Wave-5 verified everything *derived from* ``manifest.script`` but never that the scene graph
    was produced by parsing a real screenplay, so an attacker could rewrite headings, tensions,
    and scene aggregates, recompute all four SHA-256 digests, and pass. Replay of the parser on the
    retained ``normalized_source`` now refutes any such forgery.
    """
    from nexus_ai_agent.creative.storyboard_compiler.bilingual_script_parser import (
        TimeOfDay,
        WeatherCondition,
    )
    from nexus_ai_agent.creative.storyboard_compiler.editorial_cut_solver import _serialize_script

    manifest = compile_storyboard(DIALOGUE_AND_REACTION_SCRIPT)
    assert sb.verify_script_provenance(manifest.script) is True
    assert "normalized_source" in _serialize_script(manifest.script)

    mutators: tuple[Callable[[sb.ParsedScene], sb.ParsedScene], ...] = (
        lambda sc: dataclasses.replace(
            sc, heading=dataclasses.replace(sc.heading, weather=WeatherCondition.SNOW)
        ),
        lambda sc: dataclasses.replace(
            sc, heading=dataclasses.replace(sc.heading, time_of_day=TimeOfDay.DAWN)
        ),
        lambda sc: dataclasses.replace(sc, peak_tension=0.03125, mean_tension=0.0625),
        lambda sc: dataclasses.replace(
            sc, total_duration_sec=round(sc.total_duration_sec + 1.5, 6)
        ),
    )
    for mutate in mutators:
        forged_scenes = tuple(mutate(sc) for sc in manifest.script.scenes)
        forged_script = dataclasses.replace(
            manifest.script,
            scenes=forged_scenes,
            total_duration_sec=round(sum(sc.total_duration_sec for sc in forged_scenes), 6),
        )
        # Same source digest, same cast -- only the derived scene graph was rewritten.
        assert forged_script.source_sha256 == manifest.script.source_sha256
        assert forged_script.characters == manifest.script.characters
        tampered = dataclasses.replace(
            manifest,
            script=forged_script,
            axis_180_compliant=True,
            editorial_compliant=True,
            eye_trace_compliant=True,
        )
        assert sb.verify_script_provenance(forged_script) is False
        # Weak re-seal (digests only) and the strongest one (reports + digests) both refuse it.
        assert verify_manifest_integrity(_reseal_tampered_manifest(tampered)) is False
        assert verify_manifest_integrity(_reseal_with_recomputed_reports(tampered)) is False

    # The sealed digest commits to the retained text itself: swapping only the text (with a
    # matching source digest) changes script_sha256, so it cannot be edited for free either.
    from nexus_ai_agent.creative.storyboard_compiler.editorial_cut_solver import _compute_digests

    def script_digest(script: sb.ParsedScript) -> str:
        return _compute_digests(
            MANIFEST_SCHEMA_VERSION,
            script,
            manifest.cinematography,
            manifest.editorial,
            total_scenes=manifest.total_scenes,
            total_shots=manifest.total_shots,
            total_duration_sec=manifest.total_duration_sec,
            axis_180_compliant=True,
            editorial_compliant=True,
            eye_trace_compliant=True,
        )[0]

    import hashlib

    alt_text = manifest.script.normalized_source + "\n\nSARA\nappended line\n"
    alt = dataclasses.replace(
        manifest.script,
        normalized_source=alt_text,
        source_sha256=hashlib.sha256(alt_text.encode("utf-8")).hexdigest(),
    )
    assert script_digest(alt) != script_digest(manifest.script)


def test_f2_wave6_builder_refuses_a_non_canonical_axis_that_still_solves_cleanly() -> None:
    """A custom axis is geometrically legal yet not scene-canonical: the sealed flag must say so.

    This is the only observable difference between ``bind_axis=False`` (what the solver caches) and
    ``bind_axis=True`` (what the auditor re-checks); without passing the flag in
    ``build_compiled_manifest`` such a plan was sealed ``axis_180_compliant=True`` and only refused
    afterwards by ``_scene_shot_bijection_valid``.
    """
    parsed = parse_bilingual_script(DIALOGUE_AND_REACTION_SCRIPT)
    scene0 = parsed.scenes[0]
    custom_axis = sb.AxisOfAction(
        p1=(-1.0, 0.0, 1.6),
        p2=(1.0, 0.0, 1.6),
        initial_side=1,
        character_a=scene0.characters[0],
        character_b=scene0.characters[1],
    )
    plan = sb.solve_scene_cinematography(parsed.scenes[0], axis=custom_axis)
    single_scene_script = dataclasses.replace(parsed, scenes=(parsed.scenes[0],))
    editorial = solve_editorial_timeline((plan,))
    sealed = build_compiled_manifest(single_scene_script, (plan,), editorial)

    # Blocking, poses, and the cached solver report are all internally perfect for this axis.
    assert plan.axis_report.compliant is True
    assert (
        sb.verify_180_degree_rule(
            plan.shots,
            custom_axis,
            scene=parsed.scenes[0],
            character_positions=plan.character_positions,
        ).compliant
        is True
    )
    assert (
        sb.verify_180_degree_rule(
            plan.shots,
            custom_axis,
            scene=parsed.scenes[0],
            character_positions=plan.character_positions,
            bind_axis=True,
        ).compliant
        is False
    )
    # Sealing and auditing now agree the manifest is not canonically blocked.
    assert sealed.axis_180_compliant is False
    assert sealed.editorial_compliant is True
    assert verify_manifest_integrity(sealed) is False
    assert (
        verify_manifest_integrity(
            sealed, expected_source_sha256=parsed.source_sha256, expected_initial_axis_side=1
        )
        is False
    )
    # And the fully canonical compilation of the same scene still seals clean.
    canonical_plans = solve_script_cinematography(parsed)
    canonical = build_compiled_manifest(
        parsed, canonical_plans, solve_editorial_timeline(canonical_plans)
    )
    assert canonical.axis_180_compliant is True
    assert verify_manifest_integrity(canonical) is True


def test_f1_wave6_source_sha256_pin_is_fail_closed() -> None:
    """A deployment can pin the exact screenplay a manifest was compiled from."""
    manifest = compile_storyboard(DIALOGUE_AND_REACTION_SCRIPT)
    digest = manifest.script.source_sha256
    assert re.fullmatch(r"[0-9a-f]{64}", digest) is not None
    assert verify_manifest_integrity(manifest, expected_source_sha256=digest) is True
    assert verify_manifest_integrity(manifest, expected_source_sha256="0" * 64) is False
    # A syntactically perfect digest that simply is not this script's source: refused.
    assert verify_manifest_integrity(manifest, expected_source_sha256="f" * 64) is False
    # An AST whose provenance text was dropped cannot be pinned at all.
    stripped = dataclasses.replace(manifest.script, normalized_source="")
    assert (
        verify_manifest_integrity(
            _reseal_tampered_manifest(dataclasses.replace(manifest, script=stripped))
        )
        is False
    )


def test_f2_wave6_builder_and_verifier_agree_on_canonical_axis_and_side_pin() -> None:
    """``axis_180_compliant`` must mean exactly what ``verify_manifest_integrity`` re-checks."""
    parsed = parse_bilingual_script(DIALOGUE_AND_REACTION_SCRIPT)
    plans = solve_script_cinematography(parsed)
    editorial = solve_editorial_timeline(plans)

    legit = build_compiled_manifest(parsed, plans, editorial)
    assert legit.axis_180_compliant is True
    assert verify_manifest_integrity(legit) is True
    assert verify_manifest_integrity(legit, expected_initial_axis_side=1) is True

    # Slide scene 0's axis but re-derive every dependent quantity, so only canonicality is left.
    p0, rest = legit.cinematography[0], legit.cinematography[1:]
    slid_axis = dataclasses.replace(p0.axis, p1=(-1.5, 0.0, 1.6), p2=(1.5, 0.0, 1.6))
    ledger = dict(p0.character_positions)
    restaged = {
        name: ((-1.5, 0.0, 1.6) if pos[0] < 0.0 else (1.5, 0.0, 1.6))
        for name, pos in ledger.items()
    }
    slid_shots = tuple(
        dataclasses.replace(
            sh, subject_position=restaged.get(sh.subject_label, sh.subject_position)
        )
        for sh in p0.shots
    )
    slid_plan = dataclasses.replace(
        p0,
        axis=slid_axis,
        character_positions=tuple((name, restaged[name]) for name in ledger),
        shots=slid_shots,
    )
    slid_plans = (slid_plan, *rest)
    slid_editorial = solve_editorial_timeline(slid_plans)
    sealed = build_compiled_manifest(parsed, slid_plans, slid_editorial)
    # Builder and auditor now agree *at seal time*: the flag is already False, so the manifest can
    # never be stamped axis_180_compliant=True and refused later for the same reason.
    assert sealed.axis_180_compliant is False
    assert verify_manifest_integrity(sealed) is False
    assert (
        sb.verify_180_degree_rule(
            slid_plan.shots,
            slid_axis,
            scene=parsed.scenes[0],
            character_positions=slid_plan.character_positions,
            bind_axis=True,
        ).compliant
        is False
    )

    # Opening-side policy pin across the whole manifest.
    flipped_parsed = parse_bilingual_script(DIALOGUE_AND_REACTION_SCRIPT)
    flipped_plans = solve_script_cinematography(flipped_parsed, initial_axis_side=-1)
    flipped = build_compiled_manifest(
        flipped_parsed, flipped_plans, solve_editorial_timeline(flipped_plans)
    )
    assert verify_manifest_integrity(flipped, expected_initial_axis_side=-1) is True
    assert verify_manifest_integrity(flipped, expected_initial_axis_side=1) is False
    assert verify_manifest_integrity(flipped, expected_initial_axis_side=0) is False
    assert verify_manifest_integrity(flipped, expected_initial_axis_side=True) is False
    assert verify_manifest_integrity(legit, expected_initial_axis_side=cast(int, 2.0)) is False
    # Pinning does not rewrite the sealed flag: it only refuses to vouch for it.
    assert flipped.axis_180_compliant is True


def test_f3_f4_wave6_empty_timeline_and_mapping_ledger_fidelity() -> None:
    """An empty timeline reports collected findings, and ledgers bind content not ordering."""
    manifest = compile_storyboard(DIALOGUE_AND_REACTION_SCRIPT)
    ed = manifest.editorial

    # F4: previously this path hard-coded eye_trace_valid=True, so a timeline with zero events could
    # carry an out-of-policy saccade limit and still report a "valid" eye-trace sub-flag.
    empty_illegal = dataclasses.replace(
        ed, events=(), transitions=(), max_eye_trace_saccade_limit=2.0
    )
    report = verify_editorial_timeline(empty_illegal)
    assert report.compliant is False
    assert report.eye_trace_valid is False
    assert report.video_contiguous is False
    assert any("saccade policy violation" in v for v in report.violations)
    assert any("zero shot events" in v for v in report.violations)
    assert len(report.violations) == 2

    # With an in-policy limit the empty timeline still fails on cardinality only.
    empty_legal = dataclasses.replace(ed, events=(), transitions=())
    legal_report = verify_editorial_timeline(empty_legal)
    assert legal_report.compliant is False
    assert legal_report.eye_trace_valid is True
    assert legal_report.violations == ("timeline has zero shot events",)

    # A manifest built on an empty timeline is refused regardless of re-sealing.
    tampered = dataclasses.replace(manifest, editorial=empty_illegal)
    assert verify_manifest_integrity(_reseal_tampered_manifest(tampered)) is False
    assert verify_manifest_integrity(_reseal_with_recomputed_reports(tampered)) is False

    # F3: canonical blocking accepts the same cast in any Mapping order, but a plan's own tuple
    # ledger stays order-bound (see test_cinematography_solver for the helper-level proof).
    scene0 = manifest.script.scenes[0]
    p0 = manifest.cinematography[0]
    mapping_ledger = dict(reversed(list(p0.character_positions)))
    assert (
        sb.verify_scene_character_blocking(
            p0.shots,
            p0.axis,
            scene=scene0,
            character_positions=mapping_ledger,
            bind_axis=True,
        )
        is True
    )


def test_wave5_editorial_summary_bounds_reject_forged_policy_fields() -> None:
    """Editorial-level policy/breath-gap bounds are enforced even when hashes are re-sealed."""
    manifest = compile_storyboard(DIALOGUE_AND_REACTION_SCRIPT)
    for forged_limit in (0.85, 2.0):
        forged = dataclasses.replace(manifest.editorial, max_eye_trace_saccade_limit=forged_limit)
        tampered = dataclasses.replace(manifest, editorial=forged)
        rep = verify_editorial_timeline(forged)
        assert rep.compliant is False and rep.eye_trace_valid is False
        assert verify_manifest_integrity(_reseal_tampered_manifest(tampered)) is False
        assert verify_manifest_integrity(_reseal_with_recomputed_reports(tampered)) is False

    for bad_gap in (-0.05, 0.9):
        forged_ed = dataclasses.replace(manifest.editorial, breath_gap_sec=bad_gap)
        tampered = dataclasses.replace(manifest, editorial=forged_ed)
        assert verify_manifest_integrity(_reseal_tampered_manifest(tampered)) is False
        assert verify_manifest_integrity(_reseal_with_recomputed_reports(tampered)) is False


def test_package_exports_and_stdlib_plus_numpy_only_import_boundary() -> None:
    # Verify __all__ exports resolve cleanly.
    for symbol in sb.__all__:
        assert hasattr(sb, symbol), f"missing exported symbol {symbol}"
    assert len(sb.__all__) == len(set(sb.__all__)), "duplicate package export"
    assert sb.__all__ == sorted(sb.__all__), "package __all__ must stay alphabetically sorted"
    # Wave-6 additions must be reachable from the public namespace, not just the submodule.
    for wave6_symbol in (
        "verify_script_provenance",
        "canonical_axis_of_action",
        "compute_canonical_character_positions",
        "expected_shot_subject",
        "verify_scene_character_blocking",
        "MIN_EYE_TRACE_SACCADE_LOWER_BOUND",
        "MAX_EYE_TRACE_SACCADE_UPPER_BOUND",
    ):
        assert wave6_symbol in sb.__all__, f"{wave6_symbol} must be re-exported"

    pkg_dir = Path(sb.__file__).resolve().parent
    py_files = sorted(pkg_dir.glob("*.py"))
    assert {p.name for p in py_files} == {
        "__init__.py",
        "bilingual_script_parser.py",
        "cinematography_solver.py",
        "editorial_cut_solver.py",
    }

    allowed_external = {"numpy"}
    stdlib = sys.stdlib_module_names
    forbidden_io = {"http", "urllib", "socket", "subprocess", "requests", "httpx", "litellm"}

    for py_file in py_files:
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    assert root not in forbidden_io
                    assert root in stdlib or root in allowed_external, (
                        f"{py_file.name} imports disallowed module {alias.name!r}"
                    )
            elif isinstance(node, ast.ImportFrom):
                if node.level > 0:
                    continue  # intra-package relative import
                assert node.module is not None
                root = node.module.split(".")[0]
                assert root not in forbidden_io
                assert root in stdlib or root in allowed_external, (
                    f"{py_file.name} imports disallowed module {node.module!r}"
                )


def test_wave7_hmac_seal_is_deterministic_and_excluded_from_the_signed_payload() -> None:
    """The seal wraps the manifest digest; it must never perturb the payload it commits to."""
    import hashlib
    import hmac as _hmac

    key = b"deployment-signing-secret"
    unsigned = compile_storyboard(DIALOGUE_AND_REACTION_SCRIPT)
    sealed = compile_storyboard(DIALOGUE_AND_REACTION_SCRIPT, hmac_key=key)

    assert unsigned.manifest_hmac_sha256 == ""
    assert re.fullmatch(r"[0-9a-f]{64}", sealed.manifest_hmac_sha256)
    assert sealed.manifest_sha256 == unsigned.manifest_sha256
    assert sealed.script_sha256 == unsigned.script_sha256
    assert sealed.to_canonical_json() == unsigned.to_canonical_json()
    assert "manifest_hmac_sha256" not in json.loads(sealed.to_canonical_json())
    assert sealed.to_canonical_dict()["manifest_sha256"] == sealed.manifest_sha256
    assert (
        _hmac.new(key, sealed.manifest_sha256.encode("utf-8"), hashlib.sha256).hexdigest()
        == sealed.manifest_hmac_sha256
    )
    # A str key is the same secret as its UTF-8 bytes, and re-sealing is deterministic.
    text_key = compile_storyboard(
        DIALOGUE_AND_REACTION_SCRIPT, hmac_key="deployment-signing-secret"
    )
    assert text_key.manifest_hmac_sha256 == sealed.manifest_hmac_sha256
    parsed = parse_bilingual_script(DIALOGUE_AND_REACTION_SCRIPT)
    plans = solve_script_cinematography(parsed)
    editorial = solve_editorial_timeline(plans)
    again = build_compiled_manifest(parsed, plans, editorial, hmac_key=key)
    assert again.manifest_hmac_sha256 == sealed.manifest_hmac_sha256
    # Keyless builds stay unsigned, so the sealed payload digest is unchanged by the feature.
    assert build_compiled_manifest(parsed, plans, editorial).manifest_hmac_sha256 == ""
    assert verify_manifest_integrity(sealed) is True
    assert verify_manifest_integrity(sealed, hmac_key=key) is True


def test_wave7_hmac_key_validation_and_seal_verification_are_fail_closed() -> None:
    """An unusable key stops the builder; a missing, forged, or mismatched seal refuses."""
    key = b"deployment-signing-secret"
    parsed = parse_bilingual_script(DIALOGUE_AND_REACTION_SCRIPT)
    plans = solve_script_cinematography(parsed)
    editorial = solve_editorial_timeline(plans)
    sealed = build_compiled_manifest(parsed, plans, editorial, hmac_key=key)
    unsigned = build_compiled_manifest(parsed, plans, editorial)

    for bad_key in ("", b"", bytearray(), 0, 1, True, None.__class__, [key]):
        with pytest.raises(ValueError, match="hmac_key must"):
            build_compiled_manifest(parsed, plans, editorial, hmac_key=cast("bytes", bad_key))
    # The key is checked before any content validation, so a caller typo is never masked.
    with pytest.raises(ValueError, match="hmac_key must not be empty"):
        build_compiled_manifest(parsed, [], editorial, hmac_key="")

    # A verifier never raises on a bad key -- it simply refuses to vouch for the manifest.
    for bad_key in ("", b"", 123, True):
        assert verify_manifest_integrity(sealed, hmac_key=cast("bytes", bad_key)) is False
    assert verify_manifest_integrity(sealed, hmac_key=b"other-secret") is False
    assert verify_manifest_integrity(unsigned, hmac_key=key) is False
    assert verify_manifest_integrity(sealed, hmac_key=None) is True
    assert verify_manifest_integrity(unsigned, hmac_key=None) is True

    for malformed in ("", "0" * 63, "0" * 65, "z" * 64, sealed.manifest_sha256.upper()):
        assert (
            verify_manifest_integrity(
                dataclasses.replace(sealed, manifest_hmac_sha256=malformed), hmac_key=key
            )
            is False
        )
    flipped = ("0" if sealed.manifest_hmac_sha256[0] != "0" else "1") + sealed.manifest_hmac_sha256[
        1:
    ]
    tampered_seal = dataclasses.replace(sealed, manifest_hmac_sha256=flipped)
    # The seal is inert unless the deployment asks for it -- the payload still verifies...
    assert verify_manifest_integrity(tampered_seal) is True
    # ...and a single flipped hex character destroys it under a key-mismatch comparison.
    assert verify_manifest_integrity(tampered_seal, hmac_key=key) is False
    # Re-sealing an untouched manifest leaves the payload -- and therefore the seal -- identical.
    assert verify_manifest_integrity(_reseal_with_recomputed_reports(sealed), hmac_key=key) is True
    # Editing any committed field invalidates the seal even though the payload is self-consistent.
    edited = dataclasses.replace(sealed, total_duration_sec=sealed.total_duration_sec + 0.5)
    assert verify_manifest_integrity(_reseal_tampered_manifest(edited), hmac_key=key) is False


def test_wave7_hmac_anchor_defeats_a_fully_self_consistent_whole_script_substitution() -> None:
    """Only an external anchor can refuse a forged manifest that is internally perfect."""
    key = b"ops-key-2026"
    legit = compile_storyboard(DIALOGUE_AND_REACTION_SCRIPT, hmac_key=key)
    rival = compile_storyboard("INT. RING - NIGHT\nA\nGo!\n\nB\nNow!\n\nA\nHit!")
    assert rival.script_sha256 != legit.script_sha256
    # The substitution attacker: take a *different genuine* screenplay package and graft its
    # digests onto it. Every internal invariant -- all four SHA-256s, the parser replay, both
    # cached reports -- is satisfied, because the package really was compiled from a real script.
    assert verify_manifest_integrity(rival) is True
    assert verify_manifest_integrity(_reseal_with_recomputed_reports(rival)) is True
    # Its own seal is absent, so a deployment that demands the anchor refuses it outright...
    assert verify_manifest_integrity(rival, hmac_key=key) is False
    assert verify_manifest_integrity(rival, require_trusted_anchor=True) is False
    # ...and the legitimate package keeps passing while the key stays secret.
    assert verify_manifest_integrity(legit, hmac_key=key, require_trusted_anchor=True) is True
    # Copying the honest seal onto the rival package does not help: the seal commits to the digest.
    stolen = dataclasses.replace(rival, manifest_hmac_sha256=legit.manifest_hmac_sha256)
    assert verify_manifest_integrity(stolen, hmac_key=key) is False
    # Demanding an anchor without supplying one is a refusal, whatever the manifest claims.
    assert verify_manifest_integrity(stolen, require_trusted_anchor=True) is False
    assert verify_manifest_integrity(stolen, require_trusted_anchor=True, hmac_key=key) is False


def test_wave7_require_trusted_anchor_demands_an_external_commitment() -> None:
    """With the flag set, a manifest that nothing outside itself vouches for is refused."""
    key = b"ops-key-2026"
    unsigned = compile_storyboard(DIALOGUE_AND_REACTION_SCRIPT)
    sealed = compile_storyboard(DIALOGUE_AND_REACTION_SCRIPT, hmac_key=key)
    source_sha = unsigned.script.source_sha256

    assert verify_manifest_integrity(unsigned) is True
    assert verify_manifest_integrity(unsigned, require_trusted_anchor=True) is False
    assert (
        verify_manifest_integrity(
            unsigned, require_trusted_anchor=True, expected_source_sha256=source_sha
        )
        is True
    )
    assert (
        verify_manifest_integrity(
            unsigned, require_trusted_anchor=True, expected_source_sha256="0" * 64
        )
        is False
    )
    # A malformed pin is not an anchor: it cannot unlock the gate on its own.
    assert (
        verify_manifest_integrity(
            unsigned, require_trusted_anchor=True, expected_source_sha256=cast("str", 7)
        )
        is False
    )
    assert verify_manifest_integrity(sealed, require_trusted_anchor=True, hmac_key=key) is True
    # An unsigned manifest cannot satisfy the anchor requirement by pointing at the key alone.
    assert verify_manifest_integrity(unsigned, require_trusted_anchor=True, hmac_key=key) is False
    # The flag is inert when no anchor is demanded at all (default behaviour is unchanged).
    assert verify_manifest_integrity(unsigned, require_trusted_anchor=False) is True
    # A legacy manifest (no seal field in the serialized record) still verifies, unsigned.
    legacy_fields = {
        field.name: getattr(unsigned, field.name)
        for field in dataclasses.fields(unsigned)
        if field.name != "manifest_hmac_sha256"
    }
    legacy = sb.CompiledStoryboardManifest(**legacy_fields)
    assert legacy.manifest_hmac_sha256 == ""
    assert verify_manifest_integrity(legacy) is True
    assert verify_manifest_integrity(legacy, hmac_key=key) is False


def test_wave7_saccade_policy_values_are_validated_uniformly() -> None:
    """Policy pins raise ``ValueError``; forged declared limits only ever report violations."""
    manifest = compile_storyboard(DIALOGUE_AND_REACTION_SCRIPT)
    ed = manifest.editorial

    for bad_pin in ("0.35", None.__class__, True, float("nan"), float("inf"), [0.35]):
        with pytest.raises(
            ValueError, match="expected_max_saccade_limit must be None or a finite real number"
        ):
            verify_editorial_timeline(ed, expected_max_saccade_limit=cast("float", bad_pin))
    # 0 is a legal finite number (it merely fails the policy bounds), and None means "no pin".
    declared = ed.max_eye_trace_saccade_limit
    assert verify_editorial_timeline(ed, expected_max_saccade_limit=None).compliant is True
    assert verify_editorial_timeline(ed, expected_max_saccade_limit=declared).compliant is True
    assert verify_editorial_timeline(ed, expected_max_saccade_limit=0).compliant is False
    off_by_a_hair = verify_editorial_timeline(ed, expected_max_saccade_limit=declared + 1e-3)
    assert off_by_a_hair.compliant is False
    assert any("!= pinned policy" in v for v in off_by_a_hair.violations)

    for bad_limit in ("0.35", None, True, float("nan"), float("inf"), object()):
        forged = dataclasses.replace(ed, max_eye_trace_saccade_limit=cast("float", bad_limit))
        report = verify_editorial_timeline(forged)
        assert report.compliant is False
        assert report.eye_trace_valid is False
        assert any("saccade policy violation" in v for v in report.violations)
        tampered = dataclasses.replace(manifest, editorial=forged)
        assert verify_manifest_integrity(tampered) is False
    # The string and bool forgeries are the ones an attacker can actually canonicalize into a
    # re-sealed payload (both coerce to a float in the digest JSON), so they must additionally be
    # refused by the summary bounds with every SHA-256 recomputed.
    for resealed in ("0.35", True):
        forged = dataclasses.replace(ed, max_eye_trace_saccade_limit=cast("float", resealed))
        tampered = dataclasses.replace(manifest, editorial=forged)
        assert verify_manifest_integrity(_reseal_tampered_manifest(tampered)) is False
        assert verify_manifest_integrity(_reseal_with_recomputed_reports(tampered)) is False

    for bad_policy in ("0.35", None, True, float("nan"), float("inf")):
        with pytest.raises(EditorialCutError, match="max_eye_trace_saccade must lie within"):
            solve_editorial_timeline(
                manifest.cinematography, max_eye_trace_saccade=cast("float", bad_policy)
            )


def test_wave8_strict_verifier_makes_the_trust_anchor_mandatory() -> None:
    """The production gate cannot be talked out of the anchor by simply omitting it.

    ``verify_manifest_integrity`` is the format-level checker and stays keyless by
    default (the whole compiler test-suite legitimately compiles unsealed), so the
    trust decision lives in ``verify_manifest_integrity_strict``: no anchor supplied
    is a refusal, not an acceptance.
    """
    legit = compile_storyboard(DIALOGUE_AND_REACTION_SCRIPT)
    assert verify_manifest_integrity(legit) is True  # internally consistent, keyless
    # The same, untouched, fully consistent manifest is refused at the delivery gate.
    assert verify_manifest_integrity_strict(legit) is False
    # Either external anchor alone is enough to make the gate open.
    assert (
        verify_manifest_integrity_strict(legit, expected_source_sha256=legit.script.source_sha256)
        is True
    )
    key = b"ops-key-2026"
    sealed = compile_storyboard(DIALOGUE_AND_REACTION_SCRIPT, hmac_key=key)
    assert verify_manifest_integrity_strict(sealed, hmac_key=key) is True
    assert verify_manifest_integrity_strict(sealed, hmac_key=b"other-secret") is False
    # An unusable key is a refusal, never an exception and never a pass.
    for bad_key in ("", b"", 123, True):
        assert verify_manifest_integrity_strict(sealed, hmac_key=cast("bytes", bad_key)) is False
    # A key the manifest was never sealed with does not become an anchor by being present.
    assert verify_manifest_integrity_strict(legit, hmac_key=key) is False
    # Fail-closed even for a well-formed but wrong source pin.
    assert verify_manifest_integrity_strict(legit, expected_source_sha256="0" * 64) is False
    assert verify_manifest_integrity_strict(legit, expected_source_sha256="not-a-hash") is False


def test_wave8_strict_verifier_refuses_a_self_consistent_whole_script_substitution() -> None:
    """The attack the anchor exists for: a genuine, internally perfect rival package."""
    key = b"ops-key-2026"
    legit = compile_storyboard(DIALOGUE_AND_REACTION_SCRIPT, hmac_key=key)
    rival = compile_storyboard("INT. RING - NIGHT\nA\nGo!\n\nB\nNow!\n\nA\nHit!")
    # The rival is honest work: every internal invariant, including the parser replay, holds.
    assert verify_manifest_integrity(rival) is True
    assert verify_manifest_integrity(_reseal_with_recomputed_reports(rival)) is True
    # Neither anchor route admits it, and stealing the honest seal does not help.
    assert verify_manifest_integrity_strict(rival, hmac_key=key) is False
    assert (
        verify_manifest_integrity_strict(rival, expected_source_sha256=legit.script.source_sha256)
        is False
    )
    stolen = dataclasses.replace(rival, manifest_hmac_sha256=legit.manifest_hmac_sha256)
    assert verify_manifest_integrity_strict(stolen, hmac_key=key) is False
    # The legitimate package stays verifiable through the same gate.
    assert verify_manifest_integrity_strict(legit, hmac_key=key) is True
    assert (
        verify_manifest_integrity_strict(legit, expected_source_sha256=legit.script.source_sha256)
        is True
    )


def test_wave8_strict_verifier_exposes_no_way_to_weaken_the_anchor() -> None:
    """Fail-closed by construction: there is no ``require_trusted_anchor`` knob to flip.

    A boolean parameter would let a caller (or a future refactor) quietly restore the
    keyless default at the gate, which is exactly the failure being fixed. Removing
    the parameter makes the strict path unarguable, and this assertion keeps it that
    way. The exported name must also be the same object the solver module defines, so
    the two cannot drift.
    """
    import inspect

    from nexus_ai_agent.creative.storyboard_compiler import editorial_cut_solver

    signature = inspect.signature(sb.verify_manifest_integrity_strict)
    assert "require_trusted_anchor" not in signature.parameters
    assert (
        sb.verify_manifest_integrity_strict is editorial_cut_solver.verify_manifest_integrity_strict
    )
    assert "verify_manifest_integrity_strict" in sb.__all__
    with pytest.raises(TypeError):
        sb.verify_manifest_integrity_strict(
            compile_storyboard(DIALOGUE_AND_REACTION_SCRIPT), require_trusted_anchor=False
        )
