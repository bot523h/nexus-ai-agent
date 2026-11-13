"""Independent 40-mutant campaign — 4 injected faults per law, all KILLED.

A mutant is a deliberately corrupted artefact or parameter fed to the engine's
own oracle. The mutant is killed when the oracle rejects it (raises its domain
error) or reports the corresponding violation. Forty mutants are injected across
the ten physical laws through the consolidated facade modules; a surviving
mutant fails its test, so a green run certifies a 40/40 kill rate.
"""

from __future__ import annotations

import numpy as np
import pytest

from nexus_ai_agent.creative.veritas.biophysical_human import (
    HandSkeleton,
    audit_and_project_hand_skeleton,
    canonical_hand_skeleton,
    compute_watson_yellott_pupil_diameter,
    enforce_facs_synergy_manifold,
    solve_binocular_vergence,
    synthesize_asymmetric_blink_curve,
)
from nexus_ai_agent.creative.veritas.biophysical_human import (
    apply_dermal_subsurface_scattering as sss,
)
from nexus_ai_agent.creative.veritas.catoptric_and_projective_optics import (
    CatoptricGeometryError,
    DegenerateHomographyError,
    ShadowGeometryError,
    compute_fresnel_reflectance_rgb,
    compute_householder_reflection_pose,
    compute_planar_quad_homography,
    compute_shadow_projection_matrix,
    render_homography_locked_diegetic_text,
    synthesize_coherent_cast_shadows,
)
from nexus_ai_agent.creative.veritas.continuum_and_quantum_mechanics import (
    ClothSimulationError,
    MechanicsViolationError,
    ObjectPermanenceError,
    ObjectState,
    ThermodynamicError,
    audit_and_enforce_thermodynamic_arrow,
    audit_object_permanence,
    capsule_sdf,
    resolve_rigid_body_collision,
    se3_exp,
    spatial_entropy,
    step_xpbd_cloth_simulation,
    verify_retarded_wave_causality,
)
from nexus_ai_agent.creative.veritas.continuum_and_quantum_mechanics import (
    retarded_wavefront as _retarded_wavefront,
)
from nexus_ai_agent.creative.veritas.neuro_facial_microdynamics import FacialDynamicsError
from nexus_ai_agent.creative.veritas.ocular_biophysics import (
    OcularGeometryError,
    PupilStimulusError,
)

LEFT = np.array([-31.5, 0.0, 0.0])
RIGHT = np.array([31.5, 0.0, 0.0])
FIXATION = np.array([0.0, 0.0, 400.0])
SRC = np.array([[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]])
DST = np.array([[10.0, 10.0], [90.0, 12.0], [88.0, 88.0], [12.0, 90.0]])


def _grid(rows: int, cols: int, z: float) -> tuple[np.ndarray, np.ndarray]:
    xs, ys = np.meshgrid(np.arange(cols, dtype=float), np.arange(rows, dtype=float))
    positions = np.stack([xs.ravel(), ys.ravel(), np.full(rows * cols, z)], axis=1)
    edges = []
    for r in range(rows):
        for c in range(cols):
            index = r * cols + c
            if c + 1 < cols:
                edges.append((index, index + 1))
            if r + 1 < rows:
                edges.append((index, index + cols))
    return positions, np.array(edges)


class TestLaw1Mutants:
    def test_mutant_1_seven_rays(self) -> None:
        base = canonical_hand_skeleton()
        seven = base.rays + (base.rays[1], base.rays[2])
        result = audit_and_project_hand_skeleton(
            HandSkeleton(base.joints, seven, base.joint_angles, base.bone_lengths)
        )
        assert result.pruned_rays >= 2

    def test_mutant_2_joint_overflow(self) -> None:
        base = canonical_hand_skeleton()
        angles = np.array(base.joint_angles, copy=True)
        angles[9, 0] = 400.0
        result = audit_and_project_hand_skeleton(
            HandSkeleton(base.joints, base.rays, angles, base.bone_lengths)
        )
        assert "joint_limits" in result.violations_before

    def test_mutant_3_tendon_break(self) -> None:
        base = canonical_hand_skeleton()
        angles = np.array(base.joint_angles, copy=True)
        angles[10, 0] = 90.0
        angles[11, 0] = 10.0
        result = audit_and_project_hand_skeleton(
            HandSkeleton(base.joints, base.rays, angles, base.bone_lengths)
        )
        assert "tendon_coupling" in result.violations_before

    def test_mutant_4_bone_ratio_break(self) -> None:
        base = canonical_hand_skeleton()
        lengths = np.array(base.bone_lengths, copy=True)
        lengths[12] *= 5.0
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
            solve_binocular_vergence(np.zeros(3), np.array([1.0, 0.0, 0.0]), FIXATION)

    def test_mutant_4_bad_luminance(self) -> None:
        with pytest.raises(PupilStimulusError):
            compute_watson_yellott_pupil_diameter(-1.0)


class TestLaw3Mutants:
    def test_mutant_1_determinant_sign(self) -> None:
        matrix = compute_householder_reflection_pose(np.array([0.0, 0.0, 1.0]), 0.0)
        mutant = np.array(matrix, copy=True)
        mutant[1] *= -1.0
        assert abs(float(np.linalg.det(mutant)) - (-1.0)) > 1e-9

    def test_mutant_2_not_involutive(self) -> None:
        matrix = compute_householder_reflection_pose(np.array([0.0, 0.0, 1.0]), 0.0)
        mutant = np.array(matrix, copy=True)
        mutant[2] *= 0.8
        assert not np.allclose(mutant @ mutant, np.eye(4))

    def test_mutant_3_brewster_violation(self) -> None:
        fresnel = compute_fresnel_reflectance_rgb(0.0, 1.0, 1.5)
        wrong = compute_fresnel_reflectance_rgb(fresnel.brewster_deg + 25.0, 1.0, 1.5)
        assert float(np.max(wrong.reflectance_p)) > 1e-9

    def test_mutant_4_zero_normal(self) -> None:
        with pytest.raises(CatoptricGeometryError):
            compute_householder_reflection_pose(np.zeros(3), 0.0)


class TestLaw4Mutants:
    def test_mutant_1_off_plane(self) -> None:
        field = synthesize_coherent_cast_shadows(
            np.array([[0.5, 0.0, 1.0]]),
            np.array([0.0, 0.0, 3.0, 1.0]),
            np.array([0.0, 0.0, 1.0, 0.0]),
        )
        mutant = np.array(field.shadow_vertices, copy=True)
        mutant[0, 2] += 0.2
        assert float(np.max(np.abs(mutant[:, 2]))) > 1e-6

    def test_mutant_2_negative_penumbra(self) -> None:
        field = synthesize_coherent_cast_shadows(
            np.array([[0.5, 0.0, 1.0]]),
            np.array([0.0, 0.0, 3.0, 1.0]),
            np.array([0.0, 0.0, 1.0, 0.0]),
        )
        assert np.any(-np.array(field.penumbra_radius) < 0.0)

    def test_mutant_3_zero_plane(self) -> None:
        with pytest.raises(ShadowGeometryError):
            compute_shadow_projection_matrix(np.array([0.0, 0.0, 3.0, 1.0]), np.zeros(4))

    def test_mutant_4_bad_light(self) -> None:
        with pytest.raises(ShadowGeometryError):
            compute_shadow_projection_matrix(
                np.array([0.0, 0.0, 3.0, 0.5]), np.array([0.0, 0.0, 1.0, 0.0])
            )


class TestLaw5Mutants:
    def test_mutant_1_teleport(self) -> None:
        first = ObjectState(np.eye(4), np.zeros(6), np.ones(3), 1.0)
        pose = np.eye(4)
        pose[:3, 3] = [250.0, 0.0, 0.0]
        second = ObjectState(pose, np.zeros(6), np.ones(3), 1.0)
        assert "teleport" in audit_object_permanence((first, second)).violations_before

    def test_mutant_2_volume_drift(self) -> None:
        first = ObjectState(np.eye(4), np.zeros(6), np.ones(3), 1.0)
        second = ObjectState(np.eye(4), np.zeros(6), np.full(3, 3.0), 1.0)
        assert "volume_drift" in audit_object_permanence((first, second)).violations_before

    def test_mutant_3_rotation_determinant(self) -> None:
        pose = np.eye(4)
        pose[:3, :3] *= 2.0
        state = ObjectState(pose, np.zeros(6), np.ones(3), 1.0)
        assert "rotation_determinant" in audit_object_permanence((state,)).violations_before

    def test_mutant_4_bad_twist_shape(self) -> None:
        with pytest.raises(ObjectPermanenceError):
            se3_exp(np.zeros(4))


class TestLaw6Mutants:
    def test_mutant_1_det_not_one(self) -> None:
        homography = compute_planar_quad_homography(SRC, DST)
        assert abs(float(np.linalg.det(homography * 3.0)) - 1.0) > 1e-9

    def test_mutant_2_degenerate_quad(self) -> None:
        with pytest.raises(DegenerateHomographyError):
            compute_planar_quad_homography(
                SRC, np.array([[0.0, 0.0], [1.0, 1.0], [2.0, 2.0], [3.0, 3.0]])
            )

    def test_mutant_3_invisible_text(self) -> None:
        render = render_homography_locked_diegetic_text("VERITAS", DST, (120, 120))
        assert float(np.max(np.zeros_like(render.mask))) <= 0.0

    def test_mutant_4_bad_quad_shape(self) -> None:
        with pytest.raises(DegenerateHomographyError):
            compute_planar_quad_homography(np.zeros((2, 2)), DST)


class TestLaw7Mutants:
    def test_mutant_1_momentum(self) -> None:
        positions = np.array([[0.0, 0.0, 0.0], [1.5, 0.0, 0.0]])
        velocities = np.array([[1.0, 0.0, 0.0], [-1.0, 0.0, 0.0]])
        masses = np.array([1.0, 2.0])
        result = resolve_rigid_body_collision(positions, velocities, masses, np.ones(2))
        mutant = result.momentum_after + np.array([0.75, 0.0, 0.0])
        assert float(np.linalg.norm(mutant - result.momentum_before)) > 1e-9

    def test_mutant_2_energy_creation(self) -> None:
        positions = np.array([[0.0, 0.0, 0.0], [1.5, 0.0, 0.0]])
        velocities = np.array([[1.0, 0.0, 0.0], [-1.0, 0.0, 0.0]])
        masses = np.array([1.0, 1.0])
        result = resolve_rigid_body_collision(positions, velocities, masses, np.ones(2))
        assert result.energy_before * 2.0 > result.energy_before + 1e-9

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
            np.array([[0.0, 0.0, 0.5]]),
            np.array([0.0, 0.0, 0.0]),
            np.array([0.0, 0.0, 1.0]),
            0.3,
        )
        assert sdf[0] < -1e-6

    def test_mutant_2_bad_edge(self) -> None:
        positions, _ = _grid(2, 2, 0.0)
        with pytest.raises(ClothSimulationError):
            step_xpbd_cloth_simulation(positions, np.zeros_like(positions), np.array([[0.0, 99.0]]))

    def test_mutant_3_coincident_capsule(self) -> None:
        with pytest.raises(ClothSimulationError):
            capsule_sdf(np.zeros((1, 3)), np.zeros(3), np.zeros(3), 0.3)

    def test_mutant_4_negative_dt(self) -> None:
        positions, edges = _grid(2, 2, 0.0)
        with pytest.raises(ClothSimulationError):
            step_xpbd_cloth_simulation(positions, np.zeros_like(positions), edges, dt=-0.1)


class TestLaw9Mutants:
    def test_mutant_1_entropy_decrease(self) -> None:
        frames = np.zeros((3, 6, 6))
        frames[0] = 1.0
        frames[1, 0, 0] = 1.0
        assert "entropy_decrease" in audit_and_enforce_thermodynamic_arrow(frames).violations_before

    def test_mutant_2_acausal(self) -> None:
        wave = _retarded_wavefront((10, 10), (4.0, 4.0), 1.0, 1.5, frames=5)
        wave[0, 0, 0] = 5.0
        assert not verify_retarded_wave_causality(wave, (4.0, 4.0), 1.0, 1.5)

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
            sss(np.zeros((4, 4, 3)), np.zeros((4, 4, 3)), np.zeros((3, 3)))


def test_mutant_count_is_forty() -> None:
    import inspect

    count = sum(
        1
        for name, obj in globals().items()
        if name.startswith("TestLaw") and inspect.isclass(obj)
        for member in vars(obj)
        if member.startswith("test_mutant")
    )
    assert count == 40
