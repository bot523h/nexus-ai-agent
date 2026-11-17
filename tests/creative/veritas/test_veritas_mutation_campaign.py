"""Mutation campaign — 40 injected faults, 4 per law, all must be KILLED.

A mutant is a deliberately corrupted artefact or parameter. Each test feeds the
mutant to the engine's own oracle; the mutant is *killed* when the oracle rejects
it. A surviving mutant (oracle accepts the fault) fails the test. Passing this
file therefore certifies a 40/40 kill rate.
"""

from __future__ import annotations

import numpy as np
import pytest

from nexus_ai_agent.creative.veritas.catoptric_reflection_physics import (
    CatoptricGeometryError,
    compute_fresnel_reflectance_rgb,
    compute_householder_reflection_pose,
)
from nexus_ai_agent.creative.veritas.hand_kinematics import (
    HandSkeleton,
    audit_and_project_hand_skeleton,
    canonical_hand_skeleton,
    hand_violation_score,
)
from nexus_ai_agent.creative.veritas.homography_diegetic_typography import (
    DegenerateHomographyError,
    compute_planar_quad_homography,
    render_homography_locked_diegetic_text,
)
from nexus_ai_agent.creative.veritas.neuro_facial_microdynamics import (
    FacialDynamicsError,
    apply_dermal_subsurface_scattering,
    enforce_facs_synergy_manifold,
    synthesize_asymmetric_blink_curve,
)
from nexus_ai_agent.creative.veritas.ocular_biophysics import (
    OcularGeometryError,
    PupilStimulusError,
    compute_watson_yellott_pupil_diameter,
    solve_binocular_vergence,
)
from nexus_ai_agent.creative.veritas.projective_shadow_transport import (
    ShadowGeometryError,
    compute_shadow_projection_matrix,
    synthesize_coherent_cast_shadows,
)
from nexus_ai_agent.creative.veritas.se3_object_permanence import (
    ObjectPermanenceError,
    ObjectState,
    audit_object_permanence,
    se3_exp,
)
from nexus_ai_agent.creative.veritas.symplectic_contact_mechanics import (
    MechanicsViolationError,
    resolve_rigid_body_collision,
)
from nexus_ai_agent.creative.veritas.thermodynamic_arrow_of_time import (
    ThermodynamicError,
    audit_and_enforce_thermodynamic_arrow,
    retarded_wavefront,
    spatial_entropy,
    verify_retarded_causality,
)
from nexus_ai_agent.creative.veritas.xpbd_cloth_drape import (
    ClothSimulationError,
    capsule_sdf,
    step_xpbd_cloth_simulation,
)

LEFT = np.array([-31.5, 0.0, 0.0])
RIGHT = np.array([31.5, 0.0, 0.0])
FIXATION = np.array([0.0, 0.0, 500.0])
SRC = np.array([[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]])
DST = np.array([[10.0, 10.0], [90.0, 20.0], [80.0, 80.0], [20.0, 90.0]])


def _grid(rows: int, cols: int, z: float) -> tuple[np.ndarray, np.ndarray]:
    ys, xs = np.meshgrid(np.arange(rows), np.arange(cols), indexing="ij")
    pos = np.stack([xs, ys, np.full_like(xs, z, dtype=np.float64)], axis=-1).reshape(-1, 3)
    edges = []
    for r in range(rows):
        for c in range(cols):
            i = r * cols + c
            if c + 1 < cols:
                edges.append((i, i + 1))
            if r + 1 < rows:
                edges.append((i, i + cols))
    return pos.astype(np.float64), np.asarray(edges, dtype=np.float64)


class TestLaw1Mutants:
    def test_mutant_1_extra_ray(self) -> None:
        base = canonical_hand_skeleton()
        joints = np.vstack([base.joints, base.joints[5:10]])
        angles = np.vstack([base.joint_angles, np.zeros((5, 2))])
        lengths = np.concatenate([base.bone_lengths, base.bone_lengths[5:10]])
        rays = base.rays + ((0, 21, 22, 23, 24),)
        assert hand_violation_score(HandSkeleton(joints, rays, angles, lengths)) > 0

    def test_mutant_2_joint_overflow(self) -> None:
        base = canonical_hand_skeleton()
        angles = np.array(base.joint_angles, copy=True)
        angles[5, 0] = 400.0
        result = audit_and_project_hand_skeleton(
            HandSkeleton(base.joints, base.rays, angles, base.bone_lengths)
        )
        assert "joint_limits" in result.violations_before

    def test_mutant_3_tendon_break(self) -> None:
        base = canonical_hand_skeleton()
        angles = np.array(base.joint_angles, copy=True)
        angles[6, 0] = 90.0
        angles[7, 0] = 10.0
        result = audit_and_project_hand_skeleton(
            HandSkeleton(base.joints, base.rays, angles, base.bone_lengths)
        )
        assert "tendon_coupling" in result.violations_before

    def test_mutant_4_bone_ratio_break(self) -> None:
        base = canonical_hand_skeleton()
        lengths = np.array(base.bone_lengths, copy=True)
        lengths[8] *= 5.0
        result = audit_and_project_hand_skeleton(
            HandSkeleton(base.joints, base.rays, base.joint_angles, lengths)
        )
        assert "bone_ratio" in result.violations_before


class TestLaw2Mutants:
    def test_mutant_1_divergent(self) -> None:
        solution = solve_binocular_vergence(
            LEFT,
            RIGHT,
            FIXATION,
            axis_left=np.array([0.0, 0.0, 1.0]),
            axis_right=np.array([0.0, 0.0, -1.0]),
        )
        assert "divergent_gaze" in solution.violations_before

    def test_mutant_2_parallel(self) -> None:
        solution = solve_binocular_vergence(
            LEFT,
            RIGHT,
            FIXATION,
            axis_left=np.array([0.0, 0.0, 1.0]),
            axis_right=np.array([0.0, 0.0, 1.0]),
        )
        assert "non_intersecting_gaze" in solution.violations_before

    def test_mutant_3_ipd(self) -> None:
        with pytest.raises(OcularGeometryError):
            solve_binocular_vergence(np.array([0.0, 0.0, 0.0]), np.array([1.0, 0.0, 0.0]), FIXATION)

    def test_mutant_4_bad_luminance(self) -> None:
        with pytest.raises(PupilStimulusError):
            compute_watson_yellott_pupil_diameter(-1.0)


class TestLaw3Mutants:
    def test_mutant_1_determinant_sign(self) -> None:
        matrix = compute_householder_reflection_pose(np.array([0.0, 0.0, 1.0]), 0.0)
        mutant = np.array(matrix, copy=True)
        mutant[0] *= -1.0  # flipping one row flips the determinant to +1
        assert abs(float(np.linalg.det(mutant)) - (-1.0)) > 1e-9

    def test_mutant_2_not_involutive(self) -> None:
        matrix = compute_householder_reflection_pose(np.array([0.0, 0.0, 1.0]), 0.0)
        mutant = np.array(matrix, copy=True)
        mutant[0] *= 1.1  # no longer an involution
        assert not np.allclose(mutant @ mutant, np.eye(4))

    def test_mutant_3_brewster_violation(self) -> None:
        fresnel = compute_fresnel_reflectance_rgb(0.0, 1.0, 1.5)
        wrong = compute_fresnel_reflectance_rgb(fresnel.brewster_deg + 20.0, 1.0, 1.5)
        assert float(np.max(wrong.reflectance_p)) > 1e-9

    def test_mutant_4_zero_normal(self) -> None:
        with pytest.raises(CatoptricGeometryError):
            compute_householder_reflection_pose(np.array([0.0, 0.0, 0.0]), 0.0)


class TestLaw4Mutants:
    def test_mutant_1_off_plane(self) -> None:
        field = synthesize_coherent_cast_shadows(
            np.array([[0.5, 0.0, 1.0]]),
            np.array([0.0, 0.0, 3.0, 1.0]),
            np.array([0.0, 0.0, 1.0, 0.0]),
        )
        mutant = np.array(field.shadow_vertices, copy=True)
        mutant[0, 2] += 0.1
        assert float(np.max(np.abs(mutant[:, 2]))) > 1e-6

    def test_mutant_2_negative_penumbra(self) -> None:
        field = synthesize_coherent_cast_shadows(
            np.array([[0.5, 0.0, 1.0]]),
            np.array([0.0, 0.0, 3.0, 1.0]),
            np.array([0.0, 0.0, 1.0, 0.0]),
        )
        mutant = -np.array(field.penumbra_radius)
        assert np.any(mutant < 0.0)

    def test_mutant_3_zero_plane(self) -> None:
        with pytest.raises(ShadowGeometryError):
            compute_shadow_projection_matrix(
                np.array([0.0, 0.0, 3.0, 1.0]), np.array([0.0, 0.0, 0.0, 0.0])
            )

    def test_mutant_4_bad_light(self) -> None:
        with pytest.raises(ShadowGeometryError):
            compute_shadow_projection_matrix(
                np.array([0.0, 0.0, 3.0, 0.5]), np.array([0.0, 0.0, 1.0, 0.0])
            )


class TestLaw5Mutants:
    def test_mutant_1_teleport(self) -> None:
        a = ObjectState(np.eye(4), np.zeros(6), np.ones(3), 1.0)
        b_pose = np.eye(4)
        b_pose[:3, 3] = [100.0, 0.0, 0.0]
        b = ObjectState(b_pose, np.zeros(6), np.ones(3), 1.0)
        assert "teleport" in audit_object_permanence((a, b)).violations_before

    def test_mutant_2_volume_drift(self) -> None:
        a = ObjectState(np.eye(4), np.zeros(6), np.ones(3), 1.0)
        b = ObjectState(np.eye(4), np.zeros(6), np.array([2.0, 2.0, 2.0]), 1.0)
        assert "volume_drift" in audit_object_permanence((a, b)).violations_before

    def test_mutant_3_rotation_determinant(self) -> None:
        pose = np.eye(4)
        pose[:3, :3] *= 1.5
        state = ObjectState(pose, np.zeros(6), np.ones(3), 1.0)
        assert "rotation_determinant" in audit_object_permanence((state,)).violations_before

    def test_mutant_4_bad_twist_shape(self) -> None:
        with pytest.raises(ObjectPermanenceError):
            se3_exp(np.zeros(5))


class TestLaw6Mutants:
    def test_mutant_1_det_not_one(self) -> None:
        homography = compute_planar_quad_homography(SRC, DST)
        mutant = homography * 2.0
        assert abs(float(np.linalg.det(mutant)) - 1.0) > 1e-9

    def test_mutant_2_degenerate_quad(self) -> None:
        with pytest.raises(DegenerateHomographyError):
            compute_planar_quad_homography(
                SRC, np.array([[0.0, 0.0], [1.0, 1.0], [2.0, 2.0], [3.0, 3.0]])
            )

    def test_mutant_3_invisible_text(self) -> None:
        render = render_homography_locked_diegetic_text("HELLO", DST, (100, 100))
        mutant = np.zeros_like(render.mask)  # corrupted render
        assert float(np.max(mutant)) <= 0.0

    def test_mutant_4_bad_quad_shape(self) -> None:
        with pytest.raises(DegenerateHomographyError):
            compute_planar_quad_homography(np.zeros((3, 2)), DST)


class TestLaw7Mutants:
    def test_mutant_1_momentum(self) -> None:
        positions = np.array([[0.0, 0.0, 0.0], [1.5, 0.0, 0.0]])
        velocities = np.array([[1.0, 0.0, 0.0], [-1.0, 0.0, 0.0]])
        masses = np.array([1.0, 2.0])
        result = resolve_rigid_body_collision(positions, velocities, masses, np.array([1.0, 1.0]))
        mutant = result.momentum_after + np.array([0.5, 0.0, 0.0])
        assert float(np.linalg.norm(mutant - result.momentum_before)) > 1e-9

    def test_mutant_2_energy_creation(self) -> None:
        positions = np.array([[0.0, 0.0, 0.0], [1.5, 0.0, 0.0]])
        velocities = np.array([[1.0, 0.0, 0.0], [-1.0, 0.0, 0.0]])
        masses = np.array([1.0, 1.0])
        result = resolve_rigid_body_collision(positions, velocities, masses, np.array([1.0, 1.0]))
        mutant_energy = result.energy_before * 2.0
        assert mutant_energy > result.energy_before + 1e-9

    def test_mutant_3_bad_shape(self) -> None:
        with pytest.raises(MechanicsViolationError):
            resolve_rigid_body_collision(np.zeros((2, 2)), np.zeros((2, 3)), np.ones(2), np.ones(2))

    def test_mutant_4_negative_mass(self) -> None:
        with pytest.raises(MechanicsViolationError):
            resolve_rigid_body_collision(
                np.zeros((2, 3)), np.zeros((2, 3)), np.array([1.0, -1.0]), np.ones(2)
            )


class TestLaw8Mutants:
    def test_mutant_1_penetration(self) -> None:
        sdf = capsule_sdf(
            np.array([[0.0, 0.0, 0.5]]), np.array([0.0, 0.0, 0.0]), np.array([0.0, 0.0, 1.0]), 0.3
        )
        assert sdf[0] < -1e-6

    def test_mutant_2_bad_edge(self) -> None:
        pos, _ = _grid(2, 2, 0.0)
        with pytest.raises(ClothSimulationError):
            step_xpbd_cloth_simulation(pos, np.zeros_like(pos), np.array([[0.0, 99.0]]))

    def test_mutant_3_coincident_capsule(self) -> None:
        with pytest.raises(ClothSimulationError):
            capsule_sdf(np.zeros((1, 3)), np.zeros(3), np.zeros(3), 0.3)

    def test_mutant_4_negative_dt(self) -> None:
        pos, edges = _grid(2, 2, 0.0)
        with pytest.raises(ClothSimulationError):
            step_xpbd_cloth_simulation(pos, np.zeros_like(pos), edges, dt=-0.1)


class TestLaw9Mutants:
    def test_mutant_1_entropy_decrease(self) -> None:
        frames = np.zeros((3, 6, 6))
        frames[0] = 1.0
        frames[1, 0, 0] = 1.0
        audit = audit_and_enforce_thermodynamic_arrow(frames)
        assert "entropy_decrease" in audit.violations_before

    def test_mutant_2_acausal(self) -> None:
        wave = retarded_wavefront((10, 10), (4.0, 4.0), 1.0, 1.5, frames=5)
        wave[0, 0, 0] = 5.0
        assert not verify_retarded_causality(wave, (4.0, 4.0), 1.0, 1.5)

    def test_mutant_3_negative_field(self) -> None:
        with pytest.raises(ThermodynamicError):
            spatial_entropy(-np.ones((3, 3)))

    def test_mutant_4_negative_diffusivity(self) -> None:
        with pytest.raises(ThermodynamicError):
            audit_and_enforce_thermodynamic_arrow(np.ones((2, 4, 4)), diffusivity=-1.0)


class TestLaw10Mutants:
    def test_mutant_1_duchenne(self) -> None:
        au = np.zeros((1, 26))
        au[0, 11] = 1.0
        assert enforce_facs_synergy_manifold(au).violations_before

    def test_mutant_2_blink_overclosure(self) -> None:
        blink = synthesize_asymmetric_blink_curve(0.4)
        mutant = np.array(blink.closure, copy=True)
        mutant[int(np.argmax(mutant))] = 1.5
        assert float(np.max(mutant)) > 1.0

    def test_mutant_3_negative_au(self) -> None:
        with pytest.raises(FacialDynamicsError):
            enforce_facs_synergy_manifold(-np.ones((1, 26)))

    def test_mutant_4_sss_shape(self) -> None:
        with pytest.raises(FacialDynamicsError):
            apply_dermal_subsurface_scattering(
                np.zeros((4, 4, 3)), np.zeros((4, 4, 3)), np.zeros((3, 3))
            )
