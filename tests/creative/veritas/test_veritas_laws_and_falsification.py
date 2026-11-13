"""Falsification suite for the NAGAR VERITAS 11-law reality engine.

Every law is probed adversarially. A probe either proves the engine *fails
closed* (raises its documented domain error) or *deterministically heals* the
invalid state to zero residual violations. Sixty probes are collected in
``PROBES`` and parametrised; Law 11 receives dedicated unit and property tests
because it is the newest engine.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import numpy as np
import pytest

from nexus_ai_agent.creative.veritas.biophysical_human import (
    HandSkeleton,
    apply_dermal_subsurface_scattering,
    audit_and_project_hand_skeleton,
    canonical_hand_skeleton,
    compute_watson_yellott_pupil_diameter,
    enforce_facs_synergy_manifold,
    solve_binocular_vergence,
    synthesize_asymmetric_blink_curve,
    synthesize_saccade_vor_trajectory,
)
from nexus_ai_agent.creative.veritas.catoptric_and_projective_optics import (
    CatoptricGeometryError,
    ShadowGeometryError,
    compute_fresnel_reflectance_rgb,
    compute_householder_reflection_pose,
    compute_planar_quad_homography,
    compute_shadow_projection_matrix,
    penumbra_radius,
    render_homography_locked_diegetic_text,
    synthesize_coherent_cast_shadows,
    warp_ripple_reflection,
)
from nexus_ai_agent.creative.veritas.continuum_and_quantum_mechanics import (
    ObjectState,
    audit_and_enforce_thermodynamic_arrow,
    audit_object_permanence,
    bohmian_quantum_potential,
    capsule_sdf,
    madelung_continuity_residual,
    propagate_occluded_object_se3,
    resolve_rigid_body_collision,
    stabilize_madelung_phase_coherence,
    step_symplectic_verlet,
    step_xpbd_cloth_simulation,
    verify_retarded_wave_causality,
)
from nexus_ai_agent.creative.veritas.continuum_and_quantum_mechanics import (
    propagate_occluded_object as _propagate,
)
from nexus_ai_agent.creative.veritas.continuum_and_quantum_mechanics import (
    stormer_verlet_step as _stormer,
)
from nexus_ai_agent.creative.veritas.continuum_and_quantum_mechanics import (
    verify_retarded_causality as _retarded,
)
from nexus_ai_agent.creative.veritas.hand_kinematics import HandTopologyError
from nexus_ai_agent.creative.veritas.homography_diegetic_typography import (
    DegenerateHomographyError,
    TypographyError,
)
from nexus_ai_agent.creative.veritas.neuro_facial_microdynamics import FacialDynamicsError
from nexus_ai_agent.creative.veritas.ocular_biophysics import (
    OcularGeometryError,
    PupilStimulusError,
)
from nexus_ai_agent.creative.veritas.se3_object_permanence import ObjectPermanenceError
from nexus_ai_agent.creative.veritas.symplectic_contact_mechanics import (
    MechanicsViolationError,
    total_linear_momentum,
)
from nexus_ai_agent.creative.veritas.thermodynamic_arrow_of_time import (
    ThermodynamicError,
    fourier_advection_diffusion,
)
from nexus_ai_agent.creative.veritas.xpbd_cloth_drape import ClothSimulationError


def _assert_healed(result: object) -> None:
    """Assert that a result object reports a healed / violation-free state."""
    if hasattr(result, "healed"):
        assert result.healed is True
    elif hasattr(result, "violations_after"):
        assert not result.violations_after
    elif hasattr(result, "min_sdf"):
        assert result.min_sdf >= -1e-9
    else:  # pragma: no cover - defensive
        raise AssertionError("result exposes no healing signal")


def _skeleton(**overrides: object) -> HandSkeleton:
    base = canonical_hand_skeleton()
    return HandSkeleton(
        joints=overrides.get("joints", base.joints.copy()),  # type: ignore[arg-type]
        rays=overrides.get("rays", base.rays),  # type: ignore[arg-type]
        joint_angles=overrides.get("joint_angles", base.joint_angles.copy()),  # type: ignore[arg-type]
        bone_lengths=overrides.get("bone_lengths", base.bone_lengths.copy()),  # type: ignore[arg-type]
    )


# --------------------------------------------------------------------------- #
# Law 1 — hand topology
# --------------------------------------------------------------------------- #
def law1_six_ray_polydactyly() -> None:
    base = canonical_hand_skeleton()
    six = _skeleton(rays=base.rays + (base.rays[1],))
    result = audit_and_project_hand_skeleton(six)
    assert result.pruned_rays >= 1
    assert len(result.skeleton.rays) == 5


def law1_backwards_mcp_hyperextension() -> None:
    base = canonical_hand_skeleton()
    angles = base.joint_angles.copy()
    angles[5, 0] = -45.0
    result = audit_and_project_hand_skeleton(_skeleton(joint_angles=angles))
    assert result.skeleton.joint_angles[5, 0] >= -15.0 - 1e-6
    _assert_healed(result)


def law1_tendon_coupling_violation() -> None:
    base = canonical_hand_skeleton()
    angles = base.joint_angles.copy()
    angles[6, 0] = 10.0
    angles[7, 0] = 80.0
    result = audit_and_project_hand_skeleton(_skeleton(joint_angles=angles))
    healed = result.skeleton.joint_angles
    assert abs(healed[7, 0] - (2.0 / 3.0) * healed[6, 0]) < 1.0
    _assert_healed(result)


def law1_bone_ratio_violation() -> None:
    base = canonical_hand_skeleton()
    lengths = base.bone_lengths.copy()
    lengths[6] *= 3.0
    result = audit_and_project_hand_skeleton(_skeleton(bone_lengths=lengths))
    _assert_healed(result)


def law1_truncated_skeleton_fails_closed() -> None:
    base = canonical_hand_skeleton()
    truncated = _skeleton(
        joints=base.joints[:10].copy(),
        joint_angles=base.joint_angles[:10].copy(),
        bone_lengths=base.bone_lengths[:10].copy(),
    )
    with pytest.raises(HandTopologyError):
        audit_and_project_hand_skeleton(truncated)


def law1_grip_penetration_is_projected() -> None:
    base = canonical_hand_skeleton()
    tip = base.joints[4].copy()

    def sdf(points: np.ndarray) -> np.ndarray:
        return np.asarray(
            np.linalg.norm(np.asarray(points) - tip, axis=-1) - 0.01, dtype=np.float64
        )

    result = audit_and_project_hand_skeleton(base, target_sdf=sdf, grip_active=True)
    assert "grip_penetration" in result.violations_before
    assert result.grip_penetration >= -1e-6


# --------------------------------------------------------------------------- #
# Law 2 — ocular biophysics
# --------------------------------------------------------------------------- #
def law2_divergent_gaze_is_healed() -> None:
    left = np.array([-31.5, 0.0, 0.0])
    right = np.array([31.5, 0.0, 0.0])
    solution = solve_binocular_vergence(
        left,
        right,
        np.array([0.0, 0.0, 400.0]),
        axis_left=np.array([0.0, 0.0, -1.0]),
        axis_right=np.array([0.0, 0.0, -1.0]),
    )
    assert solution.vergence_deg < 15.0
    assert solution.skew_mm < 0.5


def law2_skew_gaze_is_healed() -> None:
    left = np.array([-32.0, 0.0, 0.0])
    right = np.array([32.0, 0.0, 0.0])
    solution = solve_binocular_vergence(
        left,
        right,
        np.zeros(3),
        axis_left=np.array([0.0, 1.0, 0.0]),
        axis_right=np.array([0.0, 1.0, 0.0]),
    )
    assert solution.skew_mm < 0.5


def law2_ipd_out_of_range() -> None:
    with pytest.raises(OcularGeometryError):
        solve_binocular_vergence(
            np.array([-100.0, 0.0, 0.0]), np.array([100.0, 0.0, 0.0]), np.zeros(3)
        )


def law2_degenerate_optical_axis() -> None:
    with pytest.raises(OcularGeometryError):
        solve_binocular_vergence(
            np.array([-32.0, 0.0, 0.0]),
            np.array([32.0, 0.0, 0.0]),
            np.zeros(3),
            axis_left=np.zeros(3),
        )


def law2_negative_saccade_duration() -> None:
    with pytest.raises(OcularGeometryError):
        synthesize_saccade_vor_trajectory(10.0, -1.0)


def law2_negative_pupil_luminance() -> None:
    with pytest.raises(PupilStimulusError):
        compute_watson_yellott_pupil_diameter(-10.0)


# --------------------------------------------------------------------------- #
# Law 3 — catoptric optics
# --------------------------------------------------------------------------- #
def law3_non_unit_mirror_normal() -> None:
    with pytest.raises(CatoptricGeometryError):
        compute_householder_reflection_pose(np.array([0.0, 0.0, 2.0]), 0.0)


def law3_zero_mirror_normal() -> None:
    with pytest.raises(CatoptricGeometryError):
        compute_householder_reflection_pose(np.zeros(3), 0.0)


def law3_zero_incident_index() -> None:
    with pytest.raises(CatoptricGeometryError):
        compute_fresnel_reflectance_rgb(30.0, 0.0, 1.5)


def law3_negative_refracted_index() -> None:
    with pytest.raises(CatoptricGeometryError):
        compute_fresnel_reflectance_rgb(30.0, 1.0, -1.5)


def law3_incident_angle_out_of_range() -> None:
    with pytest.raises(CatoptricGeometryError):
        compute_fresnel_reflectance_rgb(120.0, 1.0, 1.5)


def law3_ripple_on_one_dimensional_image() -> None:
    with pytest.raises(CatoptricGeometryError):
        warp_ripple_reflection(np.zeros(8), 0.0)


# --------------------------------------------------------------------------- #
# Law 4 — projective shadows
# --------------------------------------------------------------------------- #
def law4_light_not_homogeneous() -> None:
    with pytest.raises(ShadowGeometryError):
        compute_shadow_projection_matrix(np.array([0.0, 0.0, 1.0]), np.array([0.0, 0.0, 1.0, 0.0]))


def law4_light_bad_w_component() -> None:
    with pytest.raises(ShadowGeometryError):
        compute_shadow_projection_matrix(
            np.array([0.0, 0.0, 1.0, 2.0]), np.array([0.0, 0.0, 1.0, 0.0])
        )


def law4_plane_zero_normal() -> None:
    with pytest.raises(ShadowGeometryError):
        compute_shadow_projection_matrix(np.array([0.0, 0.0, 1.0, 1.0]), np.zeros(4))


def law4_negative_emitter_radius() -> None:
    with pytest.raises(ShadowGeometryError):
        penumbra_radius(0.5, -1.0, 3.0)


def law4_occluder_above_light() -> None:
    with pytest.raises(ShadowGeometryError):
        penumbra_radius(10.0, 0.05, 3.0)


def law4_bad_vertex_shape() -> None:
    with pytest.raises(ShadowGeometryError):
        synthesize_coherent_cast_shadows(
            np.zeros((2, 2)), np.array([0.0, 0.0, 1.0, 1.0]), np.array([0.0, 0.0, 1.0, 0.0])
        )


# --------------------------------------------------------------------------- #
# Law 5 — SE(3) object permanence
# --------------------------------------------------------------------------- #
def law5_bad_pose_shape() -> None:
    with pytest.raises(ObjectPermanenceError):
        ObjectState(np.eye(3), np.zeros(6), np.ones(3), 1.0)


def law5_non_positive_mass() -> None:
    with pytest.raises(ObjectPermanenceError):
        ObjectState(np.eye(4), np.zeros(6), np.ones(3), 0.0)


def law5_bad_twist_shape() -> None:
    with pytest.raises(ObjectPermanenceError):
        ObjectState(np.eye(4), np.zeros(5), np.ones(3), 1.0)


def law5_bad_extents_shape() -> None:
    with pytest.raises(ObjectPermanenceError):
        ObjectState(np.eye(4), np.zeros(6), np.ones(2), 1.0)


def law5_non_finite_pose() -> None:
    pose = np.eye(4)
    pose[0, 3] = np.nan
    with pytest.raises(ObjectPermanenceError):
        ObjectState(pose, np.zeros(6), np.ones(3), 1.0)


def law5_teleportation_is_healed() -> None:
    first = ObjectState(np.eye(4), np.zeros(6), np.ones(3), 1.0)
    pose = np.eye(4)
    pose[0, 3] = 100.0
    second = ObjectState(pose, np.zeros(6), np.ones(3), 1.0)
    audit = audit_object_permanence((first, second), impulse_bound=0.5)
    assert audit.violations_before
    _assert_healed(audit)


# --------------------------------------------------------------------------- #
# Law 6 — diegetic typography
# --------------------------------------------------------------------------- #
def _square() -> np.ndarray:
    return np.array([[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]])


def law6_collinear_quad_is_degenerate() -> None:
    collinear = np.array([[0.0, 0.0], [1.0, 0.0], [2.0, 0.0], [3.0, 0.0]])
    with pytest.raises(DegenerateHomographyError):
        compute_planar_quad_homography(collinear, _square())


def law6_quad_wrong_shape() -> None:
    with pytest.raises(DegenerateHomographyError):
        compute_planar_quad_homography(np.zeros((3, 2)), _square())


def law6_quad_non_finite() -> None:
    quad = _square()
    quad[0, 0] = np.inf
    with pytest.raises(DegenerateHomographyError):
        compute_planar_quad_homography(quad, _square())


def law6_unsupported_glyph() -> None:
    with pytest.raises(TypographyError):
        render_homography_locked_diegetic_text("@", _square(), (64, 64))


def law6_bad_canvas_shape() -> None:
    with pytest.raises(TypographyError):
        render_homography_locked_diegetic_text("A", _square(), (0, 64))


def law6_negative_illumination() -> None:
    with pytest.raises(TypographyError):
        render_homography_locked_diegetic_text("A", _square(), (64, 64), illumination=-1.0)


# --------------------------------------------------------------------------- #
# Law 7 — symplectic contact mechanics
# --------------------------------------------------------------------------- #
def _bodies() -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    positions = np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0]])
    velocities = np.zeros((2, 3))
    masses = np.array([1.0, 1.0])
    radii = np.array([0.6, 0.6])
    return positions, velocities, masses, radii


def law7_restitution_above_one() -> None:
    positions, velocities, masses, radii = _bodies()
    with pytest.raises(MechanicsViolationError):
        resolve_rigid_body_collision(positions, velocities, masses, radii, restitution=1.2)


def law7_negative_friction() -> None:
    positions, velocities, masses, radii = _bodies()
    with pytest.raises(MechanicsViolationError):
        resolve_rigid_body_collision(positions, velocities, masses, radii, friction=-1.0)


def law7_bad_position_shape() -> None:
    positions, velocities, masses, radii = _bodies()
    with pytest.raises(MechanicsViolationError):
        resolve_rigid_body_collision(np.zeros((2, 2)), velocities, masses, radii)


def law7_non_positive_mass() -> None:
    positions, velocities, masses, radii = _bodies()
    with pytest.raises(MechanicsViolationError):
        resolve_rigid_body_collision(positions, velocities, np.array([1.0, -1.0]), radii)


def law7_zero_time_step() -> None:
    positions, velocities, masses, _ = _bodies()
    with pytest.raises(MechanicsViolationError):
        step_symplectic_verlet(positions, velocities, masses, np.zeros((2, 3)), 0.0)


def law7_zero_iterations() -> None:
    positions, velocities, masses, radii = _bodies()
    with pytest.raises(MechanicsViolationError):
        resolve_rigid_body_collision(positions, velocities, masses, radii, iterations=0)


# --------------------------------------------------------------------------- #
# Law 8 — XPBD cloth
# --------------------------------------------------------------------------- #
def _cloth() -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    positions = np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [2.0, 0.0, 0.0], [3.0, 0.0, 0.0]])
    velocities = np.zeros((4, 3))
    edges = np.array([[0, 1], [1, 2], [2, 3]])
    return positions, velocities, edges


def law8_bad_capsule_points() -> None:
    with pytest.raises(ClothSimulationError):
        capsule_sdf(np.zeros((2, 2)), np.zeros(3), np.ones(3), 0.1)


def law8_non_positive_capsule_radius() -> None:
    with pytest.raises(ClothSimulationError):
        capsule_sdf(np.zeros((2, 3)), np.zeros(3), np.ones(3), 0.0)


def law8_coincident_capsule_endpoints() -> None:
    with pytest.raises(ClothSimulationError):
        capsule_sdf(np.zeros((2, 3)), np.zeros(3), np.zeros(3), 0.1)


def law8_edge_index_out_of_range() -> None:
    positions, velocities, _ = _cloth()
    with pytest.raises(ClothSimulationError):
        step_xpbd_cloth_simulation(positions, velocities, np.array([[0, 99]]))


def law8_self_edge() -> None:
    positions, velocities, _ = _cloth()
    with pytest.raises(ClothSimulationError):
        step_xpbd_cloth_simulation(positions, velocities, np.array([[0, 0]]))


def law8_negative_compliance() -> None:
    positions, velocities, edges = _cloth()
    with pytest.raises(ClothSimulationError):
        step_xpbd_cloth_simulation(positions, velocities, edges, compliance=-1.0)


# --------------------------------------------------------------------------- #
# Law 9 — thermodynamic arrow
# --------------------------------------------------------------------------- #
def law9_frames_not_three_dimensional() -> None:
    with pytest.raises(ThermodynamicError):
        audit_and_enforce_thermodynamic_arrow(np.zeros((4, 4)))


def law9_negative_frames() -> None:
    frames = np.zeros((3, 4, 4))
    frames[0, 0, 0] = -1.0
    with pytest.raises(ThermodynamicError):
        audit_and_enforce_thermodynamic_arrow(frames)


def law9_negative_diffusivity() -> None:
    with pytest.raises(ThermodynamicError):
        fourier_advection_diffusion(np.zeros((4, 4)), (0.0, 0.0), -1.0, 1.0, 1.0)


def law9_negative_buoyancy() -> None:
    with pytest.raises(ThermodynamicError):
        audit_and_enforce_thermodynamic_arrow(np.zeros((3, 4, 4)), buoyancy=-1.0)


def law9_negative_tolerance() -> None:
    with pytest.raises(ThermodynamicError):
        audit_and_enforce_thermodynamic_arrow(np.zeros((3, 4, 4)), tolerance=-1.0)


def law9_reversed_entropy_is_healed() -> None:
    rng = np.random.default_rng(7)
    noisy = rng.random((16, 16))
    smooth = np.clip(fourier_advection_diffusion(noisy, (0.0, 0.0), 0.5, 1.0, 1.0), 0.0, None)
    frames = np.stack([smooth, noisy])
    audit = audit_and_enforce_thermodynamic_arrow(frames)
    assert audit.violations_before
    _assert_healed(audit)


# --------------------------------------------------------------------------- #
# Law 10 — facial microdynamics
# --------------------------------------------------------------------------- #
def law10_facs_wrong_shape() -> None:
    with pytest.raises(FacialDynamicsError):
        enforce_facs_synergy_manifold(np.zeros((2, 5)))


def law10_negative_action_unit() -> None:
    units = np.zeros((2, 12))
    units[0, 0] = -1.0
    with pytest.raises(FacialDynamicsError):
        enforce_facs_synergy_manifold(units)


def law10_non_finite_action_unit() -> None:
    units = np.zeros((2, 12))
    units[0, 0] = np.nan
    with pytest.raises(FacialDynamicsError):
        enforce_facs_synergy_manifold(units)


def law10_zero_blink_duration() -> None:
    with pytest.raises(FacialDynamicsError):
        synthesize_asymmetric_blink_curve(0.0)


def law10_levator_slower_than_closure() -> None:
    with pytest.raises(FacialDynamicsError):
        synthesize_asymmetric_blink_curve(0.5, tau_down_s=0.2, tau_up_s=0.1)


def law10_bad_diffuse_shape() -> None:
    with pytest.raises(FacialDynamicsError):
        apply_dermal_subsurface_scattering(np.zeros((4, 4)), np.zeros((4, 4)), np.zeros((4, 4)))


PROBES: list[Callable[[], None]] = [
    law1_six_ray_polydactyly,
    law1_backwards_mcp_hyperextension,
    law1_tendon_coupling_violation,
    law1_bone_ratio_violation,
    law1_truncated_skeleton_fails_closed,
    law1_grip_penetration_is_projected,
    law2_divergent_gaze_is_healed,
    law2_skew_gaze_is_healed,
    law2_ipd_out_of_range,
    law2_degenerate_optical_axis,
    law2_negative_saccade_duration,
    law2_negative_pupil_luminance,
    law3_non_unit_mirror_normal,
    law3_zero_mirror_normal,
    law3_zero_incident_index,
    law3_negative_refracted_index,
    law3_incident_angle_out_of_range,
    law3_ripple_on_one_dimensional_image,
    law4_light_not_homogeneous,
    law4_light_bad_w_component,
    law4_plane_zero_normal,
    law4_negative_emitter_radius,
    law4_occluder_above_light,
    law4_bad_vertex_shape,
    law5_bad_pose_shape,
    law5_non_positive_mass,
    law5_bad_twist_shape,
    law5_bad_extents_shape,
    law5_non_finite_pose,
    law5_teleportation_is_healed,
    law6_collinear_quad_is_degenerate,
    law6_quad_wrong_shape,
    law6_quad_non_finite,
    law6_unsupported_glyph,
    law6_bad_canvas_shape,
    law6_negative_illumination,
    law7_restitution_above_one,
    law7_negative_friction,
    law7_bad_position_shape,
    law7_non_positive_mass,
    law7_zero_time_step,
    law7_zero_iterations,
    law8_bad_capsule_points,
    law8_non_positive_capsule_radius,
    law8_coincident_capsule_endpoints,
    law8_edge_index_out_of_range,
    law8_self_edge,
    law8_negative_compliance,
    law9_frames_not_three_dimensional,
    law9_negative_frames,
    law9_negative_diffusivity,
    law9_negative_buoyancy,
    law9_negative_tolerance,
    law9_reversed_entropy_is_healed,
    law10_facs_wrong_shape,
    law10_negative_action_unit,
    law10_non_finite_action_unit,
    law10_zero_blink_duration,
    law10_levator_slower_than_closure,
    law10_bad_diffuse_shape,
]


@pytest.mark.parametrize("probe", PROBES, ids=[p.__name__ for p in PROBES])
def test_falsification_probe(probe: Callable[[], None]) -> None:
    probe()


def test_probe_count_is_sixty() -> None:
    assert len(PROBES) == 60


# --------------------------------------------------------------------------- #
# Law 11 — Madelung quantum-fluid phase coherence
# --------------------------------------------------------------------------- #
def test_quantum_potential_vanishes_on_uniform_density() -> None:
    potential = bohmian_quantum_potential(np.ones((8, 8)))
    assert np.allclose(potential, 0.0, atol=1e-9)


def test_quantum_potential_spikes_on_edge() -> None:
    density = np.ones((16, 16))
    density[:, 8:] = 4.0
    potential = bohmian_quantum_potential(density)
    edge_energy = np.abs(potential[:, 7:9]).sum()
    interior_energy = np.abs(potential[:, 1:5]).sum()
    assert edge_energy > interior_energy


@pytest.mark.parametrize(
    "density",
    [np.ones(4), -np.ones((4, 4)), np.full((4, 4), np.nan)],
)
def test_quantum_potential_rejects_invalid_density(density: np.ndarray) -> None:
    with pytest.raises(ValueError):
        bohmian_quantum_potential(density)


def test_quantum_potential_rejects_bad_hbar() -> None:
    with pytest.raises(ValueError):
        bohmian_quantum_potential(np.ones((4, 4)), hbar_eff=0.0)


def test_continuity_residual_zero_for_uniform_phase() -> None:
    density = np.ones((8, 8))
    phase = np.zeros((8, 8))
    assert madelung_continuity_residual(density, phase) == pytest.approx(0.0, abs=1e-12)


@pytest.mark.parametrize(
    "density, phase, dt",
    [
        (np.ones((4, 4)), np.ones((4, 5)), 1.0),
        (np.ones(4), np.ones(4), 1.0),
        (np.ones((4, 4)), np.ones((4, 4)), 0.0),
    ],
)
def test_continuity_residual_rejects_invalid_inputs(
    density: np.ndarray, phase: np.ndarray, dt: float
) -> None:
    with pytest.raises(ValueError):
        madelung_continuity_residual(density, phase, dt=dt)


def test_stabilize_clean_smooth_phase_reports_no_violation() -> None:
    density = np.ones((16, 16))
    phase = np.zeros((16, 16))
    result = stabilize_madelung_phase_coherence(density, phase)
    assert result.violations == ()
    assert result.boiling_after <= result.boiling_before


def test_stabilize_boiling_phase_is_healed() -> None:
    rng = np.random.default_rng(0)
    density = np.ones((16, 16))
    phase = rng.normal(scale=3.0, size=(16, 16))
    result = stabilize_madelung_phase_coherence(density, phase)
    assert "temporal_phase_boiling" in result.violations
    assert result.healed is True
    assert result.boiling_after < result.boiling_before
    assert result.iterations >= 1


def test_stabilize_detects_phase_discontinuity() -> None:
    density = np.ones((8, 8))
    phase = np.zeros((8, 8))
    phase[:, 4:] = 2.0 * np.pi
    result = stabilize_madelung_phase_coherence(density, phase)
    assert "phase_discontinuity" in result.violations


def test_stabilize_wavefunction_modulus_matches_density() -> None:
    density = np.full((8, 8), 4.0)
    phase = np.zeros((8, 8))
    result = stabilize_madelung_phase_coherence(density, phase)
    assert np.allclose(np.abs(result.wavefunction) ** 2, density, atol=1e-9)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"hbar_eff": 0.0},
        {"boiling_threshold": 0.0},
        {"max_iterations": -1},
        {"smoothing_cutoff": 0},
    ],
)
def test_stabilize_rejects_bad_parameters(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        stabilize_madelung_phase_coherence(np.ones((8, 8)), np.zeros((8, 8)), **kwargs)


def test_stabilize_rejects_shape_mismatch() -> None:
    with pytest.raises(ValueError):
        stabilize_madelung_phase_coherence(np.ones((8, 8)), np.zeros((8, 9)))


def test_stabilize_rejects_negative_density() -> None:
    with pytest.raises(ValueError):
        stabilize_madelung_phase_coherence(-np.ones((8, 8)), np.zeros((8, 8)))


# --------------------------------------------------------------------------- #
# Property tests across the consolidated facades
# --------------------------------------------------------------------------- #
def test_aliases_are_identical_to_canonical_engines() -> None:
    assert propagate_occluded_object_se3 is _propagate
    assert step_symplectic_verlet is _stormer
    assert verify_retarded_wave_causality is _retarded


def test_householder_reflection_is_involution_with_negative_determinant() -> None:
    matrix = compute_householder_reflection_pose(np.array([0.0, 0.0, 1.0]), 0.0)
    assert np.allclose(matrix @ matrix, np.eye(4), atol=1e-12)
    assert np.linalg.det(matrix[:3, :3]) == pytest.approx(-1.0)


def test_fresnel_reflectance_is_zero_for_p_at_brewster() -> None:
    brewster = np.degrees(np.arctan(1.5 / 1.0))
    result = compute_fresnel_reflectance_rgb(brewster, 1.0, 1.5)
    assert np.min(result.reflectance_p) < 1e-9


def test_shadow_matrix_projects_vertices_onto_plane() -> None:
    light = np.array([0.0, 0.0, 5.0, 1.0])
    plane = np.array([0.0, 0.0, 1.0, 0.0])
    compute_shadow_projection_matrix(light, plane)
    vertices = np.array([[1.0, 1.0, 1.0], [-1.0, 0.5, 2.0]])
    field = synthesize_coherent_cast_shadows(vertices, light, plane)
    assert field.shadow_vertices.shape == vertices.shape
    assert np.allclose(field.shadow_vertices[:, 2], 0.0, atol=1e-9)


def test_symplectic_step_conserves_linear_momentum() -> None:
    positions = np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0]])
    velocities = np.array([[1.0, 0.0, 0.0], [-1.0, 0.0, 0.0]])
    masses = np.array([1.0, 1.0])
    forces = np.zeros((2, 3))
    before = total_linear_momentum(velocities, masses)
    new_velocities = step_symplectic_verlet(positions, velocities, masses, forces, 0.01)[1]
    after = total_linear_momentum(new_velocities, masses)
    assert np.allclose(before, after, atol=1e-12)


def test_capsule_sdf_is_zero_on_surface() -> None:
    start = np.array([0.0, 0.0, 0.0])
    end = np.array([0.0, 0.0, 1.0])
    point = np.array([[0.5, 0.0, 0.5]])
    assert capsule_sdf(point, start, end, 0.5)[0] == pytest.approx(0.0, abs=1e-9)


def test_homography_maps_source_quad_to_destination() -> None:
    destination = np.array([[2.0, 3.0], [5.0, 3.0], [5.0, 7.0], [2.0, 7.0]])
    homography = compute_planar_quad_homography(_square(), destination)
    assert homography.shape == (3, 3)
    assert np.linalg.det(homography) == pytest.approx(1.0, abs=1e-9)
