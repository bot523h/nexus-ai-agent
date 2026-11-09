"""The composition layer's builtin media capability catalog.

This is the single source of truth for the ``nagar.*`` media capabilities: the
runtime, the tests and the benchmark all read the same declarations here rather
than each re-declaring their own private registry.

Two honesty rules are visible in every entry (Gate 2 / Gate 5):

* **Adapter binding.** Every capability names an allow-listed runtime adapter
  (``imagemagick`` or ``ffmpeg``) and leaves ``canonical_operation`` unset.
  These are therefore *composition-local* operations: they are **not yet
  registered** as first-class operations in the live NEXUS registry. The
  composition layer refuses a capability that claims a ``canonical_operation``
  the live registry does not provide
  (:func:`nexus_ai_agent.creative.composition.canonical.validate_capability`);
  promoting these into the packs registry is a separate, zoned change and is
  recorded as a known limitation, never asserted as done.

* **Licence ledger.** The licence of the *adapter code* (MIT here) is recorded
  separately from the licence of the *runtime tool* it drives. The FFmpeg build
  in this environment ships ``libx264`` (GPL-2.0-or-later); the ImageMagick
  build is Apache-2.0-like. Neither build configuration was verified here, so
  ``commercial_use_verified`` is ``False`` and the derived composition is
  **not** marked commercially approved -- exactly as the licence gate demands:
  unproven is not approved.
"""

from __future__ import annotations

from .spec import (
    CapabilityInput,
    CapabilityOutput,
    CapabilitySpec,
    EvidenceContract,
    LicenseInfo,
    MediaType,
    ResourceRequirements,
    VerificationContract,
)

#: Media types used across the catalog.
PNG = MediaType(kind="image", codec="png")
GRAY_PNG = MediaType(kind="image", codec="png", color_space="gray")
JPEG = MediaType(kind="image", codec="jpeg")
VIDEO = MediaType(kind="video", codec="h264", container="mp4")

#: Adapter-code licence (this repository's code) -- distinct from the tool.
_MIT = LicenseInfo(spdx_id="MIT", source_license="MIT", copyleft=False, allow_commercial=True)

#: The runtime tools' licences. Unverified build configuration => the effective
#: commercial compatibility is *unproven*, which is treated as not-approved.
_IMAGEMAGICK_TOOL = LicenseInfo(
    spdx_id="MIT",
    source_license="MIT",
    runtime_license="ImageMagick License (Apache-2.0-like); build configuration unverified",
    commercial_use_verified=False,
    notice="drives ImageMagick `convert` (build/licence not verified in this environment)",
)
_FFMPEG_TOOL = LicenseInfo(
    spdx_id="MIT",
    source_license="MIT",
    runtime_license="GPL-2.0-or-later (FFmpeg build with libx264); build configuration unverified",
    copyleft=True,
    commercial_use_verified=False,
    notice="drives FFmpeg with libx264 (GPL); build/licence not verified in this environment",
)


def builtin_capabilities() -> dict[str, CapabilitySpec]:
    """The declared ``nagar.*`` media capabilities, keyed by capability id."""
    return {
        "nagar.image.resize": CapabilitySpec(
            capability_id="nagar.image.resize",
            version="1.0.0",
            description="Resize an image to an exact WxH (ImageMagick).",
            inputs=(CapabilityInput(name="source", media_type=PNG),),
            outputs=(CapabilityOutput(name="resized", media_type=PNG),),
            resources=ResourceRequirements(cpu_threads=1, ram_mb=200, disk_mb=100),
            license=_IMAGEMAGICK_TOOL,
            runtime_adapter_id="imagemagick",
            verification=VerificationContract(method="structural", hash_algorithm="sha256"),
            evidence=EvidenceContract(require_decode=True, required_properties=("width", "height")),
            tags=("image", "geometry"),
        ),
        "nagar.image.grayscale": CapabilitySpec(
            capability_id="nagar.image.grayscale",
            version="1.0.0",
            description="Convert an image to grayscale (ImageMagick).",
            inputs=(CapabilityInput(name="source", media_type=PNG),),
            outputs=(CapabilityOutput(name="grayscale", media_type=GRAY_PNG),),
            resources=ResourceRequirements(cpu_threads=1, ram_mb=150, disk_mb=100),
            license=_IMAGEMAGICK_TOOL,
            runtime_adapter_id="imagemagick",
            verification=VerificationContract(
                method="structural", hash_algorithm="sha256", expected_properties={"grayscale": True}
            ),
            evidence=EvidenceContract(require_decode=True, required_properties=("grayscale",)),
            tags=("image", "color"),
        ),
        "nagar.image.convert": CapabilitySpec(
            capability_id="nagar.image.convert",
            version="1.0.0",
            description="Convert an image to another format (ImageMagick).",
            inputs=(CapabilityInput(name="source", media_type=PNG),),
            outputs=(CapabilityOutput(name="converted", media_type=JPEG),),
            resources=ResourceRequirements(cpu_threads=1, ram_mb=100, disk_mb=50),
            license=_IMAGEMAGICK_TOOL,
            runtime_adapter_id="imagemagick",
            tags=("image", "format"),
        ),
        "nagar.image.blur": CapabilitySpec(
            capability_id="nagar.image.blur",
            version="1.0.0",
            description="Gaussian-blur an image (ImageMagick).",
            inputs=(CapabilityInput(name="source", media_type=PNG),),
            outputs=(CapabilityOutput(name="blurred", media_type=PNG),),
            resources=ResourceRequirements(cpu_threads=1, ram_mb=150, disk_mb=100),
            license=_IMAGEMAGICK_TOOL,
            runtime_adapter_id="imagemagick",
            tags=("image", "filter"),
        ),
        "nagar.video.from_image": CapabilitySpec(
            capability_id="nagar.video.from_image",
            version="1.0.0",
            description="Turn a still image into an H.264/MP4 clip (FFmpeg).",
            inputs=(CapabilityInput(name="source", media_type=PNG),),
            outputs=(CapabilityOutput(name="video", media_type=VIDEO),),
            resources=ResourceRequirements(cpu_threads=2, ram_mb=500, disk_mb=200),
            license=_FFMPEG_TOOL,
            runtime_adapter_id="ffmpeg",
            verification=VerificationContract(method="structural", hash_algorithm="sha256"),
            evidence=EvidenceContract(require_decode=True, required_properties=("duration_us",)),
            tags=("video", "encode"),
        ),
        "nagar.video.trim": CapabilitySpec(
            capability_id="nagar.video.trim",
            version="1.0.0",
            description="Trim a clip between two timestamps (FFmpeg).",
            inputs=(CapabilityInput(name="source", media_type=VIDEO),),
            outputs=(CapabilityOutput(name="video", media_type=VIDEO),),
            resources=ResourceRequirements(cpu_threads=1, ram_mb=300, disk_mb=200),
            license=_FFMPEG_TOOL,
            runtime_adapter_id="ffmpeg",
            tags=("video", "edit"),
        ),
        "nagar.video.add_text": CapabilitySpec(
            capability_id="nagar.video.add_text",
            version="1.0.0",
            description="Burn a text overlay into a clip (FFmpeg drawtext).",
            inputs=(CapabilityInput(name="source", media_type=VIDEO),),
            outputs=(CapabilityOutput(name="video", media_type=VIDEO),),
            resources=ResourceRequirements(cpu_threads=2, ram_mb=400, disk_mb=200),
            license=_FFMPEG_TOOL,
            runtime_adapter_id="ffmpeg",
            tags=("video", "text"),
        ),
    }


__all__ = ["GRAY_PNG", "JPEG", "PNG", "VIDEO", "builtin_capabilities"]
