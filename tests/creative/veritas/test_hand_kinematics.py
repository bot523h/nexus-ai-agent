"""Unit tests for Law 1 — hand kinematics and anatomical projection."""

from __future__ import annotations

import numpy as np
import pytest

from nexus_ai_agent.creative.veritas.hand_kinematics import (
    CANONICAL_JOINT_COUNT,
    CANONICAL_RAYS,
    HandSkeleton,
    HandTopologyError,
    audit_and_project_hand_skeleton,
    canonical_hand_skeleton,
    hand_violation_score,
    spectral_laplacian_signature,
)


def test_canonical_hand_is_clean() -> None:
    skeleton = canonical_hand_skeleton()
    assert skeleton.joint_count == CANONICAL_JOINT_COUNT
    assert skeleton.ray_count == 5
    assert skeleton.rays == CANONICAL_RAYS
    assert hand_violation_score(skeleton) == 0


def test_canonical_hand_rejects_bad_scale() -> None:
    with pytest.raises(HandTopologyError):
        canonical_hand_skeleton(0.0)
    with pytest.raises(HandTopologyError):
        canonical_hand_skeleton(float("nan"))


def test_spectral_signature_and_bad_ray() -> None:
    signature = spectral_laplacian_signature(5)
    assert signature.shape == (5,)
    assert signature[0] == pytest.approx(0.0)
    with pytest.raises(HandTopologyError):
        spectral_laplacian_signature(0)


def test_polydactyly_ray_is_pruned() -> None:
    base = canonical_hand_skeleton()
    joints = np.vstack([base.joints, base.joints[5:10] + np.array([0.4, 0.0, 0.0])])
    angles = np.vstack([base.joint_angles, np.zeros((5, 2), dtype=np.float64)])
    lengths = np.concatenate([base.bone_lengths, base.bone_lengths[5:10]])
    rays = base.rays + ((0, 21, 22, 23, 24),)
    skeleton = HandSkeleton(joints, rays, angles, lengths)
    assert hand_violation_score(skeleton) == 2
    result = audit_and_project_hand_skeleton(skeleton)
    assert result.pruned_rays == 1
    assert "polydactyly" in result.violations_before
    assert result.violations_after == ()
    assert result.skeleton.ray_count == 5
    assert result.skeleton.joint_count == 21


def test_truncated_hand_is_padded_to_five_rays() -> None:
    base = canonical_hand_skeleton()
    skeleton = HandSkeleton(
        joints=base.joints[:10],
        rays=base.rays[:2],
        joint_angles=base.joint_angles[:10],
        bone_lengths=base.bone_lengths[:10],
    )
    result = audit_and_project_hand_skeleton(skeleton)
    assert "topology" in result.violations_before
    assert result.skeleton.ray_count == 5
    assert result.violations_after == ()


def test_joint_limits_and_tendon_coupling_are_projected() -> None:
    base = canonical_hand_skeleton()
    angles = np.array(base.joint_angles, copy=True)
    # Index finger ray is (0, 5, 6, 7, 8): MCP=5, PIP=6, DIP=7.
    angles[5, 0] = -45.0  # MCP flexion below its -15 deg floor
    angles[5, 1] = 60.0  # MCP abduction beyond its 25 deg cone
    angles[6, 0] = 130.0  # PIP flexion beyond its 110 deg ceiling
    angles[7, 0] = 200.0  # DIP flexion beyond its 80 deg ceiling
    skeleton = HandSkeleton(base.joints, base.rays, angles, base.bone_lengths)
    result = audit_and_project_hand_skeleton(skeleton)
    assert "joint_limits" in result.violations_before
    assert "tendon_coupling" in result.violations_before
    assert result.violations_after == ()
    assert result.skeleton.joint_angles[5, 0] == pytest.approx(-15.0)
    assert result.skeleton.joint_angles[5, 1] == pytest.approx(25.0)
    assert result.skeleton.joint_angles[6, 0] == pytest.approx(110.0)
    assert result.skeleton.joint_angles[7, 0] == pytest.approx(2.0 / 3.0 * 110.0)


def test_bone_ratio_is_projected() -> None:
    base = canonical_hand_skeleton()
    lengths = np.array(base.bone_lengths, copy=True)
    lengths[8] *= 3.0
    skeleton = HandSkeleton(base.joints, base.rays, base.joint_angles, lengths)
    result = audit_and_project_hand_skeleton(skeleton)
    assert "bone_ratio" in result.violations_before
    assert result.violations_after == ()


def test_grip_sdf_closure() -> None:
    base = canonical_hand_skeleton()

    def sdf(points: np.ndarray) -> np.ndarray:
        return np.linalg.norm(points - np.array([0.0, 0.7, 0.0]), axis=-1) - 0.15

    result = audit_and_project_hand_skeleton(base, target_sdf=sdf, grip_active=True)
    assert "grip_penetration" in result.violations_before
    assert result.violations_after == ()
    tips = np.array([result.skeleton.joints[ray[-1]] for ray in result.skeleton.rays])
    assert float(np.max(np.abs(sdf(tips)))) < 1e-6


def test_grip_without_sdf_is_a_noop() -> None:
    base = canonical_hand_skeleton()
    result = audit_and_project_hand_skeleton(base, grip_active=True)
    assert result.violations_after == ()


def test_invalid_skeleton_shapes_raise() -> None:
    base = canonical_hand_skeleton()
    with pytest.raises(HandTopologyError):
        audit_and_project_hand_skeleton(
            HandSkeleton(np.zeros((21, 2)), base.rays, base.joint_angles, base.bone_lengths)
        )
    with pytest.raises(HandTopologyError):
        audit_and_project_hand_skeleton(
            HandSkeleton(base.joints, base.rays, np.zeros((21, 3)), base.bone_lengths)
        )
    with pytest.raises(HandTopologyError):
        audit_and_project_hand_skeleton(
            HandSkeleton(base.joints, base.rays, base.joint_angles, np.zeros((20,)))
        )
    bad_joints = np.array(base.joints, copy=True)
    bad_joints[0, 0] = np.nan
    with pytest.raises(HandTopologyError):
        audit_and_project_hand_skeleton(
            HandSkeleton(bad_joints, base.rays, base.joint_angles, base.bone_lengths)
        )
    with pytest.raises(HandTopologyError):
        audit_and_project_hand_skeleton(base, tendon_epsilon=0.0)
