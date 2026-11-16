"""Mathematical invariant and refusal tests for the SE(3) cinematography solver."""

from __future__ import annotations

import dataclasses
import math
from collections.abc import Sequence

import numpy as np
import pytest

from nexus_ai_agent.creative.storyboard_compiler.bilingual_script_parser import (
    ActionBeat,
    DialogueLine,
    SceneSetting,
    ScriptLanguage,
    TimeOfDay,
    WeatherCondition,
    parse_bilingual_script,
)
from nexus_ai_agent.creative.storyboard_compiler.cinematography_solver import (
    CANONICAL_F_STOPS,
    CANONICAL_FOCAL_LENGTHS_MM,
    AxisEnforcementTrace,
    AxisOfAction,
    CameraMoveType,
    CameraPoseSE3,
    CinematographyError,
    RuleOfThirdsPoint,
    ScreenLookDirection,
    ShotCameraPlan,
    ShotKind,
    ShotScale,
    axis_side_sign,
    build_look_at_pose,
    build_rule_of_thirds_pose,
    canonical_axis_of_action,
    compute_canonical_character_positions,
    enforce_axis_of_action,
    enforce_axis_of_action_with_trace,
    evaluate_trajectory_at,
    expected_shot_subject,
    generate_minimum_jerk_trajectory,
    horizontal_axis_cross,
    is_valid_se3,
    minimum_jerk_profile,
    minimum_jerk_scalar,
    project_world_point,
    select_lens_and_aperture,
    select_rule_of_thirds,
    skew_symmetric,
    so3_exp,
    so3_log,
    solve_scene_cinematography,
    solve_script_cinematography,
    verify_180_degree_rule,
    verify_scene_character_blocking,
)

Vec3 = Sequence[float]

TEN_SHOT_PERSIAN_DIALOGUE = """\
داخلی. اتاق بازجویی - شب

سرگرد رضا
(قاطع)
ساعت یازده شب کجا بودی؟

سارا
(آرام)
در خانه بودم و کتاب می‌خواندم.

سرگرد رضا
(با طعنه)
هیچ شاهدی هم داری که تایید کند؟

سارا
(با تردید)
تلفنم خاموش بود... کسی زنگ نزد.

سرگرد رضا
(با خشم)
دوربین سر خیابان ماشین تو را ثبت کرده!

سارا
(با ترس)
امکان ندارد، ماشینم در پارکینگ بود!

سرگرد رضا
(با عجله)
کلید پارکینگ دست چه کسی بود؟ زود بگو!

سارا
(زمزمه)
فقط برادرم یک نسخهٔ اضافه داشت.

سرگرد رضا
(غمگین)
پس حدس من از اول درست بود.

سارا
(با خشم)
او بی‌گناه است! پای او را وسط نکش!!
"""


# --------------------------------------------------------------------------- #
# SO(3) and SE(3) Lie Group Proofs
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "omega",
    [
        (0.0, 0.0, 0.0),
        (1e-10, -2e-10, 1.5e-10),
        (0.35, -0.60, 0.25),
        (1.2, 0.8, -0.9),
        (math.pi - 1e-7, 0.0, 0.0),
        (0.0, -(math.pi - 2e-7), 0.0),
        (
            (math.pi - 1e-6) / math.sqrt(3.0),
            -(math.pi - 1e-6) / math.sqrt(3.0),
            (math.pi - 1e-6) / math.sqrt(3.0),
        ),
    ],
)
def test_so3_exp_and_log_roundtrip_and_orthogonality(omega: tuple[float, float, float]) -> None:
    rot = so3_exp(omega)
    assert np.allclose(rot @ rot.T, np.eye(3), atol=1e-12)
    assert np.allclose(rot.T @ rot, np.eye(3), atol=1e-12)
    assert math.isclose(float(np.linalg.det(rot)), 1.0, abs_tol=1e-12)

    recovered = so3_log(rot)
    re_exp = so3_exp(recovered)
    assert float(np.linalg.norm(re_exp - rot)) < 1e-13

    k = skew_symmetric(omega)
    assert np.allclose(k.T, -k, atol=1e-15)


@pytest.mark.parametrize(
    "theta",
    [
        math.pi - 1e-3,
        math.pi - 1e-4,
        math.pi - 1e-7,
        math.pi,
    ],
)
@pytest.mark.parametrize(
    "raw_axis",
    [
        (1.0, 0.0, 0.0),
        (-1.0, 0.0, 0.0),
        (0.0, 1.0, 0.0),
        (0.0, -1.0, 0.0),
        (0.0, 0.0, 1.0),
        (0.0, 0.0, -1.0),
        (1.0, -2.0, 0.5),
        (-0.6, -0.4, -1.1),
    ],
)
def test_so3_log_machine_precision_near_and_at_pi(
    theta: float,
    raw_axis: tuple[float, float, float],
) -> None:
    unit = np.asarray(raw_axis, dtype=np.float64)
    unit = unit / float(np.linalg.norm(unit))
    rot = so3_exp(theta * unit)
    recovered = so3_log(rot)
    roundtrip_err = float(np.linalg.norm(so3_exp(recovered) - rot))
    assert roundtrip_err < 1e-13, f"near-pi roundtrip error {roundtrip_err:.3e} >= 1e-13"


def test_se3_pose_group_closure_inverse_and_refusals() -> None:
    p1 = build_look_at_pose((0.0, -3.0, 1.6), (0.5, 0.0, 1.5), roll_rad=math.radians(7.5))
    p2 = build_look_at_pose((1.2, -2.0, 2.1), (-0.4, 0.2, 1.4))

    assert is_valid_se3(p1.matrix)
    assert is_valid_se3(p2.matrix)
    assert math.isclose(float(np.linalg.norm(p1.forward_axis)), 1.0, abs_tol=1e-12)

    prod = p1.compose(p2)
    assert is_valid_se3(prod.matrix)
    assert np.allclose(prod.matrix, p1.matrix @ p2.matrix, atol=1e-12)

    ident_right = p1.compose(p1.inverse())
    ident_left = p1.inverse().compose(p1)
    assert np.allclose(ident_right.matrix, np.eye(4), atol=1e-12)
    assert np.allclose(ident_left.matrix, np.eye(4), atol=1e-12)

    # Vertical zenith/nadir degeneracy handling in build_look_at_pose.
    zenith = build_look_at_pose((0.0, 0.0, 1.0), (0.0, 0.0, 5.0))
    y_look = build_look_at_pose((0.0, 0.0, 1.0), (0.0, 4.0, 1.0), world_up=(0.0, 1.0, 0.0))
    assert is_valid_se3(zenith.matrix)
    assert is_valid_se3(y_look.matrix)

    # Reject reflection matrix (det == -1), non-orthogonal matrix, bad shape, bad bottom row.
    reflection = np.diag([-1.0, 1.0, 1.0])
    with pytest.raises(CinematographyError, match="determinant"):
        CameraPoseSE3.from_rt(reflection, (0.0, 0.0, 0.0))
    with pytest.raises(CinematographyError, match="orthogonality"):
        CameraPoseSE3.from_rt(2.0 * np.eye(3), (0.0, 0.0, 0.0))
    with pytest.raises(CinematographyError, match="3x3"):
        CameraPoseSE3.from_rt(np.eye(2), (0.0, 0.0, 0.0))
    with pytest.raises(CinematographyError, match="3-vector"):
        CameraPoseSE3.from_rt(np.eye(3), (0.0, 1.0))

    bad_bottom = np.eye(4)
    bad_bottom[3, 3] = 2.0
    assert is_valid_se3(bad_bottom) is False
    assert is_valid_se3(np.eye(3)) is False

    with pytest.raises(CinematographyError, match="world_up"):
        build_look_at_pose((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), world_up=(0.0, 0.0, 0.0))
    with pytest.raises(CinematographyError, match="distinct"):
        build_look_at_pose((1.0, 1.0, 1.0), (1.0, 1.0, 1.0))
    with pytest.raises(CinematographyError, match="roll_rad"):
        build_look_at_pose((0.0, -2.0, 1.6), (0.0, 0.0, 1.6), roll_rad=float("nan"))


# --------------------------------------------------------------------------- #
# Mathematical Proof of the 180-Degree Axis of Action Law (10-Shot Dialogue)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("initial_side", [1, -1])
def test_10_shot_dialogue_strictly_obeys_180_degree_rule_and_se3_invariants(
    initial_side: int,
) -> None:
    parsed = parse_bilingual_script(TEN_SHOT_PERSIAN_DIALOGUE)
    assert len(parsed.scenes) == 1
    scene = parsed.scenes[0]
    assert len(scene.elements) == 10

    # Test on a non-trivial rotated 3D axis of action as well as canonical staging.
    axis = AxisOfAction(
        p1=(-1.25, -0.45, 1.58),
        p2=(1.15, 0.65, 1.62),
        initial_side=initial_side,
        character_a="سرگرد رضا",
        character_b="سارا",
    )
    plan = solve_scene_cinematography(scene, axis=axis, num_trajectory_samples=15)
    assert len(plan.shots) == 10
    assert plan.axis_report.compliant is True
    assert plan.axis_report.intentional_jumps == 0
    assert plan.axis_report.violations == ()

    p1 = axis.p1_vector
    p2 = axis.p2_vector
    identity = np.eye(3, dtype=np.float64)

    first_sign: int | None = None
    for shot in plan.shots:
        # 1. Verify primary pose and end pose R @ R.T == I and det(R) == +1.0.
        for pose in (shot.pose, shot.end_pose):
            rot = pose.rotation_matrix
            assert np.allclose(rot @ rot.T, identity, atol=1e-12)
            assert np.allclose(rot.T @ rot, identity, atol=1e-12)
            assert math.isclose(float(np.linalg.det(rot)), 1.0, abs_tol=1e-12)
            assert is_valid_se3(pose.matrix, tol=1e-12)

        # 2. Verify horizontal cross product ((cam_pos - p1) x (p2 - p1))_z never flips sign.
        cam_pos = shot.pose.translation_vector
        cross_3d = np.cross(cam_pos - p1, p2 - p1)
        cross_z = float(cross_3d[2])
        assert math.isclose(cross_z, horizontal_axis_cross(cam_pos, p1, p2), abs_tol=1e-12)
        assert abs(cross_z) > 0.5, "camera must stay strictly clear of the 180-degree line"
        sign = 1 if cross_z > 0.0 else -1
        if first_sign is None:
            first_sign = sign
        assert sign == first_sign == initial_side
        assert axis_side_sign(cam_pos, p1, p2) == initial_side

        # 3. Verify EVERY sample along the continuous camera trajectory obeys both invariants.
        for sample in shot.trajectory.samples:
            s_rot = sample.pose.rotation_matrix
            assert np.allclose(s_rot @ s_rot.T, identity, atol=1e-12)
            assert math.isclose(float(np.linalg.det(s_rot)), 1.0, abs_tol=1e-12)
            s_cross = horizontal_axis_cross(sample.pose.translation_vector, p1, p2)
            assert s_cross * initial_side > 0.0

        # 4. Verify canonical lens selection & exact Rule-of-Thirds pinhole framing.
        assert shot.lens.focal_length_mm in CANONICAL_FOCAL_LENGTHS_MM
        assert shot.lens.f_stop in CANONICAL_F_STOPS
        assert shot.framing.framing_error_uv < 1e-9


def test_intentional_axis_jump_flips_sign_exactly_once_and_holds_new_side() -> None:
    script = """\
INT. INTERROGATION ROOM - NIGHT

REZA
Tell me the truth.

SARA
I already did.

[AXIS JUMP]

REZA
(angrily)
Everything changes right now!

SARA
(whispering)
Then listen carefully.
"""
    parsed = parse_bilingual_script(script)
    plans = solve_script_cinematography(parsed, initial_axis_side=1)
    plan = plans[0]
    assert plan.axis_report.compliant is True
    assert plan.axis_report.intentional_jumps == 1

    signs = [
        axis_side_sign(s.pose.translation_vector, plan.axis.p1, plan.axis.p2) for s in plan.shots
    ]
    assert signs == [1, 1, -1, -1]
    # Complementary look directions swap consistently when the camera crosses the line.
    assert plan.shots[0].framing.look_direction == ScreenLookDirection.RIGHT
    assert plan.shots[1].framing.look_direction == ScreenLookDirection.LEFT
    assert plan.shots[2].framing.look_direction == ScreenLookDirection.LEFT
    assert plan.shots[3].framing.look_direction == ScreenLookDirection.RIGHT


def test_enforce_axis_of_action_and_violation_detector() -> None:
    axis = AxisOfAction(p1=(-1.0, 0.0, 1.6), p2=(1.0, 0.0, 1.6), initial_side=1)
    # Camera placed illegally on the +Y side (cross_z < 0) is reflected onto -Y (cross_z > 0).
    wrong_side = np.array([0.3, 2.4, 1.7], dtype=np.float64)
    assert axis_side_sign(wrong_side, axis.p1, axis.p2) == -1
    fixed = enforce_axis_of_action(wrong_side, axis, required_side=1)
    assert np.allclose(fixed, (0.3, -2.4, 1.7), atol=1e-12)
    assert axis_side_sign(fixed, axis.p1, axis.p2) == 1

    # Camera sitting directly on the axis line (cross_z == 0) is pushed off by min_clearance_m.
    on_axis = np.array([0.0, 0.0, 1.6], dtype=np.float64)
    assert axis_side_sign(on_axis, axis.p1, axis.p2) == 0
    cleared, on_trace = enforce_axis_of_action_with_trace(
        on_axis, axis, required_side=1, min_clearance_m=0.5
    )
    assert cleared[1] == pytest.approx(-0.5, abs=1e-12)
    assert isinstance(on_trace, AxisEnforcementTrace)
    assert on_trace.was_axis_reflected is True
    assert on_trace.original_axis_side == 0
    assert on_trace.enforced_axis_side == 1

    # Explicit trace checks for: (a) valid placement, (b) opposite side, (c) near-axis clearance,
    # and strict=True ValueError enforcement on illegal / near-line placements.
    valid_pos = np.array([0.0, -1.8, 1.6], dtype=np.float64)
    unchanged, ok_trace = enforce_axis_of_action_with_trace(
        valid_pos, axis, required_side=1, strict=True
    )
    assert np.allclose(unchanged, valid_pos, atol=1e-12)
    assert ok_trace.was_axis_reflected is False
    assert ok_trace.original_axis_side == 1

    _, opp_trace = enforce_axis_of_action_with_trace(wrong_side, axis, required_side=1)
    assert opp_trace.was_axis_reflected is True
    assert opp_trace.original_axis_side == -1
    assert opp_trace.enforced_axis_side == 1

    near_same_side = np.array([0.1, -0.12, 1.6], dtype=np.float64)
    near_fixed, near_trace = enforce_axis_of_action_with_trace(
        near_same_side, axis, required_side=1, min_clearance_m=0.45
    )
    assert near_fixed[1] == pytest.approx(-0.45, abs=1e-12)
    assert near_trace.was_axis_reflected is True
    assert near_trace.original_axis_side == 1

    with pytest.raises(ValueError, match="strict mode"):
        enforce_axis_of_action(wrong_side, axis, required_side=1, strict=True)
    with pytest.raises(ValueError, match="strict mode"):
        enforce_axis_of_action(
            near_same_side, axis, required_side=1, min_clearance_m=0.45, strict=True
        )

    # Multi-shot dialogue sequence where candidate cameras attempt illegal axis crossings:
    # strict_axis=True rejects the crossing, while default mode reflects & records provenance.
    multi_parsed = parse_bilingual_script(TEN_SHOT_PERSIAN_DIALOGUE)
    overrides = {
        2: (0.25, 2.20, 1.60),  # opposite side (-1) illegal crossing attempt
        5: (0.00, 0.00, 1.60),  # directly on the 180-degree line (0)
        7: (-0.15, -0.10, 1.60),  # inside forbidden near-axis clearance buffer (+1, <0.55m)
    }
    with pytest.raises(ValueError, match="strict mode"):
        solve_scene_cinematography(
            multi_parsed.scenes[0],
            axis=axis,
            camera_overrides=overrides,
            strict_axis=True,
        )

    end_overrides = {
        3: (0.15, 1.90, 1.60),  # valid start (+1), illegal end (-1)
        4: (0.00, 0.00, 1.60),  # DOLLY shot whose end sits right on the 180-degree line (0)
        8: (-0.20, 1.75, 1.60),  # PAN shot upgraded to DOLLY with reflected end (-1)
    }
    traced_plan = solve_scene_cinematography(
        multi_parsed.scenes[0],
        axis=axis,
        camera_overrides=overrides,
        end_camera_overrides=end_overrides,
    )
    assert traced_plan.axis_report.compliant is True
    assert traced_plan.axis_report.reflected_shot_indices == (2, 3, 4, 5, 7, 8)
    assert traced_plan.shots[8].trajectory.move_type == CameraMoveType.DOLLY
    assert traced_plan.shots[8].start_was_reflected is False
    assert traced_plan.shots[8].end_was_reflected is True
    assert traced_plan.shots[8].end_original_axis_side == -1
    assert isinstance(traced_plan.shots[2], ShotCameraPlan)
    assert traced_plan.shots[0].was_axis_reflected is False
    assert traced_plan.shots[0].original_axis_side == 1
    assert traced_plan.shots[0].start_was_reflected is False
    assert traced_plan.shots[0].end_was_reflected is False

    # F2-R1: Shot 3 & Shot 4 had valid start (+1, unreflected) and reflected end (-1 / 0).
    assert traced_plan.shots[3].start_was_reflected is False
    assert traced_plan.shots[3].start_original_axis_side == 1
    assert traced_plan.shots[3].end_was_reflected is True
    assert traced_plan.shots[3].end_original_axis_side == -1
    assert traced_plan.shots[3].was_axis_reflected is True
    assert traced_plan.shots[3].end_axis_trace.original_axis_side == -1

    assert traced_plan.shots[4].start_was_reflected is False
    assert traced_plan.shots[4].start_original_axis_side == 1
    assert traced_plan.shots[4].end_was_reflected is True
    assert traced_plan.shots[4].end_original_axis_side == 0

    assert traced_plan.shots[2].was_axis_reflected is True
    assert traced_plan.shots[2].original_axis_side == -1
    assert traced_plan.shots[2].start_was_reflected is True
    assert traced_plan.shots[5].was_axis_reflected is True
    assert traced_plan.shots[5].original_axis_side == 0
    assert traced_plan.shots[7].was_axis_reflected is True
    assert traced_plan.shots[7].original_axis_side == 1

    # Also test end_camera_overrides on an establishing CRANE shot.
    crane_parsed = parse_bilingual_script(
        "EXT. COURTYARD - DAY\nSunlight stretches slowly across the silent stone fountain."
    )
    crane_plan = solve_scene_cinematography(
        crane_parsed.scenes[0],
        axis=axis,
        end_camera_overrides={0: (0.0, 2.1, 2.0)},
    )
    assert crane_plan.axis_report.compliant is True
    assert crane_plan.shots[0].trajectory.move_type == CameraMoveType.CRANE
    assert crane_plan.shots[0].start_was_reflected is False
    assert crane_plan.shots[0].end_was_reflected is True
    assert crane_plan.shots[0].end_original_axis_side == -1

    # Forging an inconsistent trace flag alone is also caught by verify_180_degree_rule.
    bad_trace_shot = dataclasses.replace(traced_plan.shots[0], end_was_reflected=True)
    trace_rep = verify_180_degree_rule((bad_trace_shot,), axis)
    assert trace_rep.compliant is False
    assert any("trace inconsistent" in v for v in trace_rep.violations)

    # Forging an illegal crossing and an invalid rotation triggers verify_180_degree_rule.
    parsed = parse_bilingual_script("INT. ROOM - NIGHT\nREZA\nOne.\n\nSARA\nTwo.")
    plan = solve_scene_cinematography(parsed.scenes[0], axis=axis, num_trajectory_samples=3)
    illegal_pose = build_look_at_pose((0.0, 2.5, 1.6), (0.0, 0.0, 1.6))
    corrupted_rot_pose = CameraPoseSE3(
        rotation=((2.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
        translation=(0.0, 2.5, 1.6),
    )
    bad_sample = dataclasses.replace(plan.shots[1].trajectory.samples[1], pose=corrupted_rot_pose)
    bad_traj = dataclasses.replace(
        plan.shots[1].trajectory,
        samples=(
            plan.shots[1].trajectory.samples[0],
            bad_sample,
            plan.shots[1].trajectory.samples[2],
        ),
    )
    forged_shot = dataclasses.replace(
        plan.shots[1], pose=corrupted_rot_pose, end_pose=illegal_pose, trajectory=bad_traj
    )
    report = verify_180_degree_rule((plan.shots[0], forged_shot), axis)
    assert report.compliant is False
    assert len(report.violations) >= 3

    # Refusals on invalid axis and enforce parameters.
    with pytest.raises(CinematographyError, match="horizontal"):
        AxisOfAction(p1=(1.0, 1.0, 1.0), p2=(1.0, 1.0, 2.5))
    with pytest.raises(CinematographyError, match="initial_side"):
        AxisOfAction(p1=(-1.0, 0.0, 1.6), p2=(1.0, 0.0, 1.6), initial_side=0)
    with pytest.raises(CinematographyError, match="required_side"):
        enforce_axis_of_action((0.0, -2.0, 1.6), axis, required_side=2)
    with pytest.raises(CinematographyError, match="min_clearance_m"):
        enforce_axis_of_action((0.0, -2.0, 1.6), axis, required_side=1, min_clearance_m=0.0)


# --------------------------------------------------------------------------- #
# Quintic Minimum-Jerk Trajectory Derivative & Variational Optimality Proofs
# --------------------------------------------------------------------------- #


def test_minimum_jerk_polynomial_exact_boundary_and_finite_difference_derivatives() -> None:
    # Exact endpoint values at tau = 0 and tau = 1.
    s0, ds0, d2s0, d3s0 = minimum_jerk_scalar(0.0)
    s1, ds1, d2s1, d3s1 = minimum_jerk_scalar(1.0)
    assert s0 == 0.0 and ds0 == 0.0 and d2s0 == 0.0 and d3s0 == 60.0
    assert s1 == 1.0 and ds1 == 0.0 and d2s1 == 0.0 and d3s1 == 60.0

    # Midpoint symmetry at tau = 0.5: peak speed = 15/8 = 1.875, inflection d2s = 0.
    sm, dsm, d2sm, d3sm = minimum_jerk_scalar(0.5)
    assert sm == pytest.approx(0.5, abs=1e-15)
    assert dsm == pytest.approx(1.875, abs=1e-15)
    assert d2sm == 0.0
    assert d3sm == pytest.approx(-30.0, abs=1e-15)

    # Verify analytical polynomial matches 10*tau^3 - 15*tau^4 + 6*tau^5 and finite differences.
    taus = np.linspace(0.0, 1.0, 201)
    prof = minimum_jerk_profile(taus)
    assert prof.shape == (201, 4)
    expected_s = 10.0 * taus**3 - 15.0 * taus**4 + 6.0 * taus**5
    assert np.allclose(prof[:, 0], expected_s, atol=1e-14)
    # Monotonicity: ds/dtau >= 0 everywhere on [0, 1].
    assert float(np.min(prof[:, 1])) >= 0.0

    # Central finite-difference verification of 1st, 2nd, and 3rd derivatives.
    h = 1e-5
    for tau in (0.1, 0.25, 0.4, 0.63, 0.85):
        _, ds, d2s, d3s = minimum_jerk_scalar(tau)
        sp, dsp, d2sp, _ = minimum_jerk_scalar(tau + h)
        sm_, dsm_, d2sm_, _ = minimum_jerk_scalar(tau - h)
        assert math.isclose(ds, (sp - sm_) / (2.0 * h), rel_tol=1e-8, abs_tol=1e-9)
        assert math.isclose(d2s, (dsp - dsm_) / (2.0 * h), rel_tol=1e-8, abs_tol=1e-9)
        assert math.isclose(d3s, (d2sp - d2sm_) / (2.0 * h), rel_tol=1e-8, abs_tol=1e-9)

    # Variational optimality: integral_0^1 (s'''(tau))^2 dtau == 720, strictly minimal.
    fine = np.linspace(0.0, 1.0, 10_001)
    dfine = np.diff(fine)
    d3_opt = minimum_jerk_profile(fine)[:, 3]
    y_opt = d3_opt * d3_opt
    j_opt = float(np.sum(0.5 * (y_opt[1:] + y_opt[:-1]) * dfine))
    assert math.isclose(j_opt, 720.0, rel_tol=1e-6)
    # Perturb by eta(tau) = tau^3 * (1 - tau)^3 (vanishes up to 2nd derivative at 0 and 1).
    eta_d3 = 6.0 - 72.0 * fine + 180.0 * fine**2 - 120.0 * fine**3
    y_pert = (d3_opt + 0.5 * eta_d3) ** 2
    j_perturbed = float(np.sum(0.5 * (y_pert[1:] + y_pert[:-1]) * dfine))
    assert j_perturbed > j_opt + 1.0


@pytest.mark.parametrize(
    ("move_type", "crane_arc"),
    [
        (CameraMoveType.DOLLY, 0.0),
        (CameraMoveType.PAN, 0.0),
        (CameraMoveType.CRANE, 0.0),
        (CameraMoveType.CRANE, 0.25),
        (CameraMoveType.STATIC, 0.0),
    ],
)
def test_se3_camera_trajectory_has_zero_velocity_and_acceleration_at_endpoints(
    move_type: CameraMoveType, crane_arc: float
) -> None:
    start = build_look_at_pose((-1.5, -3.2, 1.6), (0.0, 0.0, 1.6))
    if move_type == CameraMoveType.STATIC:
        end = start
    elif move_type == CameraMoveType.PAN:
        end = build_look_at_pose((-1.5, -3.2, 1.6), (0.9, 0.2, 1.65))
    else:
        end = build_look_at_pose((-0.4, -2.1, 2.05), (0.2, 0.0, 1.55))

    duration = 3.2
    traj = generate_minimum_jerk_trajectory(
        start,
        end,
        duration_sec=duration,
        move_type=move_type,
        num_samples=21,
        crane_arc_m=crane_arc,
    )
    first = traj.samples[0]
    last = traj.samples[-1]

    # Exact zero linear & angular velocity and acceleration at tau = 0 and tau = 1.
    assert first.tau == 0.0 and last.tau == 1.0
    assert first.linear_velocity == (0.0, 0.0, 0.0)
    assert last.linear_velocity == (0.0, 0.0, 0.0)
    assert first.linear_acceleration == (0.0, 0.0, 0.0)
    assert last.linear_acceleration == (0.0, 0.0, 0.0)
    assert first.angular_velocity == (0.0, 0.0, 0.0)
    assert last.angular_velocity == (0.0, 0.0, 0.0)
    assert first.angular_acceleration == (0.0, 0.0, 0.0)
    assert last.angular_acceleration == (0.0, 0.0, 0.0)

    # Exact start and end pose coincidence.
    assert np.allclose(first.pose.matrix, start.matrix, atol=1e-12)
    assert np.allclose(last.pose.matrix, end.matrix, atol=1e-12)

    # Verify analytical 3D velocity & acceleration against central differences at tau=0.37.
    dtau = 1e-5
    dt = dtau * duration
    mid = evaluate_trajectory_at(start, end, 0.37, duration_sec=duration, crane_arc_m=crane_arc)
    plus = evaluate_trajectory_at(
        start, end, 0.37 + dtau, duration_sec=duration, crane_arc_m=crane_arc
    )
    minus = evaluate_trajectory_at(
        start, end, 0.37 - dtau, duration_sec=duration, crane_arc_m=crane_arc
    )
    num_vel = (plus.pose.translation_vector - minus.pose.translation_vector) / (2.0 * dt)
    num_acc = (
        plus.pose.translation_vector
        - 2.0 * mid.pose.translation_vector
        + minus.pose.translation_vector
    ) / (dt * dt)
    assert np.allclose(mid.linear_velocity, num_vel, atol=1e-6)
    assert np.allclose(mid.linear_acceleration, num_acc, atol=1e-5)


def test_trajectory_input_refusals() -> None:
    pose = build_look_at_pose((0.0, -2.0, 1.6), (0.0, 0.0, 1.6))
    for bad_tau in (-0.01, 1.01, float("inf"), float("nan")):
        with pytest.raises(CinematographyError, match="tau"):
            minimum_jerk_scalar(bad_tau)
    with pytest.raises(CinematographyError, match="non-empty"):
        minimum_jerk_profile([])
    with pytest.raises(CinematographyError, match="tau"):
        minimum_jerk_profile([0.0, 1.2])
    with pytest.raises(CinematographyError, match="duration_sec"):
        evaluate_trajectory_at(pose, pose, 0.5, duration_sec=0.0)
    with pytest.raises(CinematographyError, match="crane_arc_m"):
        evaluate_trajectory_at(pose, pose, 0.5, duration_sec=1.0, crane_arc_m=float("nan"))
    with pytest.raises(CinematographyError, match="num_samples"):
        generate_minimum_jerk_trajectory(pose, pose, duration_sec=1.0, num_samples=1)
    with pytest.raises(CinematographyError, match="num_samples"):
        generate_minimum_jerk_trajectory(pose, pose, duration_sec=1.0, num_samples=True)
    with pytest.raises(CinematographyError, match="duration_sec"):
        generate_minimum_jerk_trajectory(pose, pose, duration_sec=-1.0, num_samples=5)


# --------------------------------------------------------------------------- #
# Lens, Aperture (f-stop), Rule-of-Thirds & Multi-Character Scene Coverage
# --------------------------------------------------------------------------- #


def test_lens_aperture_dof_and_rule_of_thirds_selection() -> None:
    l24 = select_lens_and_aperture(0.15)
    l35 = select_lens_and_aperture(0.35)
    l50 = select_lens_and_aperture(0.55)
    l85 = select_lens_and_aperture(0.75)
    l135 = select_lens_and_aperture(0.92)
    assert (
        l24.focal_length_mm,
        l35.focal_length_mm,
        l50.focal_length_mm,
        l85.focal_length_mm,
        l135.focal_length_mm,
    ) == (24, 35, 50, 85, 135)
    assert l24.shot_scale == ShotScale.WIDE
    assert l135.shot_scale == ShotScale.EXTREME_CLOSE_UP
    assert l24.horizontal_fov_deg > l50.horizontal_fov_deg > l135.horizontal_fov_deg
    assert l135.f_stop < l24.f_stop
    assert l85.dof_near_m < l85.focus_distance_m < l85.dof_far_m

    # Hyperfocal infinity branch (24mm at f/11 focused beyond hyperfocal distance).
    est_ext_day = select_lens_and_aperture(
        0.2,
        is_establishing=True,
        setting=SceneSetting.EXT,
        time_of_day=TimeOfDay.DAY,
        focus_distance_m=15.0,
    )
    assert est_ext_day.f_stop == 11.0
    assert est_ext_day.dof_far_m == 1_000.0

    # Atmospheric establishing third placement on both sides of axis.
    pt_pos, _, _ = select_rule_of_thirds(
        2, axis_side=1, is_establishing=True, weather=WeatherCondition.RAIN
    )
    pt_neg, _, _ = select_rule_of_thirds(
        2, axis_side=-1, is_establishing=True, weather=WeatherCondition.FOG
    )
    assert pt_pos == RuleOfThirdsPoint.LOWER_LEFT
    assert pt_neg == RuleOfThirdsPoint.LOWER_RIGHT
    pt_act_neg, _, _ = select_rule_of_thirds(2, axis_side=-1, dramatic_tension=0.80)
    assert pt_act_neg == RuleOfThirdsPoint.UPPER_LEFT

    # Steep vertical viewpoint fallback + invalid argument refusals.
    steep_pose = build_rule_of_thirds_pose(
        (0.0, 0.0, 5.0), (0.01, 0.0, 1.0), focal_length_mm=35.0, target_uv=(1.0 / 3.0, 1.0 / 3.0)
    )
    assert is_valid_se3(steep_pose.matrix)
    with pytest.raises(CinematographyError, match="roll_rad"):
        build_rule_of_thirds_pose(
            (0.0, -2.0, 1.6),
            (0.0, 0.0, 1.6),
            focal_length_mm=50.0,
            target_uv=(0.5, 0.5),
            roll_rad=float("inf"),
        )

    with pytest.raises(CinematographyError, match="dramatic_tension"):
        select_lens_and_aperture(1.5)
    with pytest.raises(CinematographyError, match="focus_distance_m"):
        select_lens_and_aperture(0.5, focus_distance_m=-1.0)
    with pytest.raises(CinematographyError, match="axis_side"):
        select_rule_of_thirds(0, axis_side=0)
    with pytest.raises(CinematographyError, match="focal_length_mm"):
        project_world_point(steep_pose, (0.0, 0.0, 1.0), 0.0)
    with pytest.raises(CinematographyError, match="behind"):
        project_world_point(steep_pose, (0.0, 0.0, 9.0), 35.0)
    with pytest.raises(CinematographyError, match="focal_length_mm"):
        build_rule_of_thirds_pose(
            (0.0, -2.0, 1.6), (0.0, 0.0, 1.6), focal_length_mm=0.0, target_uv=(0.5, 0.5)
        )
    with pytest.raises(CinematographyError, match="target_uv"):
        build_rule_of_thirds_pose(
            (0.0, -2.0, 1.6), (0.0, 0.0, 1.6), focal_length_mm=50.0, target_uv=(1.0, 0.5)
        )
    with pytest.raises(CinematographyError, match="distinct"):
        build_rule_of_thirds_pose(
            (0.0, 0.0, 1.6), (0.0, 0.0, 1.6), focal_length_mm=50.0, target_uv=(0.5, 0.5)
        )


def test_ensemble_four_character_scene_and_action_moves() -> None:
    script = """\
EXT. WAREHOUSE COURTYARD - NIGHT - RAIN

A quiet wide view of the wet concrete yard.

REZA
No.

SARA
Everything is quiet here tonight while we wait by the main entrance.

AMIR
I checked the eastern perimeter twice already and saw nobody.

MINA
(angrily)
Watch out! Get down right now!!

REZA draws a gun and slams the iron door shut with an explosion!

REZA and SARA stand shoulder to shoulder in the rain.

AMIR steps forward alone.
"""
    parsed = parse_bilingual_script(script)
    plan = solve_scene_cinematography(parsed.scenes[0])
    assert plan.axis_report.compliant is True
    assert len(plan.character_positions) == 4
    move_types = {shot.trajectory.move_type for shot in plan.shots}
    assert CameraMoveType.STATIC in move_types
    assert CameraMoveType.DOLLY in move_types
    assert CameraMoveType.CRANE in move_types
    assert CameraMoveType.PAN in move_types

    with pytest.raises(CinematographyError, match="starting_shot_index"):
        solve_scene_cinematography(parsed.scenes[0], starting_shot_index=-1)
    with pytest.raises(CinematographyError, match="num_trajectory_samples"):
        solve_scene_cinematography(parsed.scenes[0], num_trajectory_samples=1)


def test_f7_live_axis_trace_replay_rejects_forged_original_position_distance_and_cross_z() -> None:
    parsed = parse_bilingual_script(
        "INT. INTERROGATION ROOM - NIGHT\n"
        "COLE\n(angrily)\nTell me where you hid the ledger tonight!\n\n"
        "MAYA\nI left it inside the safe downstairs."
    )
    plan = solve_scene_cinematography(parsed.scenes[0])
    assert plan.axis_report.compliant is True
    sh0, sh1 = plan.shots[0], plan.shots[1]

    # 1. Forged start_axis_trace.original_position on same side of axis.
    orig_p = sh0.start_axis_trace.original_position
    forged_start_pos = dataclasses.replace(
        sh0.start_axis_trace,
        original_position=(orig_p[0] + 0.35, orig_p[1], orig_p[2]),
    )
    rep_pos = verify_180_degree_rule(
        (dataclasses.replace(sh0, start_axis_trace=forged_start_pos), sh1),
        plan.axis,
    )
    assert rep_pos.compliant is False

    # 2. Forged start_axis_trace.original_signed_distance_m.
    forged_start_dist = dataclasses.replace(
        sh0.start_axis_trace,
        original_signed_distance_m=sh0.start_axis_trace.original_signed_distance_m + 0.5,
    )
    rep_dist = verify_180_degree_rule(
        (dataclasses.replace(sh0, start_axis_trace=forged_start_dist), sh1),
        plan.axis,
    )
    assert rep_dist.compliant is False

    # 3. Forged end_axis_trace.enforced_signed_distance_m & non-finite original_position.
    forged_end_dist = dataclasses.replace(
        sh0.end_axis_trace,
        enforced_signed_distance_m=sh0.end_axis_trace.enforced_signed_distance_m + 0.25,
    )
    assert (
        verify_180_degree_rule(
            (dataclasses.replace(sh0, end_axis_trace=forged_end_dist), sh1),
            plan.axis,
        ).compliant
        is False
    )
    nan_trace = dataclasses.replace(
        sh1.end_axis_trace,
        original_position=(float("nan"), 0.0, 1.6),
    )
    assert (
        verify_180_degree_rule(
            (sh0, dataclasses.replace(sh1, end_axis_trace=nan_trace)),
            plan.axis,
        ).compliant
        is False
    )

    # 4. Forged axis_cross_z, end_axis_cross_z, or axis_side on CameraShotSpec.
    assert (
        verify_180_degree_rule(
            (dataclasses.replace(sh0, axis_cross_z=sh0.axis_cross_z + 0.5), sh1),
            plan.axis,
        ).compliant
        is False
    )
    assert (
        verify_180_degree_rule(
            (dataclasses.replace(sh0, end_axis_cross_z=sh0.end_axis_cross_z + 0.5), sh1),
            plan.axis,
        ).compliant
        is False
    )
    assert (
        verify_180_degree_rule(
            (dataclasses.replace(sh0, axis_side=-sh0.axis_side), sh1),
            plan.axis,
        ).compliant
        is False
    )


def test_wave4_angular_jerk_clearance_spoof_move_type_and_rain_establishing() -> None:
    # 1. Verify exact analytical angular_jerk == (d3s_dtau3 / T**3) * (R_0 @ omega_rel)
    #    and central finite-difference agreement with angular_acceleration.
    start = build_look_at_pose((-1.2, -2.8, 1.6), (0.0, 0.0, 1.6))
    end = build_look_at_pose((-0.3, -2.0, 1.9), (0.6, 0.1, 1.5))
    duration = 2.5
    omega_world = start.rotation_matrix @ so3_log(start.rotation_matrix.T @ end.rotation_matrix)
    for tau in (0.0, 0.25, 0.5, 0.75, 1.0):
        smp = evaluate_trajectory_at(start, end, tau, duration_sec=duration, crane_arc_m=0.12)
        expected_aj = (smp.d3s_dtau3 / (duration**3)) * omega_world
        assert np.allclose(smp.angular_jerk, expected_aj, atol=1e-12)

    h = 1e-5
    smp_mid = evaluate_trajectory_at(start, end, 0.4, duration_sec=duration)
    smp_p = evaluate_trajectory_at(start, end, 0.4 + h, duration_sec=duration)
    smp_m = evaluate_trajectory_at(start, end, 0.4 - h, duration_sec=duration)
    fd_aj = (np.array(smp_p.angular_acceleration) - np.array(smp_m.angular_acceleration)) / (
        2.0 * h * duration
    )
    assert np.allclose(smp_mid.angular_jerk, fd_aj, rtol=1e-7, atol=1e-8)

    # 2. Clearance-zone original_position spoofing & move_type spoofing in verify_180_degree_rule:
    #    Even when solve_scene_cinematography produced a clamped shot via camera_overrides,
    #    calling standalone verify_180_degree_rule (without overrides) rejects non-canonical
    #    original_position inside the clearance zone, AND spoofing move_type is rejected.
    axis = AxisOfAction(p1=(-1.0, 0.0, 1.6), p2=(1.0, 0.0, 1.6), initial_side=1)
    parsed = parse_bilingual_script(
        "EXT. ALLEY - NIGHT - RAIN\n"
        "REZA waits quietly under the wet awning.\n\n"
        "REZA\nNo.\n\n"
        "SARA\n(angrily)\nGet back inside right now!"
    )
    rain_plan = solve_scene_cinematography(parsed.scenes[0], axis=axis)
    assert rain_plan.axis_report.compliant is True

    # Relabeling STATIC shot 1 as DOLLY (to switch end_clearance from 0.55 to 0.50) fails.
    sh_static = rain_plan.shots[1]
    assert sh_static.trajectory.move_type == CameraMoveType.STATIC
    spoofed_move_shot = dataclasses.replace(
        sh_static,
        trajectory=dataclasses.replace(sh_static.trajectory, move_type=CameraMoveType.DOLLY),
    )
    assert (
        verify_180_degree_rule(
            (rain_plan.shots[0], spoofed_move_shot, rain_plan.shots[2]), axis
        ).compliant
        is False
    )

    # Override shot 2 inside clearance zone (-0.12m -> clamped to -0.55m), then spoof
    # original_position to a DIFFERENT clearance-zone point (-0.25m) that maps to the
    # exact same adjusted_position (-0.55m)!
    clamped_plan = solve_scene_cinematography(
        parsed.scenes[0],
        axis=axis,
        camera_overrides={2: (0.10, -0.12, 1.60)},
    )
    assert clamped_plan.axis_report.compliant is True
    # Standalone verify_180_degree_rule without overrides rejects the overridden shot.
    assert verify_180_degree_rule(clamped_plan.shots, axis).compliant is False
    # Even WITH the original override supplied, spoofing original_position to (0.10, -0.25, 1.60)
    # (which enforce_axis_of_action_with_trace ALSO clamps to (0.10, -0.55, 1.60)!) is caught!
    _, spoofed_trace = enforce_axis_of_action_with_trace(
        (0.10, -0.25, 1.60), axis, required_side=1, min_clearance_m=0.55
    )
    sh2_spoofed = dataclasses.replace(
        clamped_plan.shots[2],
        start_axis_trace=spoofed_trace,
        start_original_axis_side=spoofed_trace.original_axis_side,
        start_was_reflected=spoofed_trace.was_axis_reflected,
        original_axis_side=spoofed_trace.original_axis_side,
        was_axis_reflected=True,
    )
    assert (
        verify_180_degree_rule(
            (clamped_plan.shots[0], clamped_plan.shots[1], sh2_spoofed),
            axis,
            camera_overrides={2: (0.10, -0.12, 1.60)},
        ).compliant
        is False
    )


# --------------------------------------------------------------------------- #
# Wave-5: Canonical character blocking, subject binding, and axis canonicality
# --------------------------------------------------------------------------- #


def _shift(pos: Vec3, dx: float, dy: float, dz: float) -> tuple[float, float, float]:
    return (pos[0] + dx, pos[1] + dy, pos[2] + dz)


def _same_point(a: Vec3, b: Vec3) -> bool:
    return len(a) == len(b) and all(
        math.isclose(x, y, rel_tol=0.0, abs_tol=1e-9) for x, y in zip(a, b, strict=False)
    )


def test_wave5_canonical_blocking_helpers_reproduce_solver_staging() -> None:
    parsed = parse_bilingual_script(TEN_SHOT_PERSIAN_DIALOGUE)
    scene = parsed.scenes[0]
    plan = solve_scene_cinematography(scene)

    canonical = compute_canonical_character_positions(
        scene, axis=plan.axis, initial_axis_side=plan.axis.initial_side
    )
    assert dict(plan.character_positions) == canonical
    assert [name for name, _ in plan.character_positions] == list(canonical)

    expected_axis = canonical_axis_of_action(scene, 1)
    assert _same_point(plan.axis.p1, expected_axis.p1)
    assert _same_point(plan.axis.p2, expected_axis.p2)
    assert plan.axis.character_a == expected_axis.character_a
    assert plan.axis.character_b == expected_axis.character_b

    for idx, elem in enumerate(scene.elements):
        label, position = expected_shot_subject(
            scene, elem, canonical, axis=plan.axis, initial_axis_side=1
        )
        assert plan.shots[idx].subject_label == label
        assert _same_point(plan.shots[idx].subject_position, position)

    assert (
        verify_scene_character_blocking(
            plan.shots,
            plan.axis,
            scene=scene,
            character_positions=plan.character_positions,
        )
        is True
    )
    assert (
        verify_180_degree_rule(
            plan.shots,
            plan.axis,
            scene=scene,
            character_positions=plan.character_positions,
        ).compliant
        is True
    )
    # A canonical Mapping input is accepted exactly like the solver's tuple form.
    assert (
        verify_scene_character_blocking(
            plan.shots, plan.axis, scene=scene, character_positions=canonical
        )
        is True
    )
    # Without a scene the verifier stays purely geometric (backward compatible).
    assert (
        verify_scene_character_blocking(plan.shots, plan.axis, scene=None, character_positions=None)
        is True
    )


def test_wave5_expected_shot_subject_covers_dialogue_single_multi_and_no_focus() -> None:
    script = """\
EXT. WAREHOUSE COURTYARD - NIGHT - RAIN

A quiet wide view of the wet concrete yard.

REZA
No.

SARA
Everything is quiet here tonight while we wait by the main entrance.

REZA draws a gun and slams the iron door shut with an explosion!

REZA and SARA stand shoulder to shoulder in the rain.
"""
    scene = parse_bilingual_script(script).scenes[0]
    plan = solve_scene_cinematography(scene)
    positions = compute_canonical_character_positions(scene, axis=plan.axis, initial_axis_side=1)
    midpoint = _shift(plan.axis.p1, 1.10, 0.0, 0.0)
    assert _same_point(midpoint, (0.0, 0.0, plan.axis.p1[2]))

    labels: list[str] = []
    for elem in scene.elements:
        label, position = expected_shot_subject(
            scene, elem, positions, axis=plan.axis, initial_axis_side=1
        )
        labels.append(label)
        if isinstance(elem, DialogueLine):
            assert label == elem.character
            assert _same_point(position, positions[label])
        elif len(elem.focus_characters) == 1:
            assert label == elem.focus_characters[0]
            assert _same_point(position, positions[label])
        elif len(elem.focus_characters) >= 2:
            assert label == f"{elem.focus_characters[0]} & {elem.focus_characters[1]}"
            assert _same_point(position, midpoint)
        else:
            assert label == scene.heading.location
            assert _same_point(position, midpoint)

    assert [shot.subject_label for shot in plan.shots] == labels
    assert "REZA & SARA" in labels
    assert plan.shots[0].subject_label == scene.heading.location
    assert plan.shots[0].shot_kind is ShotKind.ACTION


def test_wave5_blocking_verifier_rejects_forged_positions_labels_and_geometry() -> None:
    parsed = parse_bilingual_script(TEN_SHOT_PERSIAN_DIALOGUE)
    scene = parsed.scenes[0]
    plan = solve_scene_cinematography(scene)
    assert (
        verify_scene_character_blocking(
            plan.shots,
            plan.axis,
            scene=scene,
            character_positions=plan.character_positions,
        )
        is True
    )

    # (a) Shift every declared character position while leaving shots untouched.
    shifted_pairs = tuple(
        (name, _shift(pos, 0.30, 0.20, 0.10)) for name, pos in plan.character_positions
    )
    assert (
        verify_scene_character_blocking(
            plan.shots, plan.axis, scene=scene, character_positions=shifted_pairs
        )
        is False
    )

    # (b) Fully re-stage the scene: move positions AND every shot subject together, then even the
    #     per-shot consistency check passes -- only the scene-derived canonical check catches it.
    re_staged_shots = tuple(
        dataclasses.replace(sh, subject_position=_shift(sh.subject_position, 0.30, 0.20, 0.10))
        for sh in plan.shots
    )
    assert (
        verify_scene_character_blocking(
            re_staged_shots, plan.axis, scene=None, character_positions=shifted_pairs
        )
        is True
    )
    assert (
        verify_scene_character_blocking(
            re_staged_shots, plan.axis, scene=scene, character_positions=shifted_pairs
        )
        is False
    )
    forged_report = verify_180_degree_rule(
        re_staged_shots, plan.axis, scene=scene, character_positions=shifted_pairs
    )
    assert forged_report.compliant is False
    assert any(
        "character_positions or shot subject blocking" in v for v in forged_report.violations
    )

    # (c) Swap the subject_label of two reverse shots (camera stays where it is).
    swapped = (
        dataclasses.replace(plan.shots[0], subject_label=plan.shots[1].subject_label),
        dataclasses.replace(plan.shots[1], subject_label=plan.shots[0].subject_label),
        *plan.shots[2:],
    )
    assert (
        verify_scene_character_blocking(
            swapped, plan.axis, scene=scene, character_positions=plan.character_positions
        )
        is False
    )

    # (d) Malformed position ledgers: duplicate names and non-finite coordinates.
    dupe = (plan.character_positions[0], plan.character_positions[0], *plan.character_positions[2:])
    assert (
        verify_scene_character_blocking(
            plan.shots, plan.axis, scene=scene, character_positions=dupe
        )
        is False
    )
    nan_pairs = ((name, (float("nan"), pos[1], pos[2])) for name, pos in plan.character_positions)
    assert (
        verify_scene_character_blocking(
            plan.shots, plan.axis, scene=scene, character_positions=tuple(nan_pairs)
        )
        is False
    )

    # (e) A shot whose subject_position disagrees with the plan's own blocking ledger.
    drift_shot = dataclasses.replace(
        plan.shots[0], subject_position=_shift(plan.shots[0].subject_position, 0.05, 0.0, 0.0)
    )
    assert (
        verify_scene_character_blocking(
            (drift_shot, *plan.shots[1:]),
            plan.axis,
            scene=None,
            character_positions=plan.character_positions,
        )
        is False
    )

    # (f) Shot count must stay in bijection with the scene's element count.
    assert (
        verify_scene_character_blocking(
            plan.shots[:-1], plan.axis, scene=scene, character_positions=plan.character_positions
        )
        is False
    )

    # (f2) A supporting actor who never fronts a shot is still bound by the canonical ledger.
    four_char = parse_bilingual_script(
        "INT. COURTHOUSE STEPS - DAY\n"
        "REZA\nThe hearing starts at noon, so we walk now.\n\n"
        "SARA\nI will bring the file from the car."
    ).scenes[0]
    beat = ActionBeat(
        element_index=len(four_char.elements),
        text="NIAZ and HAMID wait by the pillar.",
        language=ScriptLanguage.EN,
        duration_sec=1.75,
        dramatic_tension=0.2,
        focus_characters=("NIAZ", "HAMID"),
    )
    staged = dataclasses.replace(
        four_char,
        characters=(*four_char.characters, "NIAZ", "HAMID"),
        elements=(*four_char.elements, beat),
    )
    crowd_plan = solve_scene_cinematography(staged)
    assert crowd_plan.axis_report.compliant is True
    assert [name for name, _ in crowd_plan.character_positions] == ["REZA", "SARA", "NIAZ", "HAMID"]
    assert crowd_plan.shots[-1].subject_label == "NIAZ & HAMID"
    assert all(sh.subject_label != "NIAZ" for sh in crowd_plan.shots)
    ledger = list(crowd_plan.character_positions)
    niaz_pos = ledger[2][1]
    ledger[2] = ("NIAZ", (niaz_pos[0] + 0.9, niaz_pos[1] + 0.4, niaz_pos[2]))
    forged_ledger = tuple(ledger)
    assert (
        verify_scene_character_blocking(
            crowd_plan.shots,
            crowd_plan.axis,
            scene=staged,
            character_positions=forged_ledger,
        )
        is False
    )
    # Without the scene-derived ledger, this forgery is invisible: the shots themselves are legal.
    assert (
        verify_scene_character_blocking(
            crowd_plan.shots, crowd_plan.axis, scene=None, character_positions=forged_ledger
        )
        is True
    )

    # (g) Scene tampering that makes a declared speaker unstaged must return False, not raise.
    orphaned = dataclasses.replace(scene, characters=(scene.characters[1],))
    assert (
        verify_scene_character_blocking(
            plan.shots, plan.axis, scene=orphaned, character_positions=None
        )
        is False
    )
    assert verify_180_degree_rule(plan.shots, plan.axis, scene=orphaned).compliant is False
