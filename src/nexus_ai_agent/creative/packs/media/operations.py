"""The ``nexus.media.transform`` pack's three canonical media operations.

``media.resize_image``, ``media.grayscale_image`` and ``media.image_to_video``
are **state-recording** operations, exactly like ``slideshow.upscale``: the
media is produced by the canonical FFmpeg adapter *before* the command runs,
and the operation records the measured result as a derived asset in the
project's asset registry.  The operation never touches the filesystem — the
pack substrate is data-only and this module keeps that law.

Two contract facts are declared here and they are the reason this pack exists
at all:

* **Media ports** (``media_inputs`` / ``media_outputs``): the typed shape of
  what each operation consumes and produces.  They live on the *canonical*
  :class:`OperationSpec`, so a composition compiler type-checks a dataflow
  against the same registry the command bus authorizes against.
* **Capability version**: the registry carries it, the manifest carries the
  pack version, and a composition node pins both when it is compiled.

The operations are deliberately **not** media engines.  ``media.image_to_video``
is level C (confirmation) because it is a heavy export; ``media.resize_image``
and ``media.grayscale_image`` are level B (reversible) because they record a
derived asset exactly like the slideshow pack's upscale.
"""

from __future__ import annotations

import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from nexus_ai_agent.creative.studio.capabilities import (
    CapabilityRegistry,
    OperationContext,
    OperationOutcome,
    OperationSpec,
)
from nexus_ai_agent.creative.studio.models import (
    CommandValidationError,
    MediaContract,
    MediaPort,
    PermissionLevel,
    Project,
)

from . import DOMAIN, PACK_VERSION, PACKAGE_ID

OPERATION_RESIZE = "media.resize_image"
OPERATION_GRAYSCALE = "media.grayscale_image"
OPERATION_IMAGE_TO_VIDEO = "media.image_to_video"

#: The capability id each operation belongs to.  The registry derives the
#: capability id from ``(domain, capability)``, so the manifest's capability
#: list and the registry's capability list are the same strings.
CAPABILITY_RESIZE = "resize_image"
CAPABILITY_GRAYSCALE = "grayscale_image"
CAPABILITY_IMAGE_TO_VIDEO = "image_to_video"

#: The media contract every still-image operation consumes.
_SOURCE_IMAGE = MediaContract(kind="image")
#: The media contract every still-image operation produces (a PNG still).
_STILL_PNG = MediaContract(kind="image", container="png", codec="png")
#: The grayscale still: same PNG container, luma-only colour space.
_GRAY_PNG = MediaContract(kind="image", container="png", codec="png", color_space="gray")
#: The video contract: an H.264 stream in an MP4 container.
_H264_MP4 = MediaContract(
    kind="video",
    container="mp4",
    codec="h264",
    pixel_format="yuv420p",
)

#: The producer identity recorded in every derived asset's provenance.
PRODUCED_BY = "nagar.local.media.transform.v1"


# ---------------------------------------------------------------------------
# typed operation inputs
# ---------------------------------------------------------------------------


class _DerivedImageInput(BaseModel):
    """Shared shape of a still-image derivation.

    ``source_sha256`` is the *content* identity of the registered source
    asset.  The handler compares it with the registered asset, so a source
    that changed after registration is refused instead of being re-used.
    """

    model_config = ConfigDict(extra="forbid")

    source_asset_id: str = Field(min_length=1, max_length=128)
    source_sha256: str = Field(min_length=64, max_length=71)
    output_path: str = Field(min_length=1, max_length=1024)
    output_sha256: str = Field(min_length=64, max_length=71)
    #: The same bounds the still primitives enforce, declared **in the schema**:
    #: a geometry the render lane would refuse must fail validation, not start
    #: a process and then fail.
    width: int = Field(ge=2, le=8192)
    height: int = Field(ge=2, le=8192)

    @model_validator(mode="after")
    def _within_the_pixel_budget(self) -> _DerivedImageInput:
        if self.width * self.height > 33_177_600:
            raise ValueError(
                f"{self.width}x{self.height} exceeds the 8K pixel budget the still "
                "primitives accept"
            )
        return self


class ResizeImageInput(_DerivedImageInput):
    """Resize a registered still image to an exact ``width`` x ``height``."""

    source_width: int = Field(ge=1, le=16384)
    source_height: int = Field(ge=1, le=16384)
    filter_flags: Literal["lanczos"] = "lanczos"


class GrayscaleImageInput(_DerivedImageInput):
    """Convert a registered still image to a luma-only (grayscale) PNG."""

    mean_luma: float = Field(ge=0.0, le=1.0)


class ImageToVideoInput(BaseModel):
    """Encode a registered still image into an H.264/MP4 clip."""

    model_config = ConfigDict(extra="forbid")

    source_asset_id: str = Field(min_length=1, max_length=128)
    source_sha256: str = Field(min_length=64, max_length=71)
    output_path: str = Field(min_length=1, max_length=1024)
    output_sha256: str = Field(min_length=64, max_length=71)
    width: int = Field(ge=2, le=16384)
    height: int = Field(ge=2, le=16384)
    duration_us: int = Field(ge=1000, le=3_600_000_000)
    fps: int = Field(ge=1, le=240)
    #: The render lane has exactly one encoder; a request for another one is
    #: refused at validation instead of being silently substituted, because the
    #: recorded provenance would then name an encoder that never ran.
    encoder: Literal["libx264"] = Field()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _require_asset(project: Project, asset_id: str, *, kind: str) -> str:
    """Return the registered asset's content hash, or refuse."""
    for record in project.assets:
        if record.asset_id == asset_id:
            if record.media_kind != kind:
                raise CommandValidationError(
                    f"asset {asset_id!r} is {record.media_kind!r}, expected {kind!r}"
                )
            return record.content_sha256
    raise CommandValidationError(
        f"asset {asset_id!r} is not registered; run the media registration step first"
    )


def _check_source(project: Project, asset_id: str, expected_sha256: str) -> None:
    actual = _require_asset(project, asset_id, kind="image")
    if actual != expected_sha256:
        raise CommandValidationError(
            f"asset {asset_id!r} changed after it was registered (content hash mismatch)"
        )


def _record_derived(
    project: Project,
    *,
    media_kind: str,
    content_sha256: str,
    duration_us: int,
    parent_asset_ids: tuple[str, ...],
    provenance: dict[str, object],
) -> tuple[Project, str]:
    """Append one derived asset to *project* and return ``(project, asset_id)``.

    The source asset is never mutated — the registry is additive.
    """
    from nexus_ai_agent.creative.studio.models import AssetRecord

    asset_id = f"derived_{uuid.uuid4().hex[:12]}"
    record = AssetRecord(
        asset_id=asset_id,
        media_kind=media_kind,  # type: ignore[arg-type]
        content_sha256=content_sha256,
        duration_us=duration_us,
        parent_asset_ids=parent_asset_ids,
        provenance=provenance,
    )
    return project.model_copy(update={"assets": [*project.assets, record]}), asset_id


# ---------------------------------------------------------------------------
# handlers
# ---------------------------------------------------------------------------


def _resize_image(project: Project, context: OperationContext) -> OperationOutcome:
    payload = ResizeImageInput.model_validate(context.input_data)
    _check_source(project, payload.source_asset_id, payload.source_sha256)
    provenance: dict[str, object] = {
        "output_path": payload.output_path,
        "operation": OPERATION_RESIZE,
        "source_dimensions": f"{payload.source_width}x{payload.source_height}",
        "resolution": f"{payload.width}x{payload.height}",
        "filter": f"scale={payload.width}:{payload.height}:flags={payload.filter_flags}",
        "produced_by": PRODUCED_BY,
    }
    new_project, asset_id = _record_derived(
        project,
        media_kind="image",
        content_sha256=payload.output_sha256,
        duration_us=0,
        parent_asset_ids=(payload.source_asset_id,),
        provenance=provenance,
    )
    output: dict[str, object] = {
        "asset_id": asset_id,
        "is_derived": True,
        "parent_asset_ids": [payload.source_asset_id],
        "width": payload.width,
        "height": payload.height,
        "output_path": payload.output_path,
    }
    return OperationOutcome(new_project, context.history, output)


def _grayscale_image(project: Project, context: OperationContext) -> OperationOutcome:
    payload = GrayscaleImageInput.model_validate(context.input_data)
    _check_source(project, payload.source_asset_id, payload.source_sha256)
    provenance: dict[str, object] = {
        "output_path": payload.output_path,
        "operation": OPERATION_GRAYSCALE,
        "resolution": f"{payload.width}x{payload.height}",
        "color_space": "gray",
        "mean_luma": payload.mean_luma,
        "produced_by": PRODUCED_BY,
    }
    new_project, asset_id = _record_derived(
        project,
        media_kind="image",
        content_sha256=payload.output_sha256,
        duration_us=0,
        parent_asset_ids=(payload.source_asset_id,),
        provenance=provenance,
    )
    output: dict[str, object] = {
        "asset_id": asset_id,
        "is_derived": True,
        "parent_asset_ids": [payload.source_asset_id],
        "width": payload.width,
        "height": payload.height,
        "mean_luma": payload.mean_luma,
        "output_path": payload.output_path,
    }
    return OperationOutcome(new_project, context.history, output)


def _image_to_video(project: Project, context: OperationContext) -> OperationOutcome:
    payload = ImageToVideoInput.model_validate(context.input_data)
    _check_source(project, payload.source_asset_id, payload.source_sha256)
    provenance: dict[str, object] = {
        "output_path": payload.output_path,
        "operation": OPERATION_IMAGE_TO_VIDEO,
        "resolution": f"{payload.width}x{payload.height}",
        "duration_us": payload.duration_us,
        "fps": payload.fps,
        "encoder": payload.encoder,
        "produced_by": PRODUCED_BY,
    }
    new_project, asset_id = _record_derived(
        project,
        media_kind="video",
        content_sha256=payload.output_sha256,
        duration_us=payload.duration_us,
        parent_asset_ids=(payload.source_asset_id,),
        provenance=provenance,
    )
    output: dict[str, object] = {
        "asset_id": asset_id,
        "is_derived": True,
        "parent_asset_ids": [payload.source_asset_id],
        "width": payload.width,
        "height": payload.height,
        "duration_us": payload.duration_us,
        "fps": payload.fps,
        "output_path": payload.output_path,
    }
    return OperationOutcome(new_project, context.history, output)


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------


def register_media_operations(registry: CapabilityRegistry) -> CapabilityRegistry:
    """Register the pack's three operations on an existing capability registry."""
    registry.register_domain(
        DOMAIN, "Local still-image and clip transforms (pack nexus.media.transform)"
    )
    registry.register_operation(
        DOMAIN,
        CAPABILITY_RESIZE,
        OperationSpec(
            operation_id=OPERATION_RESIZE,
            description="Record an exact-WxH Lanczos resize of a still image as a derived asset.",
            permission_level=PermissionLevel.REVERSIBLE,
            input_model=ResizeImageInput,
            handler=_resize_image,
            required_packs=(PACKAGE_ID,),
            deterministic=True,
            schema_version=1,
            media_inputs=(
                MediaPort(name="source", contract=_SOURCE_IMAGE, description="the still to resize"),
            ),
            media_outputs=(
                MediaPort(name="resized", contract=_STILL_PNG, description="the resized PNG"),
            ),
            media_derived_fields=(
                "source_asset_id",
                "source_sha256",
                "output_path",
                "output_sha256",
                "source_width",
                "source_height",
            ),
        ),
        capability_version=PACK_VERSION,
    )
    registry.register_operation(
        DOMAIN,
        CAPABILITY_GRAYSCALE,
        OperationSpec(
            operation_id=OPERATION_GRAYSCALE,
            description="Record a luma-only (grayscale) conversion of a still image.",
            permission_level=PermissionLevel.REVERSIBLE,
            input_model=GrayscaleImageInput,
            handler=_grayscale_image,
            required_packs=(PACKAGE_ID,),
            deterministic=True,
            schema_version=1,
            media_inputs=(
                MediaPort(
                    name="source",
                    contract=_SOURCE_IMAGE,
                    description="the still to convert",
                ),
            ),
            media_outputs=(
                MediaPort(name="grayscale", contract=_GRAY_PNG, description="the grayscale PNG"),
            ),
            media_derived_fields=(
                "source_asset_id",
                "source_sha256",
                "output_path",
                "output_sha256",
                "width",
                "height",
                "mean_luma",
            ),
        ),
        capability_version=PACK_VERSION,
    )
    registry.register_operation(
        DOMAIN,
        CAPABILITY_IMAGE_TO_VIDEO,
        OperationSpec(
            operation_id=OPERATION_IMAGE_TO_VIDEO,
            description="Record an H.264/MP4 encode of a still image as a derived clip.",
            permission_level=PermissionLevel.CONFIRMATION,
            input_model=ImageToVideoInput,
            handler=_image_to_video,
            required_packs=(PACKAGE_ID,),
            deterministic=True,
            schema_version=1,
            media_inputs=(
                MediaPort(name="source", contract=_SOURCE_IMAGE, description="the still to encode"),
            ),
            media_outputs=(
                MediaPort(name="video", contract=_H264_MP4, description="the encoded clip"),
            ),
            media_derived_fields=(
                "source_asset_id",
                "source_sha256",
                "output_path",
                "output_sha256",
            ),
        ),
        capability_version=PACK_VERSION,
    )
    return registry


def build_media_registry() -> CapabilityRegistry:
    """The Wave 1 catalog plus this pack's three operations."""
    from nexus_ai_agent.creative.studio.capabilities import build_wave1_registry

    return register_media_operations(build_wave1_registry())


__all__ = [
    "CAPABILITY_GRAYSCALE",
    "CAPABILITY_IMAGE_TO_VIDEO",
    "CAPABILITY_RESIZE",
    "OPERATION_GRAYSCALE",
    "OPERATION_IMAGE_TO_VIDEO",
    "OPERATION_RESIZE",
    "GrayscaleImageInput",
    "ImageToVideoInput",
    "ResizeImageInput",
    "build_media_registry",
    "register_media_operations",
]
