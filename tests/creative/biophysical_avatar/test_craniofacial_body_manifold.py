from __future__ import annotations

import numpy as np
import pytest

from nexus_ai_agent.creative.biophysical_avatar.craniofacial_body_manifold import (
    FACIAL_ACTION_UNITS,
    JOINT_NAMES,
    PARENT_INDICES,
    BodyMorphology,
    CriticalDampedDynamics,
    FaceManifold,
    Skeleton3D,
    SkeletonPose,
    rigid_transform,
)
from nexus_ai_agent.creative.biophysical_avatar.entity_dna_seal import (
    ParametricAvatarCatalog,
    canonical_bone_lengths,
)


def test_face_manifold_has_64_dimensions_and_orthogonal_bases() -> None:
    manifold = FaceManifold.default()
    assert manifold.mean_shape.shape == (64, 3)
    assert manifold.structural_basis.shape == (192, 64)
    assert manifold.action_unit_basis.shape == (192, 46)
    assert len(FACIAL_ACTION_UNITS) == 46
    structural_gram = manifold.structural_basis.T @ manifold.structural_basis
    action_gram = manifold.action_unit_basis.T @ manifold.action_unit_basis
    cross_gram = manifold.structural_basis.T @ manifold.action_unit_basis
    assert np.allclose(structural_gram, np.eye(64) * 0.012**2, atol=1e-12)
    assert np.allclose(action_gram, np.eye(46) * 0.006**2, atol=1e-12)
    assert np.allclose(cross_gram, 0.0, atol=1e-12)
    assert not manifold.mean_shape.flags.writeable
    assert not manifold.structural_basis.flags.writeable
    with pytest.raises(ValueError):
        manifold.mean_shape[0, 0] = 99.0


def test_face_evaluation_is_stable_and_has_expected_coordinates() -> None:
    manifold = FaceManifold.default()
    seal = ParametricAvatarCatalog().profile(83)
    first = manifold.evaluate(seal.craniofacial, np.zeros(46))
    second = manifold.evaluate(seal.craniofacial, np.zeros(46))
    expressive = manifold.evaluate(seal.craniofacial, np.ones(46) * 0.5)
    assert first.shape == (64, 3)
    assert np.array_equal(first, second)
    assert np.isfinite(first).all()
    assert not np.array_equal(first, expressive)
    assert np.allclose(manifold.evaluate(np.zeros(64)), manifold.mean_shape)
    with pytest.raises(ValueError, match="shape"):
        manifold.evaluate((0.0,))
    with pytest.raises(ValueError, match="action_units"):
        manifold.evaluate(np.zeros(64), np.zeros(45))
    bad = np.zeros(64)
    bad[0] = float("nan")
    with pytest.raises(ValueError, match="finite"):
        manifold.evaluate(bad)
    with pytest.raises(ValueError, match="mean_shape"):
        FaceManifold(np.zeros((2, 3)), np.zeros((192, 64)), np.zeros((192, 46)))
    with pytest.raises(ValueError, match="structural_basis"):
        FaceManifold(np.zeros((64, 3)), np.zeros((2, 2)), np.zeros((192, 46)))
    with pytest.raises(ValueError, match="action_unit_basis"):
        FaceManifold(np.zeros((64, 3)), np.zeros((192, 64)), np.zeros((2, 2)))
    with pytest.raises(ValueError, match="numeric array"):
        FaceManifold(object(), np.zeros((192, 64)), np.zeros((192, 46)))  # type: ignore[arg-type]


def test_critically_damped_dynamics_converges_without_overshoot() -> None:
    dynamics = CriticalDampedDynamics.at_rest(3, omega0=8.0)
    target = np.asarray((1.0, -0.5, 0.25))
    positions = [dynamics.step(target, 0.01) for _ in range(360)]
    assert positions[-1] == pytest.approx(target, abs=1e-6)
    assert np.all(np.diff([point[0] for point in positions]) >= -1e-12)
    assert np.linalg.norm(dynamics.velocity) < 1e-5
    reset_position = np.asarray((0.2, 0.3, 0.4))
    dynamics.reset(reset_position, np.ones(3))
    assert np.array_equal(dynamics.position, reset_position)
    assert np.array_equal(dynamics.velocity, np.ones(3))
    assert CriticalDampedDynamics(np.zeros(1), np.zeros(1)).step((1.0,), 0.03)[0] > 0.0


def test_critical_solver_rejects_invalid_dimensions_and_parameters() -> None:
    with pytest.raises(ValueError, match="dimensions"):
        CriticalDampedDynamics.at_rest(0)
    with pytest.raises(ValueError, match="position"):
        CriticalDampedDynamics(np.zeros((2, 2)), np.zeros((2, 2)))
    with pytest.raises(ValueError, match="velocity"):
        CriticalDampedDynamics(np.zeros(2), np.zeros(3))
    with pytest.raises(ValueError, match="omega0"):
        CriticalDampedDynamics(np.zeros(1), np.zeros(1), omega0=0.0)
    with pytest.raises(ValueError, match="zeta"):
        CriticalDampedDynamics(np.zeros(1), np.zeros(1), zeta=0.8)
    dynamics = CriticalDampedDynamics.at_rest(2)
    with pytest.raises(ValueError, match="target"):
        dynamics.step((0.0,), 0.01)
    with pytest.raises(ValueError, match="dt"):
        dynamics.step((0.0, 0.0), 0.0)
    with pytest.raises(ValueError, match="velocity"):
        dynamics.reset((0.0, 0.0), (1.0,))


def test_body_morphology_maps_all_coefficients_to_semantic_readouts() -> None:
    seal = ParametricAvatarCatalog().profile(28)
    body = BodyMorphology(seal.morphology)
    assert len(body.bone_lengths) == 17
    assert body.stature_m > 1.0
    assert body.shoulder_width_m > 0.0
    assert 0.0 < body.muscle_mass_index < 1.0
    assert 0.0 < body.body_fat_index < 1.0
    assert abs(body.spine_curvature_m) <= 0.04
    assert abs(body.shoulder_slope_radians) <= 0.18
    assert 0.88 <= body.neck_scale <= 1.12
    assert 0.88 <= body.upper_arm_scale <= 1.12
    assert 0.88 <= body.forearm_scale <= 1.12
    assert 0.88 <= body.thigh_scale <= 1.12
    assert 0.88 <= body.shin_scale <= 1.12
    assert body.chest_width_m > 0.0
    assert body.waist_width_m > 0.0
    assert body.hip_width_m > 0.0
    assert abs(body.stance_angle_radians) <= 0.12
    assert abs(body.posture_tilt_radians) <= 0.10
    upper_arm_change = np.zeros(24)
    upper_arm_change[11] = 1.0
    assert (
        canonical_bone_lengths(tuple(upper_arm_change))[4] != canonical_bone_lengths((0.0,) * 24)[4]
    )
    with pytest.raises(ValueError, match="shape"):
        BodyMorphology((0.0,))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="numeric vector"):
        BodyMorphology("not a vector")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="contain numbers"):
        BodyMorphology(("bad",) + (0.0,) * 23)  # type: ignore[arg-type]


def test_se3_rigid_transform_is_orthonormal_and_rejects_reflections() -> None:
    angle = 0.45
    rotation = np.asarray(
        ((np.cos(angle), -np.sin(angle), 0.0), (np.sin(angle), np.cos(angle), 0.0), (0.0, 0.0, 1.0))
    )
    transform = rigid_transform(rotation, (1.0, 2.0, 3.0))
    assert transform.shape == (4, 4)
    assert np.allclose(transform[:3, 3], (1.0, 2.0, 3.0))
    assert np.allclose(transform[3], (0.0, 0.0, 0.0, 1.0))
    with pytest.raises(ValueError, match="orthonormal"):
        rigid_transform(np.zeros((3, 3)), (0.0, 0.0, 0.0))
    with pytest.raises(ValueError, match="finite 3x3"):
        rigid_transform(np.zeros((4, 4)), (0.0, 0.0, 0.0))
    nonfinite_rotation = np.eye(3)
    nonfinite_rotation[0, 0] = np.nan
    with pytest.raises(ValueError, match="finite 3x3"):
        rigid_transform(nonfinite_rotation, (0.0, 0.0, 0.0))
    with pytest.raises(ValueError, match="determinant"):
        rigid_transform(np.diag((1.0, 1.0, -1.0)), (0.0, 0.0, 0.0))
    with pytest.raises(ValueError, match="translation"):
        rigid_transform(np.eye(3), (0.0, 0.0))


def test_18_joint_kinematics_preserves_every_bone_under_rotations() -> None:
    seal = ParametricAvatarCatalog().profile(72)
    skeleton = Skeleton3D(seal.morphology)
    pose = skeleton.pose(root_translation=(1.0, -2.0, 0.95))
    assert len(JOINT_NAMES) == 18
    assert len(PARENT_INDICES) == 18
    assert pose.positions.shape == (18, 3)
    assert pose.transforms.shape == (18, 4, 4)
    assert np.allclose(pose.positions[0], (1.0, -2.0, 0.95))
    assert np.allclose(pose.bone_lengths, skeleton.bone_lengths, atol=1e-12)
    assert not pose.positions.flags.writeable

    rotations = np.repeat(np.eye(3)[None, :, :], 18, axis=0)
    angle = 0.7
    rotations[4] = np.asarray(
        ((np.cos(angle), 0.0, np.sin(angle)), (0.0, 1.0, 0.0), (-np.sin(angle), 0.0, np.cos(angle)))
    )
    animated = skeleton.pose(rotations)
    assert np.allclose(animated.bone_lengths, skeleton.bone_lengths, atol=1e-12)
    assert not np.allclose(animated.positions, skeleton.pose().positions)
    assert np.allclose(animated.transforms[:, 3, :], np.tile((0.0, 0.0, 0.0, 1.0), (18, 1)))


def test_skeleton_and_pose_reject_nonrigid_or_malformed_inputs() -> None:
    skeleton = Skeleton3D(np.zeros(24))
    with pytest.raises(ValueError, match="shape"):
        skeleton.pose(root_translation=(0.0, 0.0))
    with pytest.raises(ValueError, match="shape"):
        skeleton.pose(np.zeros((2, 3, 3)))
    with pytest.raises(ValueError, match="numeric"):
        skeleton.pose(("bad",))  # type: ignore[arg-type]
    bad_rotations = np.repeat(np.eye(3)[None, :, :], 18, axis=0)
    bad_rotations[4, 0, 0] = 2.0
    with pytest.raises(ValueError, match="orthonormal"):
        skeleton.pose(bad_rotations)
    reflections = np.repeat(np.eye(3)[None, :, :], 18, axis=0)
    reflections[1, 2, 2] = -1.0
    with pytest.raises(ValueError, match="determinant"):
        skeleton.pose(reflections)
    with pytest.raises(ValueError, match="positions"):
        SkeletonPose(np.zeros((1, 3)), np.zeros((18, 4, 4)), np.zeros(17))
    with pytest.raises(ValueError, match="transforms"):
        SkeletonPose(np.zeros((18, 3)), np.zeros((1, 4, 4)), np.zeros(17))
    with pytest.raises(ValueError, match="bone_lengths"):
        SkeletonPose(np.zeros((18, 3)), np.zeros((18, 4, 4)), np.zeros(16))
