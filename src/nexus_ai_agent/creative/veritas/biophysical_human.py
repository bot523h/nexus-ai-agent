"""Consolidated biophysical human manifold — Laws 1, 2 and 10.

This module is the single import surface for the neuro-physical human layer of
NAGAR VERITAS. It aggregates the three verified sub-engines:

* :mod:`hand_kinematics` — Law 1, the 21-joint topological hand graph, the
  Rijpkema distal tendon coupling manifold and the anatomical joint cones.
* :mod:`ocular_biophysics` — Law 2, binocular vergence, the Bahill main
  sequence, the vestibulo-ocular reflex and the Watson-Yellott pupil reflex.
* :mod:`neuro_facial_microdynamics` — Law 10, Ekman FACS synergy, asymmetric
  blink kinetics and dermal subsurface scattering.

Every symbol is re-exported unchanged so downstream code can depend on a single
stable namespace without duplicating physics.
"""

from __future__ import annotations

from nexus_ai_agent.creative.veritas.hand_kinematics import (
    CANONICAL_JOINT_COUNT,
    CANONICAL_RAYS,
    HandAuditResult,
    HandSkeleton,
    HandTopologyError,
    audit_and_project_hand_skeleton,
    canonical_hand_skeleton,
    hand_violation_score,
    spectral_laplacian_signature,
)
from nexus_ai_agent.creative.veritas.neuro_facial_microdynamics import (
    BlinkCurve,
    FacialDynamicsError,
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

__all__ = [
    "CANONICAL_JOINT_COUNT",
    "CANONICAL_RAYS",
    "BlinkCurve",
    "FacsResult",
    "FacialDynamicsError",
    "HandAuditResult",
    "HandSkeleton",
    "HandTopologyError",
    "OcularGeometryError",
    "PupilResponse",
    "PupilStimulusError",
    "SaccadeTrajectory",
    "VergenceSolution",
    "apply_dermal_subsurface_scattering",
    "audit_and_project_hand_skeleton",
    "canonical_hand_skeleton",
    "compute_watson_yellott_pupil_diameter",
    "enforce_facs_synergy_manifold",
    "hand_violation_score",
    "solve_binocular_vergence",
    "spectral_laplacian_signature",
    "synthesize_asymmetric_blink_curve",
    "synthesize_saccade_vor_trajectory",
]
