"""NAGAR VERITAS — Neuro-Physical Reality & Anti-Hallucination Cinema Engine.

A deterministic, zero-external-API, 100% vectorised NumPy engine that detects,
proves and repairs the ten most infamous failure modes of AI filmmaking. Every
law is exported here so the repository orphan-module architecture gate sees the
whole package as reachable.
"""

from __future__ import annotations

from nexus_ai_agent.creative.veritas.catoptric_reflection_physics import (
    CatoptricGeometryError,
    FresnelResult,
    ReflectionConsistency,
    compute_fresnel_reflectance_rgb,
    compute_householder_reflection_pose,
    verify_reflection_epipolar_consistency,
    warp_ripple_reflection,
)
from nexus_ai_agent.creative.veritas.hand_kinematics import (
    CANONICAL_JOINT_COUNT,
    CANONICAL_RAYS,
    HandAuditResult,
    HandSkeleton,
    audit_and_project_hand_skeleton,
    canonical_hand_skeleton,
    hand_violation_score,
    spectral_laplacian_signature,
)
from nexus_ai_agent.creative.veritas.homography_diegetic_typography import (
    DegenerateHomographyError,
    DiegeticTextRender,
    TypographyError,
    compute_planar_quad_homography,
    procedural_glyph_bitmap,
    render_homography_locked_diegetic_text,
)
from nexus_ai_agent.creative.veritas.neuro_facial_microdynamics import (
    BlinkCurve,
    FacsResult,
    apply_dermal_subsurface_scattering,
    enforce_facs_synergy_manifold,
    synthesize_asymmetric_blink_curve,
)
from nexus_ai_agent.creative.veritas.ocular_biophysics import (
    OcularGeometryError,
    PupilResponse,
    PupilStimulusError,
    SaccadeTrajectory,
    VergenceSolution,
    compute_watson_yellott_pupil_diameter,
    solve_binocular_vergence,
    synthesize_saccade_vor_trajectory,
)
from nexus_ai_agent.creative.veritas.projective_shadow_transport import (
    ShadowField,
    compute_shadow_projection_matrix,
    penumbra_radius,
    project_vertices_to_shadow,
    synthesize_coherent_cast_shadows,
)
from nexus_ai_agent.creative.veritas.reality_auditor import (
    CinemaShot,
    VeritasRealityAuditor,
    VeritasRealityPassport,
)
from nexus_ai_agent.creative.veritas.se3_object_permanence import (
    ObjectState,
    PermanenceAudit,
    audit_object_permanence,
    propagate_occluded_object,
    repair_object_permanence,
    se3_exp,
    se3_log,
)
from nexus_ai_agent.creative.veritas.symplectic_contact_mechanics import (
    CollisionResult,
    MechanicsViolationError,
    kinetic_energy,
    resolve_rigid_body_collision,
    stormer_verlet_step,
    total_angular_momentum,
    total_linear_momentum,
)
from nexus_ai_agent.creative.veritas.thermodynamic_arrow_of_time import (
    ArrowAudit,
    audit_and_enforce_thermodynamic_arrow,
    fourier_advection_diffusion,
    retarded_wavefront,
    spatial_entropy,
    verify_retarded_causality,
)
from nexus_ai_agent.creative.veritas.xpbd_cloth_drape import (
    XpbdResult,
    capsule_sdf,
    step_xpbd_cloth_simulation,
)

__all__ = [
    "CANONICAL_JOINT_COUNT",
    "CANONICAL_RAYS",
    "ArrowAudit",
    "BlinkCurve",
    "CatoptricGeometryError",
    "CinemaShot",
    "CollisionResult",
    "DegenerateHomographyError",
    "DiegeticTextRender",
    "FacsResult",
    "FresnelResult",
    "HandAuditResult",
    "HandSkeleton",
    "MechanicsViolationError",
    "ObjectState",
    "OcularGeometryError",
    "PermanenceAudit",
    "PupilResponse",
    "PupilStimulusError",
    "ReflectionConsistency",
    "SaccadeTrajectory",
    "ShadowField",
    "TypographyError",
    "VergenceSolution",
    "VeritasRealityAuditor",
    "VeritasRealityPassport",
    "XpbdResult",
    "apply_dermal_subsurface_scattering",
    "audit_and_enforce_thermodynamic_arrow",
    "audit_and_project_hand_skeleton",
    "audit_object_permanence",
    "canonical_hand_skeleton",
    "capsule_sdf",
    "compute_fresnel_reflectance_rgb",
    "compute_householder_reflection_pose",
    "compute_planar_quad_homography",
    "compute_shadow_projection_matrix",
    "compute_watson_yellott_pupil_diameter",
    "enforce_facs_synergy_manifold",
    "fourier_advection_diffusion",
    "hand_violation_score",
    "kinetic_energy",
    "penumbra_radius",
    "procedural_glyph_bitmap",
    "propagate_occluded_object",
    "project_vertices_to_shadow",
    "render_homography_locked_diegetic_text",
    "repair_object_permanence",
    "resolve_rigid_body_collision",
    "retarded_wavefront",
    "se3_exp",
    "se3_log",
    "solve_binocular_vergence",
    "spatial_entropy",
    "spectral_laplacian_signature",
    "step_xpbd_cloth_simulation",
    "stormer_verlet_step",
    "synthesize_asymmetric_blink_curve",
    "synthesize_coherent_cast_shadows",
    "synthesize_saccade_vor_trajectory",
    "total_angular_momentum",
    "total_linear_momentum",
    "verify_reflection_epipolar_consistency",
    "verify_retarded_causality",
    "warp_ripple_reflection",
]
