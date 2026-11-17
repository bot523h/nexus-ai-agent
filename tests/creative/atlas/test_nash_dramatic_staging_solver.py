"""WAVE 22 — tests for the Nash dramatic staging solver.

Checks the potential formulation (proxemic wells, collision barrier, camera
line-of-sight, Rembrandt key-light geometry), the Riemannian ascent solver, and
the measured ``eps``-Nash certification.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from nexus_ai_agent.creative.atlas import nash_dramatic_staging_solver as nds


def _problem(*, speakers: tuple[int, ...] = (0, 1)) -> nds.DramaticStagingProblem:
    return nds.DramaticStagingProblem(
        actor_count=3,
        camera_position=np.array([0.0, -6.0]),
        camera_yaw=0.0,
        key_light_azimuth=0.6,
        interactions=((0, 1, "personal"), (1, 2, "social")),
        speakers=speakers,
    )


def _initial() -> np.ndarray:
    return np.array([[-1.0, 0.0, 0.0], [0.5, 0.3, 0.2], [2.0, -0.5, -0.3]])


# --- constructor validation -------------------------------------------------------


@pytest.mark.parametrize("count", [1, nds.MAX_ACTORS + 1])
def test_problem_rejects_actor_count(count: int) -> None:
    with pytest.raises(nds.StagingError):
        nds.DramaticStagingProblem(
            actor_count=count,
            camera_position=np.zeros(2),
            camera_yaw=0.0,
            key_light_azimuth=0.0,
            interactions=(),
            speakers=(),
        )


def test_problem_rejects_bad_camera_shape() -> None:
    with pytest.raises(nds.StagingError):
        nds.DramaticStagingProblem(
            actor_count=2,
            camera_position=np.zeros(3),
            camera_yaw=0.0,
            key_light_azimuth=0.0,
            interactions=(),
            speakers=(),
        )


def test_problem_rejects_non_finite_camera() -> None:
    with pytest.raises(nds.StagingError):
        nds.DramaticStagingProblem(
            actor_count=2,
            camera_position=np.array([np.nan, 0.0]),
            camera_yaw=0.0,
            key_light_azimuth=0.0,
            interactions=(),
            speakers=(),
        )


def test_problem_rejects_self_interaction() -> None:
    with pytest.raises(nds.StagingError):
        nds.DramaticStagingProblem(
            actor_count=2,
            camera_position=np.zeros(2),
            camera_yaw=0.0,
            key_light_azimuth=0.0,
            interactions=((0, 0, "personal"),),
            speakers=(),
        )


def test_problem_rejects_unknown_preset() -> None:
    with pytest.raises(nds.StagingError):
        nds.DramaticStagingProblem(
            actor_count=2,
            camera_position=np.zeros(2),
            camera_yaw=0.0,
            key_light_azimuth=0.0,
            interactions=((0, 1, "cosmic"),),
            speakers=(),
        )


def test_problem_rejects_speaker_out_of_range() -> None:
    with pytest.raises(nds.StagingError):
        nds.DramaticStagingProblem(
            actor_count=2,
            camera_position=np.zeros(2),
            camera_yaw=0.0,
            key_light_azimuth=0.0,
            interactions=(),
            speakers=(5,),
        )


def test_problem_rejects_bad_collision_distance() -> None:
    with pytest.raises(nds.StagingError):
        nds.DramaticStagingProblem(
            actor_count=2,
            camera_position=np.zeros(2),
            camera_yaw=0.0,
            key_light_azimuth=0.0,
            interactions=(),
            speakers=(),
            d_collision=0.0,
        )


def test_proxemic_preset_helper() -> None:
    assert nds._proxemic_preset("intimate") == 0.6
    assert nds._proxemic_preset("public") == 4.0
    with pytest.raises(nds.StagingError):
        nds._proxemic_preset("unknown")


def test_wrap_angle_is_bounded() -> None:
    for angle in (-10.0, 0.0, 3.0, 10.0):
        assert -math.pi <= nds._wrap_angle(angle) <= math.pi


# --- the solver -------------------------------------------------------------------


def test_solver_produces_collision_free_staging() -> None:
    report = nds.solve_nash_dramatic_staging(
        _problem(), _initial(), epsilon=1e-2, max_iterations=80, response_samples=3
    )
    assert report.collisions == 0
    assert report.min_pairwise_distance >= _problem().d_collision - 1e-9


def test_solver_keeps_speakers_visible() -> None:
    report = nds.solve_nash_dramatic_staging(
        _problem(), _initial(), epsilon=1e-2, max_iterations=80, response_samples=3
    )
    assert report.camera_visibility_fraction == pytest.approx(1.0)


def test_solver_approaches_target_proxemics() -> None:
    problem = _problem()
    report = nds.solve_nash_dramatic_staging(
        problem, _initial(), epsilon=1e-2, max_iterations=120, response_samples=3
    )
    distance = float(np.linalg.norm(report.poses[0, :2] - report.poses[1, :2]))
    assert abs(distance - nds.PROXEMIC_PRESETS["personal"]) < 0.1


def test_solver_improves_potential() -> None:
    problem = _problem()
    report = nds.solve_nash_dramatic_staging(
        problem, _initial(), epsilon=1e-2, max_iterations=80, response_samples=3
    )
    assert report.potential >= nds._potential(_initial(), problem) - 1e-9


def test_solver_certifies_nash() -> None:
    report = nds.solve_nash_dramatic_staging(
        _problem(),
        _initial(),
        epsilon=1e-2,
        max_iterations=80,
        response_samples=3,
        response_radius=0.1,
    )
    assert report.nash_gain <= 1e-2
    assert report.is_nash is True


def test_solver_resolves_initial_collision() -> None:
    colliding = np.array([[0.0, 0.0, 0.0], [0.05, 0.0, 0.1], [3.0, 0.0, 0.0]])
    report = nds.solve_nash_dramatic_staging(
        _problem(), colliding, epsilon=1e-2, max_iterations=200, response_samples=3
    )
    assert report.collisions == 0


def test_solver_rejects_wrong_pose_shape() -> None:
    with pytest.raises(nds.StagingError):
        nds.solve_nash_dramatic_staging(_problem(), np.zeros((2, 3)))


def test_solver_rejects_non_finite_poses() -> None:
    bad = _initial()
    bad[0, 0] = np.inf
    with pytest.raises(nds.StagingError):
        nds.solve_nash_dramatic_staging(_problem(), bad)


def test_solver_rejects_negative_epsilon() -> None:
    with pytest.raises(nds.StagingError):
        nds.solve_nash_dramatic_staging(_problem(), _initial(), epsilon=-1.0)


def test_solver_rejects_bad_max_iterations() -> None:
    with pytest.raises(nds.StagingError):
        nds.solve_nash_dramatic_staging(_problem(), _initial(), max_iterations=0)


def test_solver_rejects_bad_learning_rate() -> None:
    with pytest.raises(nds.StagingError):
        nds.solve_nash_dramatic_staging(_problem(), _initial(), learning_rate=0.0)


def test_solver_rejects_bad_response_radius() -> None:
    with pytest.raises(nds.StagingError):
        nds.solve_nash_dramatic_staging(_problem(), _initial(), response_radius=-0.1)


def test_solver_rejects_too_few_response_samples() -> None:
    with pytest.raises(nds.StagingError):
        nds.solve_nash_dramatic_staging(_problem(), _initial(), response_samples=2)


def test_report_serialises() -> None:
    report = nds.solve_nash_dramatic_staging(
        _problem(), _initial(), epsilon=1e-2, max_iterations=40, response_samples=3
    )
    payload = report.to_dict()
    assert payload["kind"] == "atlas.nash_dramatic_staging"
    assert payload["is_nash"] == report.is_nash
    assert len(payload["poses"]) == 3  # type: ignore[arg-type]
