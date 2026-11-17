"""Consolidated catoptric and projective optics — Laws 3, 4 and 6.

Single import surface for the optical layer of NAGAR VERITAS, aggregating:

* :mod:`catoptric_reflection_physics` — Law 3, exact affine Householder mirror
  reflection, epipolar consistency and dielectric Fresnel polarisation.
* :mod:`projective_shadow_transport` — Law 4, the projective shadow matrix,
  extended-emitter penumbra geometry and contact ambient occlusion.
* :mod:`homography_diegetic_typography` — Law 6, the normalised DLT planar
  homography in SL(3) and Jacobian-footprint anisotropic glyph filtering.

All symbols are re-exported unchanged.
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
from nexus_ai_agent.creative.veritas.homography_diegetic_typography import (
    DegenerateHomographyError,
    DiegeticTextRender,
    TypographyError,
    compute_planar_quad_homography,
    procedural_glyph_bitmap,
    render_homography_locked_diegetic_text,
)
from nexus_ai_agent.creative.veritas.projective_shadow_transport import (
    ShadowField,
    ShadowGeometryError,
    compute_shadow_projection_matrix,
    penumbra_radius,
    project_vertices_to_shadow,
    synthesize_coherent_cast_shadows,
)

__all__ = [
    "CatoptricGeometryError",
    "DegenerateHomographyError",
    "DiegeticTextRender",
    "FresnelResult",
    "ReflectionConsistency",
    "ShadowField",
    "ShadowGeometryError",
    "TypographyError",
    "compute_fresnel_reflectance_rgb",
    "compute_householder_reflection_pose",
    "compute_planar_quad_homography",
    "compute_shadow_projection_matrix",
    "penumbra_radius",
    "procedural_glyph_bitmap",
    "project_vertices_to_shadow",
    "render_homography_locked_diegetic_text",
    "synthesize_coherent_cast_shadows",
    "verify_reflection_epipolar_consistency",
    "warp_ripple_reflection",
]
