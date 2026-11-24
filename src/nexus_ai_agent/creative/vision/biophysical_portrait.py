"""Portrait pack facade over the temporal colorimetry implementation."""

from __future__ import annotations

from nexus_ai_agent.creative.temporal.biophysical_portrait import (
    BASELINE_OPTICAL_DENSITY,
    HEMOGLOBIN_EXTINCTION,
    MELANIN_EXTINCTION,
    WAVELENGTHS_NM,
    HueProtectionResult,
    Oklch,
    SkinChromophoreEstimate,
    SkinModelError,
    enforce_skin_hue_protection,
    estimate_chromophores,
    linear_srgb_to_oklab,
    linear_srgb_to_oklch,
    oklab_to_linear_srgb,
    oklch_to_linear_srgb,
    reflectance_from_chromophores,
    skin_luminance,
)
from nexus_ai_agent.creative.temporal.biophysical_portrait import (
    _gamma_to_linear as _gamma_to_linear,
)
from nexus_ai_agent.creative.temporal.biophysical_portrait import (
    _shortest_angle as _shortest_angle,
)

__all__ = [
    "BASELINE_OPTICAL_DENSITY",
    "HEMOGLOBIN_EXTINCTION",
    "MELANIN_EXTINCTION",
    "WAVELENGTHS_NM",
    "HueProtectionResult",
    "Oklch",
    "SkinChromophoreEstimate",
    "SkinModelError",
    "enforce_skin_hue_protection",
    "estimate_chromophores",
    "linear_srgb_to_oklab",
    "linear_srgb_to_oklch",
    "oklab_to_linear_srgb",
    "oklch_to_linear_srgb",
    "reflectance_from_chromophores",
    "skin_luminance",
]
