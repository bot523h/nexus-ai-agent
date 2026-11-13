"""Unit tests for Law 5 — se(3) object permanence."""

from __future__ import annotations

import numpy as np
import pytest

from nexus_ai_agent.creative.veritas.se3_object_permanence import (
    ObjectPermanenceError,
    ObjectState,
    audit_object_permanence,
    propagate_occluded_object,
    repair_object_permanence,
    se3_exp,
    se3_log,
)


def _state(pose: np.ndarray | None = None, extents: np.ndarray | None = None) -> ObjectState:
    return ObjectState(
        pose=np.eye(4) if pose is None else pose,
        twist=np.array([0.0, 0.0, 0.1, 1.0, 0.0, 0.0]),
        extents=np.array([1.0, 1.0, 1.0]) if extents is None else extents,
        mass=2.0,
    )


def test_se3_exp_log_roundtrip() -> None:
    twist = np.array([0.1, -0.2, 0.3, 1.0, 2.0, -1.0])
    transform = se3_exp(twist)
    assert np.allclose(se3_exp(se3_log(transform)), transform, atol=1e-9)


def test_se3_small_and_pi_rotations() -> None:
    small = se3_exp(np.array([1e-13, 0.0, 0.0, 0.0, 0.0, 0.0]))
    assert np.allclose(small, np.eye(4), atol=1e-12)
    rotation = np.diag([-1.0, -1.0, 1.0])
    transform = np.eye(4)
    transform[:3, :3] = rotation
    recovered = se3_exp(se3_log(transform))
    assert np.allclose(recovered[:3, :3], rotation, atol=1e-9)


def test_se3_validation() -> None:
    with pytest.raises(ObjectPermanenceError):
        se3_exp(np.zeros(5))
    with pytest.raises(ObjectPermanenceError):
        se3_log(np.eye(3))


def test_object_state_validation() -> None:
    with pytest.raises(ObjectPermanenceError):
        ObjectState(np.eye(3), np.zeros(6), np.ones(3), 1.0)
    with pytest.raises(ObjectPermanenceError):
        ObjectState(np.eye(4), np.zeros(5), np.ones(3), 1.0)
    with pytest.raises(ObjectPermanenceError):
        ObjectState(np.eye(4), np.zeros(6), np.ones(2), 1.0)
    with pytest.raises(ObjectPermanenceError):
        ObjectState(np.eye(4), np.zeros(6), np.ones(3), 0.0)
    with pytest.raises(ObjectPermanenceError):
        ObjectState(np.full((4, 4), np.nan), np.zeros(6), np.ones(3), 1.0)


def test_propagation_under_gravity() -> None:
    trajectory = propagate_occluded_object(_state(), 0.1, steps=3)
    assert len(trajectory) == 4
    assert trajectory[-1].pose[2, 3] < trajectory[0].pose[2, 3]
    assert np.allclose(trajectory[-1].extents, trajectory[0].extents)


def test_propagation_validation() -> None:
    with pytest.raises(ObjectPermanenceError):
        propagate_occluded_object(_state(), 0.0)
    with pytest.raises(ObjectPermanenceError):
        propagate_occluded_object(_state(), 0.1, steps=-1)
    with pytest.raises(ObjectPermanenceError):
        propagate_occluded_object(_state(), 0.1, gravity=(0.0, 0.0))


def test_teleport_is_detected_and_repaired() -> None:
    first = _state()
    pose = np.eye(4)
    pose[:3, 3] = np.array([10.0, 0.0, 0.0])
    second = _state(pose=pose)
    audit = audit_object_permanence((first, second), impulse_bound=0.5)
    assert "teleport" in audit.violations_before
    assert audit.violations_after == ()
    assert audit.max_impulse <= 0.5 + 1e-9


def test_volume_and_rotation_drift_are_repaired() -> None:
    first = _state()
    pose = np.eye(4)
    pose[:3, :3] *= 2.0
    pose[:3, 3] = np.array([0.1, 0.0, 0.0])
    second = _state(pose=pose, extents=np.array([2.0, 2.0, 2.0]))
    audit = audit_object_permanence((first, second))
    assert "volume_drift" in audit.violations_before
    assert "rotation_determinant" in audit.violations_before
    assert audit.violations_after == ()
    assert np.allclose(audit.repaired_states[1].extents, first.extents)
    assert np.linalg.det(audit.repaired_states[1].pose[:3, :3]) == pytest.approx(1.0)


def test_audit_validation() -> None:
    with pytest.raises(ObjectPermanenceError):
        audit_object_permanence(())
    with pytest.raises(ObjectPermanenceError):
        audit_object_permanence((_state(),), impulse_bound=0.0)
    with pytest.raises(ObjectPermanenceError):
        repair_object_permanence((), impulse_bound=0.5)
    with pytest.raises(ObjectPermanenceError):
        repair_object_permanence((_state(),), impulse_bound=0.0)
