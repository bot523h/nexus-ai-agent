"""Biophysical portrait analysis: Beer-Lambert chromophores and OKLab protection.

Skin reflectance is explained by two dominant chromophores — epidermal melanin
and dermal haemoglobin — under a modified Beer-Lambert law in optical-density
space.  The inversion is a closed-form 2x2 Gramian projection, and grading is
constrained in Bjorn Ottosson's OKLab space so a look can never rotate skin hue
past a biological plausibility bound.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from nexus_ai_agent.creative.perception.radiometric_scene_cut import (
    CutDetectionError,
    linear_luminance,
    srgb_to_linear,
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

WAVELENGTHS_NM: Final[tuple[float, ...]] = (450.0, 500.0, 550.0, 600.0, 650.0, 700.0)
# Relative extinction coefficients; melanin falls with wavelength while
# haemoglobin peaks near the green Soret/Q-bands.
MELANIN_EXTINCTION: Final[tuple[float, ...]] = (1.00, 0.86, 0.70, 0.57, 0.47, 0.39)
HEMOGLOBIN_EXTINCTION: Final[tuple[float, ...]] = (1.20, 1.05, 1.45, 1.00, 0.34, 0.24)
BASELINE_OPTICAL_DENSITY: Final[tuple[float, ...]] = (0.22, 0.20, 0.18, 0.16, 0.14, 0.12)
_REFLECTANCE_FLOOR: Final[float] = 1e-9
_MAX_HUE_DELTA_DEGREES: Final[float] = 180.0


class SkinModelError(ValueError):
    """Raised when reflectance or colour input violates a physical precondition."""


def _validate_reflectance(reflectance: Sequence[float]) -> tuple[float, ...]:
    if not reflectance:
        raise SkinModelError("reflectance spectrum must not be empty")
    values = []
    for value in reflectance:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SkinModelError("reflectance values must be real numbers")
        number = float(value)
        if not math.isfinite(number) or not 0.0 <= number <= 1.0:
            raise SkinModelError("reflectance must be finite and inside [0, 1]")
        values.append(number)
    return tuple(values)


def _validate_wavelengths(wavelengths: Sequence[float] | None) -> tuple[float, ...]:
    if wavelengths is None:
        return WAVELENGTHS_NM
    validated = []
    for value in wavelengths:
        number = float(value)
        if not math.isfinite(number) or number <= 0.0:
            raise SkinModelError("wavelengths must be finite and positive")
        validated.append(number)
    return tuple(validated)


@dataclass(frozen=True, slots=True)
class SkinChromophoreEstimate:
    melanin: float
    hemoglobin: float
    residual_norm: float
    reconstructed_optical_density: tuple[float, ...]
    clamped: bool

    def __post_init__(self) -> None:
        for name in ("melanin", "hemoglobin", "residual_norm"):
            value = getattr(self, name)
            if not math.isfinite(value) or value < 0.0:
                raise SkinModelError(f"{name} must be finite and non-negative")
        if not self.reconstructed_optical_density:
            raise SkinModelError("reconstructed optical density must not be empty")
        if any(not math.isfinite(value) for value in self.reconstructed_optical_density):
            raise SkinModelError("reconstructed optical density must be finite")
        if not isinstance(self.clamped, bool):
            raise TypeError("clamped must be a bool")


def reflectance_from_chromophores(
    melanin: float,
    hemoglobin: float,
    *,
    baseline: Sequence[float] = BASELINE_OPTICAL_DENSITY,
    melanin_extinction: Sequence[float] = MELANIN_EXTINCTION,
    hemoglobin_extinction: Sequence[float] = HEMOGLOBIN_EXTINCTION,
) -> tuple[float, ...]:
    """Forward Beer-Lambert model: concentrations -> reflectance spectrum."""

    if melanin < 0.0 or hemoglobin < 0.0:
        raise SkinModelError("chromophore concentrations must be non-negative")
    if len(baseline) != len(melanin_extinction) or len(baseline) != len(hemoglobin_extinction):
        raise SkinModelError("spectral basis vectors must share a length")
    optical_density = [
        base + melanin * mel + hemoglobin * hem
        for base, mel, hem in zip(baseline, melanin_extinction, hemoglobin_extinction, strict=True)
    ]
    return tuple(math.exp(-density) for density in optical_density)


def estimate_chromophores(
    reflectance: Sequence[float],
    *,
    wavelengths_nm: Sequence[float] | None = None,
    ridge: float = 1e-9,
) -> SkinChromophoreEstimate:
    """Closed-form non-negative 2x2 Gramian least-squares chromophore inversion."""

    values = _validate_reflectance(reflectance)
    _validate_wavelengths(wavelengths_nm)
    if not math.isfinite(ridge) or ridge < 0.0:
        raise SkinModelError("ridge must be finite and non-negative")
    size = len(values)
    if size != len(BASELINE_OPTICAL_DENSITY):
        raise SkinModelError(
            "reflectance must cover the same bands as the spectral basis "
            f"({len(BASELINE_OPTICAL_DENSITY)}), got {size}"
        )
    target = []
    for value, base in zip(values, BASELINE_OPTICAL_DENSITY, strict=True):
        optical_density = -math.log(max(value, _REFLECTANCE_FLOOR))
        target.append(optical_density - base)
    mel = MELANIN_EXTINCTION
    hem = HEMOGLOBIN_EXTINCTION
    gram_00 = sum(component * component for component in mel) + ridge
    gram_11 = sum(component * component for component in hem) + ridge
    gram_01 = sum(a * b for a, b in zip(mel, hem, strict=False))
    determinant = gram_00 * gram_11 - gram_01 * gram_01
    if determinant <= 1e-18:
        raise SkinModelError("spectral basis is degenerate; cannot invert")
    projection_mel = sum(a * y for a, y in zip(mel, target, strict=False))
    projection_hem = sum(b * y for b, y in zip(hem, target, strict=False))
    raw_melanin = (gram_11 * projection_mel - gram_01 * projection_hem) / determinant
    raw_hemoglobin = (gram_00 * projection_hem - gram_01 * projection_mel) / determinant
    clamped = raw_melanin < 0.0 or raw_hemoglobin < 0.0
    melanin = max(0.0, raw_melanin)
    hemoglobin = max(0.0, raw_hemoglobin)
    reconstructed = tuple(
        base + melanin * a + hemoglobin * b
        for base, a, b in zip(BASELINE_OPTICAL_DENSITY, mel, hem, strict=True)
    )
    residual = math.sqrt(
        sum(
            (y - (melanin * a + hemoglobin * b)) ** 2
            for y, a, b in zip(target, MELANIN_EXTINCTION, HEMOGLOBIN_EXTINCTION, strict=False)
        )
    )
    return SkinChromophoreEstimate(
        melanin=melanin,
        hemoglobin=hemoglobin,
        residual_norm=residual,
        reconstructed_optical_density=reconstructed,
        clamped=clamped,
    )


# ---------------------------------------------------------------- OKLab colour


def _validate_linear_rgb(rgb: Sequence[float]) -> tuple[float, float, float]:
    if len(rgb) != 3:
        raise SkinModelError("RGB colour must have three components")
    values = []
    for value in rgb:
        number = float(value)
        if not math.isfinite(number) or not 0.0 <= number <= 1.0:
            raise SkinModelError("RGB components must be finite and inside [0, 1]")
        values.append(number)
    return values[0], values[1], values[2]


def _cbrt(value: float) -> float:
    return math.copysign(abs(value) ** (1.0 / 3.0), value)


def linear_srgb_to_oklab(rgb: Sequence[float]) -> tuple[float, float, float]:
    """Bjorn Ottosson's linear-sRGB to OKLab transform."""

    red, green, blue = _validate_linear_rgb(rgb)
    l_cone = 0.4122214708 * red + 0.5363325363 * green + 0.0514459929 * blue
    m_cone = 0.2119034982 * red + 0.6806995451 * green + 0.1073969566 * blue
    s_cone = 0.0883024619 * red + 0.2817188376 * green + 0.6299787005 * blue
    l_root = _cbrt(l_cone)
    m_root = _cbrt(m_cone)
    s_root = _cbrt(s_cone)
    lightness = 0.2104542553 * l_root + 0.7936177850 * m_root - 0.0040720468 * s_root
    a_axis = 1.9779984951 * l_root - 2.4285922050 * m_root + 0.4505937099 * s_root
    b_axis = 0.0259040371 * l_root + 0.7827717662 * m_root - 0.8086757660 * s_root
    return lightness, a_axis, b_axis


def oklab_to_linear_srgb(lab: Sequence[float]) -> tuple[float, float, float]:
    if len(lab) != 3:
        raise SkinModelError("OKLab colour must have three components")
    lightness, a_axis, b_axis = (float(component) for component in lab)
    if not all(math.isfinite(component) for component in (lightness, a_axis, b_axis)):
        raise SkinModelError("OKLab components must be finite")
    l_root = lightness + 0.3963377774 * a_axis + 0.2158037573 * b_axis
    m_root = lightness - 0.1055613458 * a_axis - 0.0638541728 * b_axis
    s_root = lightness - 0.0894841775 * a_axis - 1.2914855480 * b_axis
    l_cone = l_root**3
    m_cone = m_root**3
    s_cone = s_root**3
    red = 4.0767416621 * l_cone - 3.3077115913 * m_cone + 0.2309699292 * s_cone
    green = -1.2684380046 * l_cone + 2.6097574011 * m_cone - 0.3413193965 * s_cone
    blue = -0.0041960863 * l_cone - 0.7034186147 * m_cone + 1.7076147010 * s_cone
    return red, green, blue


def _encode_srgb(linear: float) -> float:
    clamped = max(0.0, min(1.0, linear))
    if clamped <= 0.0031308:
        return clamped * 12.92
    return 1.055 * math.pow(clamped, 1.0 / 2.4) - 0.055


@dataclass(frozen=True, slots=True)
class Oklch:
    lightness: float
    chroma: float
    hue_deg: float

    def __post_init__(self) -> None:
        if not math.isfinite(self.lightness):
            raise SkinModelError("lightness must be finite")
        if not math.isfinite(self.chroma) or self.chroma < 0.0:
            raise SkinModelError("chroma must be finite and non-negative")
        if not math.isfinite(self.hue_deg):
            raise SkinModelError("hue must be finite")


def linear_srgb_to_oklch(rgb: Sequence[float]) -> Oklch:
    lightness, a_axis, b_axis = linear_srgb_to_oklab(rgb)
    chroma = math.hypot(a_axis, b_axis)
    hue = math.degrees(math.atan2(b_axis, a_axis)) % 360.0
    return Oklch(lightness, chroma, hue)


def oklch_to_linear_srgb(colour: Oklch) -> tuple[float, float, float]:
    radians = math.radians(colour.hue_deg)
    return oklab_to_linear_srgb(
        (
            colour.lightness,
            colour.chroma * math.cos(radians),
            colour.chroma * math.sin(radians),
        )
    )


def skin_luminance(rgb: Sequence[float]) -> float:
    """Rec.709 luminance of an sRGB triple (gamma-encoded input)."""

    if len(rgb) != 3:
        raise SkinModelError("RGB colour must have three components")
    try:
        return linear_luminance((float(rgb[0]), float(rgb[1]), float(rgb[2])))
    except CutDetectionError as exc:
        raise SkinModelError(str(exc)) from exc


def _gamma_to_linear(rgb: Sequence[float]) -> tuple[float, float, float]:
    if len(rgb) != 3:
        raise SkinModelError("RGB colour must have three components")
    try:
        return (
            srgb_to_linear(float(rgb[0])),
            srgb_to_linear(float(rgb[1])),
            srgb_to_linear(float(rgb[2])),
        )
    except CutDetectionError as exc:
        raise SkinModelError(str(exc)) from exc


def _shortest_angle(first: float, second: float) -> float:
    difference = (first - second + 180.0) % 360.0 - 180.0
    return difference


@dataclass(frozen=True, slots=True)
class HueProtectionResult:
    original_hue_deg: float
    graded_hue_deg: float
    applied_hue_deg: float
    delta_deg: float
    allowed: bool
    adjusted_rgb: tuple[float, float, float]

    def __post_init__(self) -> None:
        for name in ("original_hue_deg", "graded_hue_deg", "applied_hue_deg", "delta_deg"):
            if not math.isfinite(getattr(self, name)):
                raise SkinModelError(f"{name} must be finite")
        if self.delta_deg < 0.0 or self.delta_deg > _MAX_HUE_DELTA_DEGREES:
            raise SkinModelError("delta_deg must lie inside [0, 180]")
        if not isinstance(self.allowed, bool):
            raise TypeError("allowed must be a bool")
        if len(self.adjusted_rgb) != 3 or any(
            not math.isfinite(value) or not 0.0 <= value <= 1.0 for value in self.adjusted_rgb
        ):
            raise SkinModelError("adjusted_rgb must be a finite sRGB triple")


def enforce_skin_hue_protection(
    original_rgb: Sequence[float],
    graded_rgb: Sequence[float],
    *,
    max_delta_deg: float = 12.0,
) -> HueProtectionResult:
    """Reject or bend a grade whose skin hue rotation exceeds the bound."""

    if not math.isfinite(max_delta_deg) or not 0.0 < max_delta_deg <= _MAX_HUE_DELTA_DEGREES:
        raise SkinModelError("max_delta_deg must lie inside (0, 180]")
    original_linear = _gamma_to_linear(original_rgb)
    graded_linear = _gamma_to_linear(graded_rgb)
    original = linear_srgb_to_oklch(original_linear)
    graded = linear_srgb_to_oklch(graded_linear)
    delta = abs(_shortest_angle(graded.hue_deg, original.hue_deg))
    if delta <= max_delta_deg or graded.chroma <= 1e-6:
        return HueProtectionResult(
            original_hue_deg=original.hue_deg,
            graded_hue_deg=graded.hue_deg,
            applied_hue_deg=graded.hue_deg,
            delta_deg=delta,
            allowed=True,
            adjusted_rgb=(float(graded_rgb[0]), float(graded_rgb[1]), float(graded_rgb[2])),
        )
    direction = _shortest_angle(graded.hue_deg, original.hue_deg)
    boundary = (original.hue_deg + math.copysign(max_delta_deg, direction)) % 360.0
    adjusted_linear = oklch_to_linear_srgb(
        Oklch(lightness=graded.lightness, chroma=graded.chroma, hue_deg=boundary)
    )
    adjusted = tuple(_encode_srgb(component) for component in adjusted_linear)
    return HueProtectionResult(
        original_hue_deg=original.hue_deg,
        graded_hue_deg=graded.hue_deg,
        applied_hue_deg=boundary,
        delta_deg=delta,
        allowed=False,
        adjusted_rgb=(adjusted[0], adjusted[1], adjusted[2]),
    )
