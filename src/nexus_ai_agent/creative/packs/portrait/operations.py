"""Declarative registrar for the ``nexus.portrait.refine`` pack.

The pack directory stays declarative: typed models, typed errors, a manifest
and this registration table.  Executing the image math requires the NumPy
kernels in the composition zone, so the registered handlers are thin
*fail-closed delegates* (the audio-pack pattern): they look for an injected
kernel on the operation context (``context.portrait_kernel``) and refuse the
operation honestly — never simulated — when none is composed.

The reference implementation is
:class:`nexus_ai_agent.creative.composition.portrait_ops.NativePortraitKernel`.
Tests and tools that want the real kernel without a command bus can build one
up registry via
:func:`nexus_ai_agent.creative.composition.portrait_ops.build_portrait_registry`,
which registers the same specs with the kernels wired directly.

``PORTRAIT_SPECS`` is the single declaration source for the operation table
(name → input model + description); the composition zone imports it, so the
pack catalogue and the native kernel cannot drift apart. The ``OPERATION_*``
re-exports keep ``models.py`` the single declaration source of truth for the
operation-truth scanner — they are not a second declaration.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from pydantic import BaseModel

from nexus_ai_agent.creative.packs.portrait.models import (
    DOMAIN,
    OPERATION_AGING_DEAGING,
    OPERATION_DOF_BOKEH,
    OPERATION_EYEBLINK_SYNTHESIS,
    OPERATION_FACIAL_HAIR_CONTINUITY,
    OPERATION_FACIAL_RELIGHTING,
    OPERATION_GAZE_REDIRECTION,
    OPERATION_HEAD_REPOSE_3D,
    OPERATION_MICRO_EXPRESSION_MORPH,
    OPERATION_RELIGHT_SPHERICAL_HARMONICS,
    OPERATION_SKINTONE_HARMONIZATION,
    OPERATION_TEETH_WHITENING,
    PORTRAIT_PACKAGE_ID,
    AgingInput,
    DepthOfFieldBokehInput,
    EyeblinkInput,
    FacialHairInput,
    FacialRelightingInput,
    GazeRedirectionInput,
    HeadReposeInput,
    MicroExpressionInput,
    SkintoneInput,
    SphericalHarmonicsRelightInput,
    TeethWhiteningInput,
)
from nexus_ai_agent.creative.studio.capabilities import (
    CapabilityRegistry,
    OperationContext,
    OperationOutcome,
    OperationSpec,
)
from nexus_ai_agent.creative.studio.models import CommandValidationError, PermissionLevel, Project

if TYPE_CHECKING:
    from nexus_ai_agent.creative.studio.portrait_kernel import PortraitKernel

#: Failure code recorded when no native kernel is composed.
CODE_REQUIRES_PORTRAIT_KERNEL = "PORTRAIT_KERNEL_REQUIRED"

#: Local name (after ``portrait.``) → (input model, human description).
#: Single source of truth for both the pack registrar and the composition
#: kernels; do not duplicate these tuples elsewhere.
PORTRAIT_SPECS: dict[str, tuple[type[BaseModel], str]] = {
    "gaze_redirection": (GazeRedirectionInput, "Redirect the gaze inside the eye mask."),
    "eyeblink_synthesis": (EyeblinkInput, "Deterministic sin^2 blink closure."),
    "facial_relighting_gradient": (
        FacialRelightingInput,
        "Lambertian re-lighting from an estimated normal.",
    ),
    "skintone_harmonization": (
        SkintoneInput,
        "Constrain the skin region to the CIE L*a*b* skin locus.",
    ),
    "micro_expression_morph": (
        MicroExpressionInput,
        "Thin-plate-spline micro-expression warp.",
    ),
    "teeth_whitening": (
        TeethWhiteningInput,
        "Adaptive desaturation inside the teeth mask.",
    ),
    "facial_hair_continuity": (
        FacialHairInput,
        "Gabor-filtered stubble density held across shots.",
    ),
    "head_repose_3d": (HeadReposeInput, "Weak-perspective head re-pose (+/-15 deg)."),
    "depth_of_field_bokeh": (
        DepthOfFieldBokehInput,
        "Depth-driven polygonal-aperture bokeh.",
    ),
    "aging_deaging_geometry": (
        AgingInput,
        "Metric landmark displacement for aging/de-aging.",
    ),
    "relight_spherical_harmonics": (
        SphericalHarmonicsRelightInput,
        "L2 spherical-harmonics radiative-transfer portrait relight.",
    ),
}


def _kernel(context: OperationContext, operation_id: str) -> PortraitKernel:
    kernel = context.portrait_kernel
    if kernel is None:
        raise CommandValidationError(
            f"{operation_id} requires the native portrait kernel; none was composed "
            f"into this command bus (code {CODE_REQUIRES_PORTRAIT_KERNEL}). "
            "Portrait rendering is not simulated: inject NativePortraitKernel "
            "(or an in-memory double in tests) as context.portrait_kernel to execute "
            "this operation."
        )
    return kernel


def _delegate(operation_id: str) -> Callable[[Project, OperationContext], OperationOutcome]:
    def handler(project: Project, context: OperationContext) -> OperationOutcome:
        return _kernel(context, operation_id).execute(operation_id, project, context)

    handler.__name__ = f"handle_{operation_id.split('.', 1)[1]}"
    handler.__qualname__ = handler.__name__
    return handler


def register_portrait_operations(registry: CapabilityRegistry) -> CapabilityRegistry:
    """Register the ``portrait.*`` operations on ``registry`` (idempotent)."""
    for name, (model, description) in PORTRAIT_SPECS.items():
        operation_id = f"{DOMAIN}.{name}"
        try:
            registry.get_spec(operation_id)
            continue
        except Exception:
            pass
        registry.register_operation(
            DOMAIN,
            name,
            OperationSpec(
                operation_id=operation_id,
                description=description,
                permission_level=PermissionLevel.REVERSIBLE,
                input_model=model,
                handler=_delegate(operation_id),
                required_packs=(PORTRAIT_PACKAGE_ID,),
                deterministic=True,
            ),
        )
    return registry


def build_portrait_registry() -> CapabilityRegistry:
    """A base registry pre-loaded with the ``portrait.refine`` operations.

    The registered handlers fail closed until a native kernel is injected
    (``context.portrait_kernel``); for direct kernel-backed execution use
    :func:`nexus_ai_agent.creative.composition.portrait_ops.build_portrait_registry`.
    """
    from nexus_ai_agent.creative.studio.capabilities import build_wave1_registry

    return register_portrait_operations(build_wave1_registry())


__all__ = [
    "PORTRAIT_SPECS",
    "CODE_REQUIRES_PORTRAIT_KERNEL",
    "register_portrait_operations",
    "build_portrait_registry",
    "OPERATION_GAZE_REDIRECTION",
    "OPERATION_EYEBLINK_SYNTHESIS",
    "OPERATION_FACIAL_RELIGHTING",
    "OPERATION_SKINTONE_HARMONIZATION",
    "OPERATION_MICRO_EXPRESSION_MORPH",
    "OPERATION_TEETH_WHITENING",
    "OPERATION_FACIAL_HAIR_CONTINUITY",
    "OPERATION_HEAD_REPOSE_3D",
    "OPERATION_DOF_BOKEH",
    "OPERATION_AGING_DEAGING",
    "OPERATION_RELIGHT_SPHERICAL_HARMONICS",
]
