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
