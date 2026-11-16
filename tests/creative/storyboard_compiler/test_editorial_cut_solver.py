"""Tests for J/L-Cut editorial timing, eye-trace continuity, SHA-256 manifests, and purity."""

from __future__ import annotations

import ast
import dataclasses
import json
import math
import sys
from pathlib import Path

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


def test_package_exports_and_stdlib_plus_numpy_only_import_boundary() -> None:
    # Verify __all__ exports resolve cleanly.
    for symbol in sb.__all__:
        assert hasattr(sb, symbol), f"missing exported symbol {symbol}"

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
