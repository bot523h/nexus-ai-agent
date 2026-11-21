"""Antipodal Lie maps, fail-closed numerics and variational calculus."""

import math

import numpy as np
import pytest

from nexus_ai_agent.creative.storyboard_compiler import cinematography_solver as solver


@pytest.mark.parametrize("angle", [0.0, 1e-10, 1e-5, math.pi - 1e-5, math.pi])
@pytest.mark.parametrize("axis", [(1, 0, 0), (1, -2, 3), (-1, -1, -1)])
def test_lie_roundtrips(angle, axis):
    unit = np.array(axis, dtype=float)
    unit /= np.linalg.norm(unit)
    rotation = solver.so3_exp(unit * angle)
    assert np.linalg.norm(solver.so3_exp(solver.so3_log(rotation)) - rotation) < 1e-6
    transform = np.eye(4)
    transform[:3, :3] = rotation
    transform[:3, 3] = [2.0, -3.0, 0.5]
    assert np.linalg.norm(solver.se3_exp(solver.se3_log(transform)) - transform) < 1e-6


@pytest.mark.parametrize("bad", [True, np.bool_(False), float("nan"), float("inf"), -float("inf")])
def test_scalar_guards(bad):
    with pytest.raises((TypeError, ValueError)):
        solver.minimum_jerk_scalar(bad)
    with pytest.raises((TypeError, ValueError)):
        solver.so3_exp([bad, 0.0, 0.0])
    with pytest.raises((TypeError, ValueError)):
        solver.se3_exp([0.0, 0.0, 0.0, bad, 0.0, 0.0])


@pytest.mark.parametrize("bad", [np.diag([2.0, 1.0, 0.5]), np.diag([1.0, 1.0, -1.0]), np.eye(4)])
def test_invalid_manifold(bad):
    with pytest.raises(ValueError, match="Matrix is not a valid SO\\(3\\) rotation"):
        solver.so3_log(bad)


@pytest.mark.parametrize("tau", [-0.01, 1.01])
def test_tau_interval(tau):
    with pytest.raises(ValueError):
        solver.minimum_jerk_scalar(tau)


@pytest.mark.parametrize("steps", [100, 999, 1000, 2000])
def test_minimum_jerk_calculus(steps):
    result = solver.verify_minimum_jerk_calculus_invariants(steps)
    assert result["verified"]
    assert result["action"] == pytest.approx(720.0, abs=1e-3)
    assert result["velocity_integral"] == pytest.approx(1.0, abs=1e-6)
    assert result["perturbed_action"] > result["action"]


@pytest.mark.parametrize("dimension", [0.0, -1.0, True, float("nan")])
def test_sensor_guards(dimension):
    pose = solver.CameraPoseSE3.from_rt(np.eye(3), [0, 0, 0])
    with pytest.raises((TypeError, ValueError)):
        solver.project_world_point(pose, [0, 0, 2], 50, sensor_width_mm=dimension)


def test_nested_boolean_rotation_rejected():
    with pytest.raises(TypeError):
        solver.so3_log([[True, 0, 0], [0, 1, 0], [0, 0, 1]])


@pytest.mark.parametrize("angle", [0.0, 1e-8, 0.7, math.pi - 1e-7, math.pi])
def test_veritas_cross_module_equivalence(angle):
    from nexus_ai_agent.creative.veritas import se3_object_permanence as veritas

    axis = np.array([-1.0, 2.0, -3.0])
    omega = axis / np.linalg.norm(axis) * angle
    velocity = np.array([0.4, -2.0, 5.0])
    cinema_twist = np.concatenate((velocity, omega))
    veritas_twist = np.concatenate((omega, velocity))
    transform = solver.se3_exp(cinema_twist)
    np.testing.assert_allclose(veritas.se3_exp(veritas_twist), transform, atol=1e-13)
    recovered = veritas.se3_log(transform)
    np.testing.assert_allclose(veritas.se3_exp(recovered), transform, atol=1e-13)
    cinema_recovered = solver.se3_log(transform)
    np.testing.assert_allclose(
        recovered, np.concatenate((cinema_recovered[3:], cinema_recovered[:3])), atol=1e-13
    )


@pytest.mark.parametrize(
    "matrix",
    [
        np.diag([2.0, 1.0, 0.5, 1.0]),
        np.diag([1.0, 1.0, -1.0, 1.0]),
        np.full((4, 4), np.nan),
        np.ones((4, 4)),
        np.eye(4, dtype=bool),
    ],
)
def test_veritas_rejects_invalid_se3(matrix):
    from nexus_ai_agent.creative.veritas import se3_object_permanence as veritas

    with pytest.raises((ValueError, TypeError)):
        veritas.se3_log(matrix)


def test_direct_pose_strict_validation_enforces_so3():
    with pytest.raises(ValueError, match="orthogonality"):
        solver.CameraPoseSE3(
            rotation=((2.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 0.5)),
            translation=(0.0, 0.0, 0.0),
        ).validate_strict()


def test_strict_trace_factory_rejects_raw_audit_nan():
    values = dict(
        adjusted_position=(0.0, 0.0, 0.0),
        original_position=(float("nan"), 0.0, 0.0),
        original_axis_side=1,
        enforced_axis_side=1,
        original_signed_distance_m=1.0,
        enforced_signed_distance_m=1.0,
        was_axis_reflected=False,
    )
    raw = solver.AxisEnforcementTrace(**values)
    with pytest.raises(ValueError):
        raw.validate_strict()
    with pytest.raises(ValueError):
        solver.AxisEnforcementTrace.from_validated(**values)


def test_independent_calculus_detects_inconsistent_derivative(monkeypatch):
    original = solver.minimum_jerk_scalar

    def altered(tau):
        s, v, a, j = original(tau)
        return s, v, a + 0.1, j

    monkeypatch.setattr(solver, "minimum_jerk_scalar", altered)
    result = solver.verify_minimum_jerk_calculus_invariants()
    assert result["derivative_2_max_error"] > 0.09
    assert not result["verified"]


def test_strict_geometry_factory_boundaries():
    from dataclasses import fields, replace

    from nexus_ai_agent.creative.storyboard_compiler.bilingual_script_parser import (
        parse_bilingual_script,
    )

    scene = parse_bilingual_script("INT. ROOM - NIGHT\nREZA\nOne.\n\nSARA\nTwo.").scenes[0]
    shot = solver.solve_scene_cinematography(scene).shots[0]
    payload = {f.name: getattr(shot, f.name) for f in fields(shot)}
    assert solver.CameraShotSpec.from_validated(**payload) == shot
    assert shot.start_axis_trace.validate_strict() is shot.start_axis_trace
    trace = shot.start_axis_trace
    trace_payload = {f.name: getattr(trace, f.name) for f in fields(trace)}
    assert solver.AxisEnforcementTrace.from_validated(**trace_payload) == trace
    with pytest.raises(ValueError, match="duration_sec"):
        replace(shot, duration_sec=0.0).validate_strict()
    for name in ("focal_length_mm", "f_stop", "focus_distance_m", "hyperfocal_distance_m"):
        with pytest.raises(ValueError, match=name):
            replace(shot.lens, **{name: 0.0})
    with pytest.raises(ValueError, match="duration_sec"):
        replace(shot.trajectory, duration_sec=0.0)
    for shape in ((4, 4), (3,)):
        with pytest.raises(ValueError, match="shape"):
            solver.CameraPoseSE3(np.ones(shape), (0.0, 0.0, 0.0))
    assert shot.pose.validate_strict() is shot.pose


@pytest.mark.parametrize("steps", [True, "1000", 99])
def test_calculus_steps_guard(steps):
    with pytest.raises((TypeError, ValueError)):
        solver.verify_minimum_jerk_calculus_invariants(steps)


def test_lie_shape_and_tolerance_and_sensor_boundaries():
    for tolerance in (0.0, -1.0, 1.0):
        with pytest.raises(ValueError, match="tol"):
            solver.so3_log(np.eye(3), tol=tolerance)
    with pytest.raises(ValueError, match="twist"):
        solver.se3_exp(np.ones(5))
    for transform in (np.eye(3), np.ones((4, 4))):
        with pytest.raises(ValueError, match="transform"):
            solver.se3_log(transform)
    with pytest.raises(ValueError, match="eps"):
        solver.axis_side_sign((0.0, 0.0, 1.0), (0.0, 0.0, 0.0), (1.0, 0.0, 0.0), eps=-1.0)
    with pytest.raises(ValueError, match="sensor"):
        solver.build_rule_of_thirds_pose(
            (0.0, -2.0, 1.0),
            (0.0, 0.0, 1.0),
            focal_length_mm=50.0,
            target_uv=(1 / 3, 1 / 3),
            sensor_width_mm=0.0,
        )
    from nexus_ai_agent.creative.veritas import se3_object_permanence as veritas

    np.testing.assert_array_equal(veritas._so3_log(np.eye(3)), np.zeros(3))
