"""Registrars for the ten missing ``portrait.*`` catalogue operations (Wave 4).

Each operation is a pure, deterministic image transform implemented in
:mod:`nexus_ai_agent.creative.composition.portrait_imaging`. The handlers adapt a
typed payload plus a caller-supplied in-memory image store into an
:class:`OperationOutcome`. They never touch the network, never dispatch a
command, and never own project state: the caller supplies the images.

The ``portrait.refine`` pack is *additive*: ``register_portrait_operations``
adds the ten operations to any :class:`CapabilityRegistry` the caller passes,
and ``build_portrait_registry`` builds one on top of the base wave-1 registry.
"""

from __future__ import annotations

from typing import Any

from nexus_ai_agent.creative.composition import portrait_imaging as kernels
from nexus_ai_agent.creative.composition import radiative_relighting as relight
from nexus_ai_agent.creative.packs.portrait.errors import InvalidImageError
from nexus_ai_agent.creative.packs.portrait.models import (
    DOMAIN,
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
from nexus_ai_agent.creative.studio.models import PermissionLevel, Project

__all__ = ["register_portrait_operations", "build_portrait_registry", "PORTRAIT_HANDLERS"]


def _split(context: OperationContext) -> tuple[dict[str, object], dict[str, Any]]:
    """Split the caller-supplied ``_images`` store from the typed payload."""
    data = dict(context.input_data)
    store = data.pop("_images", None)
    if not isinstance(store, dict):
        raise InvalidImageError("operation requires an '_images' buffer store")
    return data, store


def _require(store: dict[str, Any], key: str) -> Any:
    try:
        return store[key]
    except KeyError:
        raise InvalidImageError(f"image buffer {key!r} not found") from None


def _out(project: Project, context: OperationContext, image: Any) -> OperationOutcome:
    return OperationOutcome(project=project, history=context.history, output={"image": image})


def _gaze(project: Project, context: OperationContext) -> OperationOutcome:
    data, store = _split(context)
    p = GazeRedirectionInput.model_validate(data)
    return _out(
        project,
        context,
        kernels.gaze_redirection(
            _require(store, p.image_id),
            _require(store, p.eye_mask_id),
            target=(p.target_x, p.target_y),
            strength=p.strength,
        ),
    )


def _blink(project: Project, context: OperationContext) -> OperationOutcome:
    data, store = _split(context)
    p = EyeblinkInput.model_validate(data)
    return _out(
        project,
        context,
        kernels.eyeblink_synthesis(
            _require(store, p.image_id), _require(store, p.eye_mask_id), phase=p.phase
        ),
    )


def _relight(project: Project, context: OperationContext) -> OperationOutcome:
    data, store = _split(context)
    p = FacialRelightingInput.model_validate(data)
    return _out(
        project,
        context,
        kernels.facial_relighting_gradient(
            _require(store, p.image_id),
            light_azimuth_deg=p.light_azimuth_deg,
            light_elevation_deg=p.light_elevation_deg,
            ambient=p.ambient,
        ),
    )


def _skintone(project: Project, context: OperationContext) -> OperationOutcome:
    data, store = _split(context)
    p = SkintoneInput.model_validate(data)
    return _out(
        project,
        context,
        kernels.skintone_harmonization(
            _require(store, p.image_id), _require(store, p.skin_mask_id), strength=p.strength
        ),
    )


def _morph(project: Project, context: OperationContext) -> OperationOutcome:
    data, store = _split(context)
    p = MicroExpressionInput.model_validate(data)
    return _out(
        project,
        context,
        kernels.micro_expression_morph(_require(store, p.image_id), p.control_points),
    )


def _teeth(project: Project, context: OperationContext) -> OperationOutcome:
    data, store = _split(context)
    p = TeethWhiteningInput.model_validate(data)
    return _out(
        project,
        context,
        kernels.teeth_whitening(
            _require(store, p.image_id), _require(store, p.teeth_mask_id), strength=p.strength
        ),
    )


def _beard(project: Project, context: OperationContext) -> OperationOutcome:
    data, store = _split(context)
    p = FacialHairInput.model_validate(data)
    return _out(
        project,
        context,
        kernels.facial_hair_continuity(
            _require(store, p.image_id), _require(store, p.beard_mask_id), density=p.density
        ),
    )


def _repose(project: Project, context: OperationContext) -> OperationOutcome:
    data, store = _split(context)
    p = HeadReposeInput.model_validate(data)
    return _out(
        project,
        context,
        kernels.head_repose_3d(
            _require(store, p.image_id), yaw_deg=p.yaw_deg, pitch_deg=p.pitch_deg
        ),
    )


def _bokeh(project: Project, context: OperationContext) -> OperationOutcome:
    data, store = _split(context)
    p = DepthOfFieldBokehInput.model_validate(data)
    return _out(
        project,
        context,
        kernels.depth_of_field_bokeh(
            _require(store, p.image_id), _require(store, p.depth_id), focus=p.focus, blades=p.blades
        ),
    )


def _aging(project: Project, context: OperationContext) -> OperationOutcome:
    data, store = _split(context)
    p = AgingInput.model_validate(data)
    return _out(
        project,
        context,
        kernels.aging_deaging_geometry(
            _require(store, p.image_id), years=p.years, strength=p.strength
        ),
    )


def _relight_spherical_harmonics(project: Project, context: OperationContext) -> OperationOutcome:
    """Relight a portrait to a declared ambient SH environment."""
    data, store = _split(context)
    p = SphericalHarmonicsRelightInput.model_validate(data)
    image = _require(store, p.image_id)
    normals = _require(store, p.normal_id)
    mask = _require(store, p.mask_id)
    source = relight.estimate_sh_from_subject_normals(image, normals, mask)
    target = relight.ambient_probe(p.target_ambient)
    result = relight.relight_subject_to_environment(
        image,
        normals,
        mask,
        source_probe=source,
        target_probe=target,
        roughness=p.roughness,
        specular_strength=p.specular_strength,
    )
    return _out(project, context, result)


PORTRAIT_HANDLERS: dict[str, Any] = {
    "gaze_redirection": (_gaze, GazeRedirectionInput, "Redirect the gaze inside the eye mask."),
    "eyeblink_synthesis": (_blink, EyeblinkInput, "Deterministic sin^2 blink closure."),
    "facial_relighting_gradient": (
        _relight,
        FacialRelightingInput,
        "Lambertian re-lighting from an estimated normal.",
    ),
    "skintone_harmonization": (
        _skintone,
        SkintoneInput,
        "Constrain the skin region to the CIE L*a*b* skin locus.",
    ),
    "micro_expression_morph": (
        _morph,
        MicroExpressionInput,
        "Thin-plate-spline micro-expression warp.",
    ),
    "teeth_whitening": (
        _teeth,
        TeethWhiteningInput,
        "Adaptive desaturation inside the teeth mask.",
    ),
    "facial_hair_continuity": (
        _beard,
        FacialHairInput,
        "Gabor-filtered stubble density held across shots.",
    ),
    "head_repose_3d": (_repose, HeadReposeInput, "Weak-perspective head re-pose (+/-15 deg)."),
    "depth_of_field_bokeh": (
        _bokeh,
        DepthOfFieldBokehInput,
        "Depth-driven polygonal-aperture bokeh.",
    ),
    "aging_deaging_geometry": (
        _aging,
        AgingInput,
        "Metric landmark displacement for aging/de-aging.",
    ),
    "relight_spherical_harmonics": (
        _relight_spherical_harmonics,
        SphericalHarmonicsRelightInput,
        "L2 spherical-harmonics radiative-transfer portrait relight.",
    ),
}


def register_portrait_operations(registry: CapabilityRegistry) -> CapabilityRegistry:
    """Register the ten ``portrait.*`` operations on ``registry`` (idempotent)."""
    for name, (handler, model, description) in PORTRAIT_HANDLERS.items():
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
                handler=handler,
                required_packs=(PORTRAIT_PACKAGE_ID,),
                deterministic=True,
            ),
        )
    return registry


def build_portrait_registry() -> CapabilityRegistry:
    """A base registry pre-loaded with the ``portrait.refine`` operations."""
    from nexus_ai_agent.creative.studio.capabilities import build_wave1_registry

    return register_portrait_operations(build_wave1_registry())
