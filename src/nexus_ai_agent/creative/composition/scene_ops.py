"""Registrars for the ten missing ``scene.*`` catalogue operations (Wave 3).

Each operation is a pure, deterministic image transform implemented in
:mod:`kernels`. The handlers here adapt a typed payload plus a caller-supplied
in-memory image store into an :class:`OperationOutcome`. They never touch the
network, never dispatch a command, and never own project state: the caller
supplies the images.

The ``scene.imaging`` pack is *additive*: ``register_scene_operations`` adds the
ten operations to any :class:`CapabilityRegistry` the caller passes, and
``build_scene_registry`` builds one on top of the base wave-1 registry.
"""

from __future__ import annotations

from typing import Any

from nexus_ai_agent.creative.composition import computational_cinematography as cine
from nexus_ai_agent.creative.composition import film_emulation as film
from nexus_ai_agent.creative.composition import hdr_tonemapping as hdr
from nexus_ai_agent.creative.composition import radiative_relighting as relight
from nexus_ai_agent.creative.composition import scene_imaging as kernels
from nexus_ai_agent.creative.packs.scene.errors import InvalidImageError
from nexus_ai_agent.creative.packs.scene.models import (
    DOMAIN,
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
from nexus_ai_agent.creative.studio.models import PermissionLevel, Project

__all__ = ["register_scene_operations", "build_scene_registry", "SCENE_HANDLERS"]


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
    """Return a no-state-change outcome carrying the produced image."""
    return OperationOutcome(project=project, history=context.history, output={"image": image})


def _insert_object(project: Project, context: OperationContext) -> OperationOutcome:
    data, store = _split(context)
    p = InsertObjectInput.model_validate(data)
    result = kernels.insert_object_with_shadow(
        _require(store, p.image_id),
        _require(store, p.object_id),
        _require(store, p.mask_id),
        offset=(p.offset_x, p.offset_y),
    )
    return _out(project, context, result)


def _remove_object(project: Project, context: OperationContext) -> OperationOutcome:
    data, store = _split(context)
    p = RemoveObjectInput.model_validate(data)
    return _out(
        project,
        context,
        kernels.remove_object_inpaint(
            _require(store, p.image_id), _require(store, p.mask_id), iters=p.iters
        ),
    )


def _outpaint(project: Project, context: OperationContext) -> OperationOutcome:
    data, store = _split(context)
    p = OutpaintInput.model_validate(data)
    return _out(
        project, context, kernels.outpaint_canvas_expand(_require(store, p.image_id), pad=p.pad)
    )


def _dof(project: Project, context: OperationContext) -> OperationOutcome:
    data, store = _split(context)
    p = DepthOfFieldInput.model_validate(data)
    return _out(
        project,
        context,
        kernels.synthesize_depth_of_field(
            _require(store, p.image_id),
            _require(store, p.depth_id),
            focus=p.focus,
            f_number=p.f_number,
            f_mm=p.f_mm,
        ),
    )


def _fog(project: Project, context: OperationContext) -> OperationOutcome:
    data, store = _split(context)
    p = FogHazeInput.model_validate(data)
    return _out(
        project,
        context,
        kernels.add_volumetric_fog_haze(
            _require(store, p.image_id), _require(store, p.depth_id), beta=p.beta, g=p.g
        ),
    )


def _lens(project: Project, context: OperationContext) -> OperationOutcome:
    data, store = _split(context)
    p = LensDistortionInput.model_validate(data)
    return _out(
        project,
        context,
        kernels.lens_distortion_correct(_require(store, p.image_id), k1=p.k1, k2=p.k2, k3=p.k3),
    )


def _chromatic(project: Project, context: OperationContext) -> OperationOutcome:
    data, store = _split(context)
    p = ChromaticAberrationInput.model_validate(data)
    return _out(
        project,
        context,
        kernels.chromatic_aberration_control(
            _require(store, p.image_id), scales=(p.scale_r, p.scale_g, p.scale_b)
        ),
    )


def _horizon(project: Project, context: OperationContext) -> OperationOutcome:
    data, store = _split(context)
    p = HorizonLevelInput.model_validate(data)
    image, angle = kernels.perspective_horizon_level(_require(store, p.image_id))
    return OperationOutcome(
        project=project, history=context.history, output={"image": image, "angle_deg": angle}
    )


def _sky_relit(project: Project, context: OperationContext) -> OperationOutcome:
    data, store = _split(context)
    p = SkyRelitInput.model_validate(data)
    return _out(
        project,
        context,
        kernels.sky_replacement_relit(
            _require(store, p.image_id),
            _require(store, p.mask_id),
            _require(store, p.sky_id),
            sky_temp_k=p.sky_temp_k,
        ),
    )


def _reflection(project: Project, context: OperationContext) -> OperationOutcome:
    data, store = _split(context)
    p = ReflectionInput.model_validate(data)
    return _out(
        project,
        context,
        kernels.reflection_water_glass_synth(
            _require(store, p.image_id), axis=p.axis, fresnel=p.fresnel, waves=p.waves
        ),
    )


def _harmonize_radiative_transfer(project: Project, context: OperationContext) -> OperationOutcome:
    """Relight a composited foreground to a declared ambient SH environment."""
    data, store = _split(context)
    p = RadiativeTransferHarmonizeInput.model_validate(data)
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


def _tonemap_hdr(project: Project, context: OperationContext) -> OperationOutcome:
    """Tone-map scene-linear HDR radiance and optionally adapt the white point."""
    data, store = _split(context)
    p = HdrTonemapInput.model_validate(data)
    image = _require(store, p.image_id)
    working = image
    if p.adapt_temperature_k is not None:
        working = hdr.bradford_adapt(
            image,
            src_white_xy=(0.3127, 0.3290),
            dst_white_xy=hdr.kelvin_to_xy(p.adapt_temperature_k),
        )
    exposure = p.exposure
    if p.auto_exposure:
        exposure *= hdr.auto_exposure_scale(working, key=p.key)
    curves = {
        "reinhard": lambda x: hdr.reinhard_tonemap(x, white=p.white, exposure=exposure),
        "aces": lambda x: hdr.aces_filmic_tonemap(x, exposure=exposure),
        "hejl": lambda x: hdr.hejl_burgess_tonemap(x, exposure=exposure),
        "uncharted": lambda x: hdr.uncharted2_tonemap(x, exposure=exposure),
    }
    result = curves[p.curve](working)
    return _out(project, context, result)


def _film_emulation(project: Project, context: OperationContext) -> OperationOutcome:
    """Apply photochemical gate weave, halation and grain to a frame."""
    data, store = _split(context)
    p = FilmEmulationInput.model_validate(data)
    image = _require(store, p.image_id)
    result = film.apply_film_emulation(
        image,
        frame_index=p.frame_index,
        seed=p.seed,
        grain_sigma=p.grain_sigma,
        halation_strength=p.halation_strength,
        weave_amplitude=p.weave_amplitude,
    )
    return _out(project, context, result)


def _retime_optical_flow(project: Project, context: OperationContext) -> OperationOutcome:
    """Flow-based interpolation of a frame between two keyframes (variable frame rate)."""
    data, store = _split(context)
    p = OpticalFlowRetimeInput.model_validate(data)
    image_a = _require(store, p.image_a_id)
    image_b = _require(store, p.image_b_id)
    result = cine.interpolate_frame(image_a, image_b, p.t)
    return _out(project, context, result)


SCENE_HANDLERS: dict[str, Any] = {
    "insert_object_with_shadow": (
        _insert_object,
        InsertObjectInput,
        "Insert a premultiplied-alpha object with a contact shadow.",
    ),
    "remove_object_inpaint": (
        _remove_object,
        RemoveObjectInput,
        "Remove an object with multiscale harmonic inpainting.",
    ),
    "outpaint_canvas_expand": (
        _outpaint,
        OutpaintInput,
        "Expand the canvas with a mirrored Laplacian-pyramid border.",
    ),
    "synthesize_depth_of_field": (
        _dof,
        DepthOfFieldInput,
        "Depth-driven circle-of-confusion defocus.",
    ),
    "add_volumetric_fog_haze": (
        _fog,
        FogHazeInput,
        "Koschmieder atmospheric scattering with HG phase.",
    ),
    "lens_distortion_correct": (_lens, LensDistortionInput, "Brown-Conrady radial undistortion."),
    "chromatic_aberration_control": (
        _chromatic,
        ChromaticAberrationInput,
        "Per-channel radial magnification for lateral CA.",
    ),
    "perspective_horizon_level": (
        _horizon,
        HorizonLevelInput,
        "Estimate and level the dominant horizon.",
    ),
    "sky_replacement_relit": (
        _sky_relit,
        SkyRelitInput,
        "Replace the sky and re-light the foreground.",
    ),
    "reflection_water_glass_synth": (
        _reflection,
        ReflectionInput,
        "Flat mirror reflection with Fresnel and ripples.",
    ),
    "harmonize_radiative_transfer": (
        _harmonize_radiative_transfer,
        RadiativeTransferHarmonizeInput,
        "L2 spherical-harmonics radiative-transfer relight of a composite.",
    ),
    "retime_optical_flow": (
        _retime_optical_flow,
        OpticalFlowRetimeInput,
        "Flow-based interpolation of a frame between two keyframes (VFR retime).",
    ),
    "tonemap_hdr": (
        _tonemap_hdr,
        HdrTonemapInput,
        "Scene-linear HDR tone mapping with optional white-point adaptation.",
    ),
    "film_emulation": (
        _film_emulation,
        FilmEmulationInput,
        "Photochemical gate weave, halation and film grain.",
    ),
}


def register_scene_operations(registry: CapabilityRegistry) -> CapabilityRegistry:
    """Register the ten ``scene.*`` operations on ``registry`` (idempotent)."""
    for name, (handler, model, description) in SCENE_HANDLERS.items():
        operation_id = f"{DOMAIN}.{name}"
        try:
            registry.get_spec(operation_id)
            continue  # already registered
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
                required_packs=(SCENE_PACKAGE_ID,),
                deterministic=True,
            ),
        )
    return registry


def build_scene_registry() -> CapabilityRegistry:
    """A base registry pre-loaded with the ``scene.imaging`` operations."""
    from nexus_ai_agent.creative.studio.capabilities import build_wave1_registry

    return register_scene_operations(build_wave1_registry())
