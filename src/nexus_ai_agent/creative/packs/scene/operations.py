"""Declarative registrar for the ``nexus.scene.imaging`` pack.

The pack directory stays declarative: typed models, typed errors, a manifest
and this registration table.  Executing the image math requires the NumPy
kernels in the composition zone, so the registered handlers are thin
*fail-closed delegates* (the audio-pack pattern): they look for an injected
kernel on the operation context (``context.scene_kernel``) and refuse the
operation honestly — never simulated — when none is composed.

The reference implementation is
:class:`nexus_ai_agent.creative.composition.scene_ops.NativeSceneKernel`.
Tests and tools that want the real kernels without a command bus can build a
registry via
:func:`nexus_ai_agent.creative.composition.scene_ops.build_scene_registry`,
which registers the same specs with the kernels wired directly.

``SCENE_SPECS`` is the single declaration source for the operation table
(name → input model + description); the composition zone imports it, so the
pack catalogue and the native kernels cannot drift apart. The
``OPERATION_*`` re-exports keep ``models.py`` the single declaration source
of truth for the operation-truth scanner — they are not a second declaration.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from pydantic import BaseModel

from nexus_ai_agent.creative.packs.scene.models import (
    DOMAIN,
    OPERATION_ADD_VOLUMETRIC_FOG,
    OPERATION_CHROMATIC_ABERRATION_CONTROL,
    OPERATION_FILM_EMULATION,
    OPERATION_HARMONIZE_RADIATIVE_TRANSFER,
    OPERATION_INSERT_OBJECT_WITH_SHADOW,
    OPERATION_LENS_DISTORTION_CORRECT,
    OPERATION_OUTPAINT_CANVAS_EXPAND,
    OPERATION_PERSPECTIVE_HORIZON_LEVEL,
    OPERATION_REFLECTION_WATER_GLASS,
    OPERATION_REMOVE_OBJECT_INPAINT,
    OPERATION_RETIME_OPTICAL_FLOW,
    OPERATION_SKY_REPLACEMENT_RELIT,
    OPERATION_SYNTHESIZE_DOF,
    OPERATION_TONEMAP_HDR,
    SCENE_PACKAGE_ID,
    ChromaticAberrationInput,
    DepthOfFieldInput,
    FilmEmulationInput,
    FogHazeInput,
    HdrTonemapInput,
    HorizonLevelInput,
    InsertObjectInput,
    LensDistortionInput,
    OpticalFlowRetimeInput,
    OutpaintInput,
    RadiativeTransferHarmonizeInput,
    ReflectionInput,
    RemoveObjectInput,
    SkyRelitInput,
)
from nexus_ai_agent.creative.studio.capabilities import (
    CapabilityRegistry,
    OperationContext,
    OperationOutcome,
    OperationSpec,
)
from nexus_ai_agent.creative.studio.models import CommandValidationError, PermissionLevel, Project

if TYPE_CHECKING:
    from nexus_ai_agent.creative.studio.scene_kernel import SceneKernel

#: Failure code recorded when no native kernel is composed.
CODE_REQUIRES_SCENE_KERNEL = "SCENE_KERNEL_REQUIRED"

#: Local name (after ``scene.``) → (input model, human description).
#: Single source of truth for both the pack registrar and the composition
#: kernels; do not duplicate these tuples elsewhere.
SCENE_SPECS: dict[str, tuple[type[BaseModel], str]] = {
    "insert_object_with_shadow": (
        InsertObjectInput,
        "Insert a premultiplied-alpha object with a contact shadow.",
    ),
    "remove_object_inpaint": (
        RemoveObjectInput,
        "Remove an object with multiscale harmonic inpainting.",
    ),
    "outpaint_canvas_expand": (
        OutpaintInput,
        "Expand the canvas with a mirrored Laplacian-pyramid border.",
    ),
    "synthesize_depth_of_field": (
        DepthOfFieldInput,
        "Depth-driven circle-of-confusion defocus.",
    ),
    "add_volumetric_fog_haze": (
        FogHazeInput,
        "Koschmieder atmospheric scattering with HG phase.",
    ),
    "lens_distortion_correct": (LensDistortionInput, "Brown-Conrady radial undistortion."),
    "chromatic_aberration_control": (
        ChromaticAberrationInput,
        "Per-channel radial magnification for lateral CA.",
    ),
    "perspective_horizon_level": (
        HorizonLevelInput,
        "Estimate and level the dominant horizon.",
    ),
    "sky_replacement_relit": (
        SkyRelitInput,
        "Replace the sky and re-light the foreground.",
    ),
    "reflection_water_glass_synth": (
        ReflectionInput,
        "Flat mirror reflection with Fresnel and ripples.",
    ),
    "harmonize_radiative_transfer": (
        RadiativeTransferHarmonizeInput,
        "L2 spherical-harmonics radiative-transfer relight of a composite.",
    ),
    "retime_optical_flow": (
        OpticalFlowRetimeInput,
        "Flow-based interpolation of a frame between two keyframes (VFR retime).",
    ),
    "tonemap_hdr": (
        HdrTonemapInput,
        "Scene-linear HDR tone mapping with optional white-point adaptation.",
    ),
    "film_emulation": (
        FilmEmulationInput,
        "Photochemical gate weave, halation and film grain.",
    ),
}


def _kernel(context: OperationContext, operation_id: str) -> SceneKernel:
    kernel = context.scene_kernel
    if kernel is None:
        raise CommandValidationError(
            f"{operation_id} requires the native scene kernel; none was composed "
            f"into this command bus (code {CODE_REQUIRES_SCENE_KERNEL}). "
            "Scene rendering is not simulated: inject NativeSceneKernel "
            "(or an in-memory double in tests) as context.scene_kernel to execute "
            "this operation."
        )
    return kernel


def _delegate(operation_id: str) -> Callable[[Project, OperationContext], OperationOutcome]:
    def handler(project: Project, context: OperationContext) -> OperationOutcome:
        return _kernel(context, operation_id).execute(operation_id, project, context)

    handler.__name__ = f"handle_{operation_id.split('.', 1)[1]}"
    handler.__qualname__ = handler.__name__
    return handler


def register_scene_operations(registry: CapabilityRegistry) -> CapabilityRegistry:
    """Register the ``scene.*`` operations on ``registry`` (idempotent)."""
    for name, (model, description) in SCENE_SPECS.items():
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
                required_packs=(SCENE_PACKAGE_ID,),
                deterministic=True,
            ),
        )
    return registry


def build_scene_registry() -> CapabilityRegistry:
    """A base registry pre-loaded with the ``scene.imaging`` operations.

    The registered handlers fail closed until a native kernel is injected
    (``context.scene_kernel``); for direct kernel-backed execution use
    :func:`nexus_ai_agent.creative.composition.scene_ops.build_scene_registry`.
    """
    from nexus_ai_agent.creative.studio.capabilities import build_wave1_registry

    return register_scene_operations(build_wave1_registry())


__all__ = [
    "SCENE_SPECS",
    "CODE_REQUIRES_SCENE_KERNEL",
    "register_scene_operations",
    "build_scene_registry",
    "OPERATION_INSERT_OBJECT_WITH_SHADOW",
    "OPERATION_REMOVE_OBJECT_INPAINT",
    "OPERATION_OUTPAINT_CANVAS_EXPAND",
    "OPERATION_SYNTHESIZE_DOF",
    "OPERATION_ADD_VOLUMETRIC_FOG",
    "OPERATION_LENS_DISTORTION_CORRECT",
    "OPERATION_CHROMATIC_ABERRATION_CONTROL",
    "OPERATION_PERSPECTIVE_HORIZON_LEVEL",
    "OPERATION_SKY_REPLACEMENT_RELIT",
    "OPERATION_REFLECTION_WATER_GLASS",
    "OPERATION_HARMONIZE_RADIATIVE_TRANSFER",
    "OPERATION_RETIME_OPTICAL_FLOW",
    "OPERATION_TONEMAP_HDR",
    "OPERATION_FILM_EMULATION",
]
