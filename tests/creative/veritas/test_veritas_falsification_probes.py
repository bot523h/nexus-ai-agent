"""Popperian falsification probes for the ten VERITAS laws (6 probes/law).

Each probe states a falsifiable physical claim. A probe *passes* when the engine
upholds the claim; it would *fail* if the corresponding invariant were violated.
Together the 60 probes form the adversarial oracle that the mutation campaign
must survive.
"""

from __future__ import annotations

import numpy as np
import pytest

from nexus_ai_agent.creative.veritas.catoptric_reflection_physics import (
    compute_fresnel_reflectance_rgb,
    compute_householder_reflection_pose,
    verify_reflection_epipolar_consistency,
)
from nexus_ai_agent.creative.veritas.hand_kinematics import (
    BONE_RATIOS,
    CANONICAL_JOINT_COUNT,
    HandSkeleton,
    audit_and_project_hand_skeleton,
    canonical_hand_skeleton,
)
from nexus_ai_agent.creative.veritas.homography_diegetic_typography import (
    DegenerateHomographyError,
    compute_planar_quad_homography,
    procedural_glyph_bitmap,
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
    compute_watson_yellott_pupil_diameter,
    solve_binocular_vergence,
    synthesize_saccade_vor_trajectory,
)
from nexus_ai_agent.creative.veritas.projective_shadow_transport import (
    ShadowGeometryError,
    compute_shadow_projection_matrix,
    penumbra_radius,
    synthesize_coherent_cast_shadows,
)
from nexus_ai_agent.creative.veritas.se3_object_permanence import (
    ObjectState,
    audit_object_permanence,
    propagate_occluded_object,
    se3_exp,
    se3_log,
)
from nexus_ai_agent.creative.veritas.symplectic_contact_mechanics import (
    MechanicsViolationError,
    resolve_rigid_body_collision,
    stormer_verlet_step,
    total_linear_momentum,
)
from nexus_ai_agent.creative.veritas.thermodynamic_arrow_of_time import (
    audit_and_enforce_thermodynamic_arrow,
    fourier_advection_diffusion,
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


class TestLaw1Hand:
    def test_probe_1_canonical_topology(self) -> None:
        skeleton = canonical_hand_skeleton()
        assert skeleton.joint_count == CANONICAL_JOINT_COUNT
        assert skeleton.ray_count == 5

    def test_probe_2_polydactyly_pruned(self) -> None:
        base = canonical_hand_skeleton()
        joints = np.vstack([base.joints, base.joints[5:10] + np.array([0.4, 0.0, 0.0])])
        angles = np.vstack([base.joint_angles, np.zeros((5, 2))])
        lengths = np.concatenate([base.bone_lengths, base.bone_lengths[5:10]])
        rays = base.rays + ((0, 21, 22, 23, 24),)
        result = audit_and_project_hand_skeleton(HandSkeleton(joints, rays, angles, lengths))
        assert result.pruned_rays == 1
        assert result.skeleton.ray_count == 5

    def test_probe_3_joint_cone_clamped(self) -> None:
        base = canonical_hand_skeleton()
        angles = np.array(base.joint_angles, copy=True)
        angles[5, 0] = 500.0
        result = audit_and_project_hand_skeleton(
            HandSkeleton(base.joints, base.rays, angles, base.bone_lengths)
        )
        assert result.skeleton.joint_angles[5, 0] <= 90.0

    def test_probe_4_tendon_coupling(self) -> None:
        base = canonical_hand_skeleton()
        angles = np.array(base.joint_angles, copy=True)
        angles[6, 0] = 90.0
        angles[7, 0] = 0.0
        result = audit_and_project_hand_skeleton(
            HandSkeleton(base.joints, base.rays, angles, base.bone_lengths)
        )
        assert result.skeleton.joint_angles[7, 0] == pytest.approx(2.0 / 3.0 * 90.0)

    def test_probe_5_bone_ratios(self) -> None:
        base = canonical_hand_skeleton()
        lengths = np.array(base.bone_lengths, copy=True)
        lengths[8] *= 4.0
        result = audit_and_project_hand_skeleton(
            HandSkeleton(base.joints, base.rays, base.joint_angles, lengths)
        )
        ray = result.skeleton.rays[1]
        segment = result.skeleton.bone_lengths[list(ray[2:5])]
        observed = segment / segment.sum()
        assert np.allclose(observed, np.array(BONE_RATIOS[1]), atol=1e-6)

    def test_probe_6_grip_sdf_closure(self) -> None:
        base = canonical_hand_skeleton()

        def sdf(points: np.ndarray) -> np.ndarray:
            return np.linalg.norm(points - np.array([0.0, 0.7, 0.0]), axis=-1) - 0.15

        result = audit_and_project_hand_skeleton(base, target_sdf=sdf, grip_active=True)
        tips = np.array([result.skeleton.joints[ray[-1]] for ray in result.skeleton.rays])
        assert float(np.max(np.abs(sdf(tips)))) < 1e-6


class TestLaw2Eyes:
    def test_probe_1_exact_vergence(self) -> None:
        solution = solve_binocular_vergence(LEFT, RIGHT, FIXATION)
        assert solution.violations_before == ()
        assert 0.0 < solution.vergence_deg < 90.0
        assert solution.skew_mm < 0.5

    def test_probe_2_divergent_gaze_flagged(self) -> None:
        solution = solve_binocular_vergence(
            LEFT,
            RIGHT,
            FIXATION,
            axis_left=np.array([0.0, 0.0, 1.0]),
            axis_right=np.array([0.0, 0.0, -1.0]),
        )
        assert "divergent_gaze" in solution.violations_before

    def test_probe_3_non_intersecting_healed(self) -> None:
        solution = solve_binocular_vergence(
            LEFT,
            RIGHT,
            FIXATION,
            axis_left=np.array([0.0, 0.0, 1.0]),
            axis_right=np.array([0.0, 0.0, 1.0]),
        )
        assert "non_intersecting_gaze" in solution.violations_before
        assert solution.skew_mm < 0.5

    def test_probe_4_ipd_bounds(self) -> None:
        with pytest.raises(OcularGeometryError):
            solve_binocular_vergence(np.array([0.0, 0.0, 0.0]), np.array([1.0, 0.0, 0.0]), FIXATION)

    def test_probe_5_saccade_main_sequence(self) -> None:
        small = synthesize_saccade_vor_trajectory(10.0, 0.1)
        large = synthesize_saccade_vor_trajectory(40.0, 0.1)
        assert 0.0 < small.peak_velocity_deg_s < large.peak_velocity_deg_s

    def test_probe_6_pupil_constriction(self) -> None:
        response = compute_watson_yellott_pupil_diameter(0.9, 20.0, 30.0)
        assert 2.0 <= response.steady_state_mm <= 8.0
        assert response.constriction_mm > 0.0


class TestLaw3Catoptric:
    def test_probe_1_reflection_determinant(self) -> None:
        matrix = compute_householder_reflection_pose(np.array([0.0, 0.0, 1.0]), 0.0)
        assert np.linalg.det(matrix) == pytest.approx(-1.0)

    def test_probe_2_involution(self) -> None:
        matrix = compute_householder_reflection_pose(np.array([0.0, 0.0, 1.0]), 0.0)
        assert np.allclose(matrix @ matrix, np.eye(4))

    def test_probe_3_mirror_image(self) -> None:
        matrix = compute_householder_reflection_pose(np.array([0.0, 0.0, 1.0]), 0.0)
        reflected = matrix @ np.array([0.0, 0.0, 2.0, 1.0])
        assert np.allclose(reflected[:3] / reflected[3], np.array([0.0, 0.0, -2.0]))

    def test_probe_4_brewster_zero_p(self) -> None:
        fresnel = compute_fresnel_reflectance_rgb(0.0, 1.0, 1.5)
        at_brewster = compute_fresnel_reflectance_rgb(fresnel.brewster_deg, 1.0, 1.5)
        assert float(np.max(at_brewster.reflectance_p)) < 1e-9

    def test_probe_5_fresnel_bounds(self) -> None:
        fresnel = compute_fresnel_reflectance_rgb(np.array([10.0, 45.0, 80.0]), 1.0, 1.5)
        assert np.all(fresnel.reflectance_s >= 0.0) and np.all(fresnel.reflectance_s <= 1.0)
        assert np.all(fresnel.reflectance_s >= fresnel.reflectance_p)

    def test_probe_6_epipolar_consistency(self) -> None:
        consistency = verify_reflection_epipolar_consistency(
            np.array([0.0, 0.0, 1.0]),
            0.0,
            np.array([0.0, 0.0, 2.0]),
            np.array([1.0, 1.0, 1.0]),
        )
        assert consistency.consistent


class TestLaw4Shadow:
    def test_probe_1_shadow_on_plane(self) -> None:
        field = synthesize_coherent_cast_shadows(
            np.array([[0.5, 0.0, 1.0]]),
            np.array([0.0, 0.0, 3.0, 1.0]),
            np.array([0.0, 0.0, 1.0, 0.0]),
        )
        assert abs(float(field.shadow_vertices[0, 2])) < 1e-6

    def test_probe_2_matrix_is_4x4(self) -> None:
        matrix = compute_shadow_projection_matrix(
            np.array([0.0, 0.0, 3.0, 1.0]), np.array([0.0, 0.0, 1.0, 0.0])
        )
        assert matrix.shape == (4, 4)

    def test_probe_3_penumbra_grows_with_height(self) -> None:
        assert penumbra_radius(0.5, 0.05, 3.0) < penumbra_radius(1.0, 0.05, 3.0)

    def test_probe_4_penumbra_grows_with_emitter(self) -> None:
        assert penumbra_radius(0.5, 0.05, 3.0) < penumbra_radius(0.5, 0.1, 3.0)

    def test_probe_5_contact_ao_bounds(self) -> None:
        field = synthesize_coherent_cast_shadows(
            np.array([[0.0, 0.0, 0.1], [0.0, 0.0, 2.0]]),
            np.array([0.0, 0.0, 3.0, 1.0]),
            np.array([0.0, 0.0, 1.0, 0.0]),
        )
        assert np.all(field.contact_ao >= 0.0) and np.all(field.contact_ao <= 1.0)

    def test_probe_6_plane_validation(self) -> None:
        with pytest.raises(ShadowGeometryError):
            compute_shadow_projection_matrix(
                np.array([0.0, 0.0, 3.0, 1.0]), np.array([0.0, 0.0, 0.0, 0.0])
            )


class TestLaw5Permanence:
    def test_probe_1_se3_roundtrip(self) -> None:
        xi = np.array([0.1, -0.2, 0.3, 1.0, 2.0, -0.5])
        assert np.allclose(se3_log(se3_exp(xi)), xi, atol=1e-9)

    def test_probe_2_identity_log(self) -> None:
        assert np.allclose(se3_log(np.eye(4)), 0.0)

    def test_probe_3_extent_conserved(self) -> None:
        state = ObjectState(np.eye(4), np.zeros(6), np.array([1.0, 1.0, 1.0]), 1.0)
        propagated = propagate_occluded_object(state, 0.01, steps=5)
        assert np.allclose(propagated[-1].extents, state.extents)

    def test_probe_4_teleport_flagged(self) -> None:
        a = ObjectState(np.eye(4), np.zeros(6), np.ones(3), 1.0)
        b_pose = np.eye(4)
        b_pose[:3, 3] = [100.0, 0.0, 0.0]
        b = ObjectState(b_pose, np.zeros(6), np.ones(3), 1.0)
        audit = audit_object_permanence((a, b))
        assert "teleport" in audit.violations_before

    def test_probe_5_rotation_drift_flagged(self) -> None:
        pose = np.eye(4)
        pose[:3, :3] *= 1.5
        state = ObjectState(pose, np.zeros(6), np.ones(3), 1.0)
        audit = audit_object_permanence((state,))
        assert "rotation_determinant" in audit.violations_before

    def test_probe_6_audit_heals(self) -> None:
        a = ObjectState(np.eye(4), np.zeros(6), np.ones(3), 1.0)
        b_pose = np.eye(4)
        b_pose[:3, 3] = [100.0, 0.0, 0.0]
        b = ObjectState(b_pose, np.zeros(6), np.ones(3), 1.0)
        audit = audit_object_permanence((a, b))
        assert audit.violations_after == ()


class TestLaw6Typography:
    SRC = np.array([[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]])
    DST = np.array([[10.0, 10.0], [90.0, 20.0], [80.0, 80.0], [20.0, 90.0]])

    def test_probe_1_sl3_determinant(self) -> None:
        homography = compute_planar_quad_homography(self.SRC, self.DST)
        assert np.linalg.det(homography) == pytest.approx(1.0)

    def test_probe_2_corner_mapping(self) -> None:
        homography = compute_planar_quad_homography(self.SRC, self.DST)
        mapped = []
        for point in self.SRC:
            vec = homography @ np.array([point[0], point[1], 1.0])
            mapped.append(vec[:2] / vec[2])
        assert np.allclose(np.array(mapped), self.DST, atol=1e-6)

    def test_probe_3_glyph_bitmap(self) -> None:
        bitmap = procedural_glyph_bitmap("A")
        assert bitmap.ndim == 2 and float(bitmap.max()) > 0.0

    def test_probe_4_text_visible(self) -> None:
        render = render_homography_locked_diegetic_text("HELLO", self.DST, (100, 100))
        assert float(render.mask.max()) > 0.0

    def test_probe_5_degenerate_quad(self) -> None:
        degenerate = np.array([[0.0, 0.0], [1.0, 1.0], [2.0, 2.0], [3.0, 3.0]])
        with pytest.raises(DegenerateHomographyError):
            compute_planar_quad_homography(self.SRC, degenerate)

    def test_probe_6_ewa_footprint(self) -> None:
        render = render_homography_locked_diegetic_text("VERITAS", self.DST, (100, 100))
        assert float(np.min(render.jacobian_frobenius)) > 0.0


class TestLaw7Mechanics:
    def test_probe_1_free_fall(self) -> None:
        positions = np.zeros((1, 3))
        velocities = np.zeros((1, 3))
        forces = np.array([[0.0, 0.0, -9.80665]])
        new_pos, new_vel = stormer_verlet_step(positions, velocities, np.array([1.0]), forces, 0.01)
        assert new_pos[0, 2] < 0.0 and new_vel[0, 2] < 0.0

    def test_probe_2_momentum_conservation(self) -> None:
        positions = np.array([[0.0, 0.0, 0.0], [1.5, 0.0, 0.0]])
        velocities = np.array([[1.0, 0.0, 0.0], [-1.0, 0.0, 0.0]])
        masses = np.array([1.0, 2.0])
        result = resolve_rigid_body_collision(positions, velocities, masses, np.array([1.0, 1.0]))
        assert np.allclose(result.momentum_after, result.momentum_before, atol=1e-12)

    def test_probe_3_elastic_energy(self) -> None:
        positions = np.array([[0.0, 0.0, 0.0], [1.5, 0.0, 0.0]])
        velocities = np.array([[1.0, 0.0, 0.0], [-1.0, 0.0, 0.0]])
        masses = np.array([1.0, 1.0])
        result = resolve_rigid_body_collision(
            positions, velocities, masses, np.array([1.0, 1.0]), restitution=1.0, friction=0.0
        )
        assert result.energy_after == pytest.approx(result.energy_before, abs=1e-9)

    def test_probe_4_inelastic_dissipation(self) -> None:
        positions = np.array([[0.0, 0.0, 0.0], [1.5, 0.0, 0.0]])
        velocities = np.array([[1.0, 0.0, 0.0], [-1.0, 0.0, 0.0]])
        masses = np.array([1.0, 1.0])
        result = resolve_rigid_body_collision(
            positions, velocities, masses, np.array([1.0, 1.0]), restitution=0.0, friction=0.0
        )
        assert result.energy_after <= result.energy_before + 1e-9

    def test_probe_5_angular_momentum(self) -> None:
        positions = np.array([[0.0, 0.0, 0.0], [1.5, 0.0, 0.0]])
        velocities = np.array([[0.0, 1.0, 0.0], [0.0, 1.0, 0.0]])
        masses = np.array([2.0, 2.0])
        result = resolve_rigid_body_collision(positions, velocities, masses, np.array([1.0, 1.0]))
        assert np.allclose(result.angular_momentum_after, result.angular_momentum_before, atol=1e-9)

    def test_probe_6_validation(self) -> None:
        with pytest.raises(MechanicsViolationError):
            total_linear_momentum(np.zeros((2, 2)), np.array([1.0, 1.0]))


class TestLaw8Cloth:
    def test_probe_1_capsule_surface(self) -> None:
        sdf = capsule_sdf(
            np.array([[0.5, 0.0, 0.5]]), np.array([0.0, 0.0, 0.0]), np.array([0.0, 0.0, 1.0]), 0.5
        )
        assert sdf[0] == pytest.approx(0.0)

    def test_probe_2_gravity_drape(self) -> None:
        pos, edges = _grid(3, 3, 2.0)
        result = step_xpbd_cloth_simulation(pos, np.zeros_like(pos), edges)
        assert np.all(result.positions[:, 2] < pos[:, 2])

    def test_probe_3_zero_penetration(self) -> None:
        pos, edges = _grid(3, 3, 0.0)
        pos[:, 0] -= 1.0
        capsule = (np.array([-2.0, 0.0, 0.0]), np.array([2.0, 0.0, 0.0]), 0.3)
        result = step_xpbd_cloth_simulation(
            pos, np.zeros_like(pos), edges, capsules=[capsule], iterations=8
        )
        assert result.min_sdf >= -1e-6

    def test_probe_4_stretch_bounded(self) -> None:
        pos, edges = _grid(3, 3, 2.0)
        result = step_xpbd_cloth_simulation(pos, np.zeros_like(pos), edges)
        assert result.max_stretch < 1.0

    def test_probe_5_validation(self) -> None:
        pos, edges = _grid(2, 2, 0.0)
        with pytest.raises(ClothSimulationError):
            step_xpbd_cloth_simulation(pos, np.zeros_like(pos), np.array([[0.0, 99.0]]))

    def test_probe_6_rest_length_preserved(self) -> None:
        pos = np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0]])
        edges = np.array([[0.0, 1.0]])
        result = step_xpbd_cloth_simulation(pos, np.zeros_like(pos), edges, iterations=8)
        length = float(np.linalg.norm(result.positions[0] - result.positions[1]))
        assert length == pytest.approx(1.0, abs=1e-2)


class TestLaw9Thermodynamics:
    def test_probe_1_uniform_entropy(self) -> None:
        assert spatial_entropy(np.ones((4, 4))) == pytest.approx(np.log(16.0))

    def test_probe_2_delta_entropy(self) -> None:
        delta = np.zeros((4, 4))
        delta[0, 0] = 1.0
        assert spatial_entropy(delta) == pytest.approx(0.0)

    def test_probe_3_mass_conservation(self) -> None:
        field = np.zeros((16, 16))
        field[8, 8] = 1.0
        diffused = fourier_advection_diffusion(field, (0.0, 0.0), 0.1, 0.01, 1.0)
        assert diffused.sum() == pytest.approx(field.sum(), abs=1e-9)

    def test_probe_4_entropy_increase(self) -> None:
        field = np.zeros((16, 16))
        field[8, 8] = 1.0
        diffused = np.clip(
            fourier_advection_diffusion(field, (0.0, 0.0), 0.1, 0.01, 1.0), 0.0, None
        )
        assert spatial_entropy(diffused) > spatial_entropy(field)

    def test_probe_5_retarded_support(self) -> None:
        wave = retarded_wavefront((12, 12), (5.0, 5.0), 2.0, 2.0, frames=6)
        assert np.allclose(wave[:2], 0.0)

    def test_probe_6_causality_and_heal(self) -> None:
        wave = retarded_wavefront((10, 10), (4.0, 4.0), 1.0, 1.5, frames=5)
        assert verify_retarded_causality(wave, (4.0, 4.0), 1.0, 1.5)
        frames = np.zeros((3, 6, 6))
        frames[0] = 1.0
        frames[1, 0, 0] = 1.0
        audit = audit_and_enforce_thermodynamic_arrow(frames)
        assert "entropy_decrease" in audit.violations_before
        assert audit.violations_after == ()


class TestLaw10Face:
    def test_probe_1_duchenne_violation(self) -> None:
        au = np.zeros((1, 26))
        au[0, 11] = 1.0
        result = enforce_facs_synergy_manifold(au)
        assert result.violations_before

    def test_probe_2_duchenne_heal(self) -> None:
        au = np.zeros((1, 26))
        au[0, 11] = 1.0
        result = enforce_facs_synergy_manifold(au)
        assert result.action_units[0, 5] >= 0.5 * (1.0 - 0.1) - 1e-9

    def test_probe_3_blink_bounded(self) -> None:
        blink = synthesize_asymmetric_blink_curve(0.4)
        assert blink.peak_closure <= 1.0

    def test_probe_4_blink_asymmetry(self) -> None:
        blink = synthesize_asymmetric_blink_curve(0.4)
        peak = int(np.argmax(blink.closure))
        rise = peak
        fall = len(blink.closure) - peak
        assert rise < fall

    def test_probe_5_sss_shape(self) -> None:
        diffuse = np.random.default_rng(0).random((8, 8, 3))
        specular = np.zeros((8, 8, 3))
        mask = np.ones((8, 8))
        composite = apply_dermal_subsurface_scattering(diffuse, specular, mask)
        assert composite.shape == diffuse.shape

    def test_probe_6_facs_validation(self) -> None:
        with pytest.raises(FacialDynamicsError):
            enforce_facs_synergy_manifold(-np.ones((1, 26)))
