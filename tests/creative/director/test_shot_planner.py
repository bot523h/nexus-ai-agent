"""Tests for the AETHEL shot planner (180-degree, 30-degree, eyeline, proxemics)."""

from __future__ import annotations

import pytest

from nexus_ai_agent.creative.director import (
    ActorPlacement,
    AxisViolationError,
    CameraSetup,
    HallBand,
    JumpCutError,
    ProxemicsError,
    ShotPlanRequest,
    ShotSize,
    camera_angle_deg,
    check_eyeline_match,
    check_jump_cut,
    check_proxemics,
    hall_band,
    plan_shot_sequence,
    side_of_line,
)


def _pair() -> tuple[ActorPlacement, ActorPlacement]:
    return (ActorPlacement("mara", 0.0, 0.0, 0.0), ActorPlacement("ivo", 2.0, 0.0, 180.0))


def test_side_of_line_sign() -> None:
    a, b = _pair()
    # L = p_B - p_A = (2, 0); cross = vx*ly - vy*lx = -vy*2
    assert side_of_line((0.0, 1.0), a, b) == -1
    assert side_of_line((0.0, -1.0), a, b) == 1
    assert side_of_line((1.0, 0.0), a, b) == 0  # on the line


def test_plan_keeps_one_axis_side() -> None:
    a, b = _pair()
    seq = plan_shot_sequence(ShotPlanRequest(0, (a, b), ("mara", "ivo"), axis_side=1))
    assert all(s.axis_side == 1 for s in seq.setups)
    assert [s.shot_size for s in seq.setups] == [
        ShotSize.ESTABLISHING,
        ShotSize.MASTER,
        ShotSize.OTS,
        ShotSize.OTS,
        ShotSize.CLOSE_UP,
        ShotSize.INSERT,
    ]


def test_plan_other_side() -> None:
    a, b = _pair()
    seq = plan_shot_sequence(ShotPlanRequest(0, (a, b), ("mara", "ivo"), axis_side=-1))
    assert all(s.axis_side == -1 for s in seq.setups)


def test_plan_is_deterministic() -> None:
    a, b = _pair()
    req = ShotPlanRequest(3, (a, b), ("mara", "ivo"))
    assert plan_shot_sequence(req) == plan_shot_sequence(req)


def test_eyeline_match() -> None:
    a = ActorPlacement("a", 0, 0, 0.0)  # looks +x (right)
    b = ActorPlacement("b", 2, 0, 180.0)  # looks -x (left)
    assert check_eyeline_match(a, b).matched is True


def test_eyeline_mismatch() -> None:
    a = ActorPlacement("a", 0, 0, 0.0)  # right
    b = ActorPlacement("b", 2, 0, 0.0)  # also right
    assert check_eyeline_match(a, b).matched is False


def test_plan_flags_eyeline_mismatch() -> None:
    a = ActorPlacement("mara", 0, 0, 0.0)
    b = ActorPlacement("ivo", 2, 0, 0.0)
    seq = plan_shot_sequence(ShotPlanRequest(0, (a, b), ("mara", "ivo")))
    assert "eyeline_mismatch" in seq.notes


def test_min_separation_violation() -> None:
    a = ActorPlacement("a", 0.0, 0.0)
    b = ActorPlacement("b", 0.1, 0.0)
    with pytest.raises(ProxemicsError):
        check_proxemics((a, b))


def test_proxemics_bands() -> None:
    actors = (
        ActorPlacement("a", 0.0, 0.0),
        ActorPlacement("b", 0.6, 0.0),
        ActorPlacement("c", 2.0, 0.0),
    )
    bands = check_proxemics(actors)
    assert bands[0] is HallBand.PERSONAL
    assert bands[1] is HallBand.SOCIAL


def test_hall_band_boundaries() -> None:
    assert hall_band(0.2) is HallBand.INTIMATE
    assert hall_band(0.45) is HallBand.PERSONAL
    assert hall_band(1.2) is HallBand.SOCIAL
    assert hall_band(3.61) is HallBand.PUBLIC


def test_hall_band_rejects_negative() -> None:
    with pytest.raises(ValueError):
        hall_band(-1.0)


def test_camera_angle_deg() -> None:
    subject = ActorPlacement("s", 0.0, 0.0)
    # two cameras 90 degrees apart around the subject
    assert camera_angle_deg(subject, (1.0, 0.0), (0.0, 1.0)) == pytest.approx(90.0)


def test_jump_cut_same_angle_and_focal() -> None:
    subject = ActorPlacement("s", 0.0, 0.0)
    a = CameraSetup("a", ShotSize.MEDIUM, "s", 2.0, 0.0, 50, 1)
    b = CameraSetup("b", ShotSize.MEDIUM, "s", 2.0, 0.0, 50, 1)
    with pytest.raises(JumpCutError):
        check_jump_cut(a, b, subject)


def test_no_jump_cut_when_focal_changes_enough() -> None:
    subject = ActorPlacement("s", 0.0, 0.0)
    a = CameraSetup("a", ShotSize.MEDIUM, "s", 2.0, 0.0, 50, 1)
    b = CameraSetup("b", ShotSize.CLOSE_UP, "s", 2.0, 0.0, 85, 1)
    check_jump_cut(a, b, subject)  # no raise: focal delta 70% >= 25%


def test_no_jump_cut_for_different_subject() -> None:
    subject = ActorPlacement("s", 0.0, 0.0)
    a = CameraSetup("a", ShotSize.MEDIUM, "s", 2.0, 0.0, 50, 1)
    b = CameraSetup("b", ShotSize.MEDIUM, "t", 2.0, 0.0, 50, 1)
    check_jump_cut(a, b, subject)  # different subject -> no raise


def test_missing_actor_is_refused() -> None:
    a = ActorPlacement("mara", 0.0, 0.0)
    with pytest.raises(ProxemicsError):
        plan_shot_sequence(ShotPlanRequest(0, (a,), ("mara", "ghost")))


def test_request_rejects_bad_axis() -> None:
    a, b = _pair()
    with pytest.raises(ValueError):
        ShotPlanRequest(0, (a, b), ("mara", "ivo"), axis_side=2)


def test_request_rejects_same_pair() -> None:
    a, _ = _pair()
    with pytest.raises(ValueError):
        ShotPlanRequest(0, (a,), ("mara", "mara"))


def test_axis_violation_is_detected(monkeypatch) -> None:
    # force a wrong-side camera by breaking the rotation
    from nexus_ai_agent.creative.director import shot_planner as sp

    a, b = _pair()
    orig = sp._camera_for

    def broken(request, subject, partner, size, idx, *, reset=False):  # type: ignore[no-untyped-def]
        setup = orig(request, subject, partner, size, idx, reset=reset)
        return CameraSetup(
            setup.setup_id,
            setup.shot_size,
            setup.subject_id,
            setup.cam_x,
            setup.cam_y,
            setup.focal_mm,
            -request.axis_side,
        )

    monkeypatch.setattr(sp, "_camera_for", broken)
    with pytest.raises(AxisViolationError):
        plan_shot_sequence(ShotPlanRequest(0, (a, b), ("mara", "ivo"), axis_side=1))
