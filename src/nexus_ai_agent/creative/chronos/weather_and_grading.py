"""Physical precipitation, ASC-CDL/3D LUT grading, and broadcast legalization."""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

import numpy as np
from numpy.typing import NDArray

from .contracts import KernelError

FloatArray = NDArray[np.float64]
_LUMA = np.array([0.2126, 0.7152, 0.0722], dtype=np.float64)


@dataclass(frozen=True, slots=True)
class PrecipitationReceipt:
    layer: FloatArray
    vertical_streak_length_px: float
    total_streak_length_px: float
    beer_lambert_attenuation: float
    rendered_particle_count: int


@dataclass(frozen=True, slots=True)
class CubeLUT:
    size: int
    values: FloatArray  # [blue, green, red, output-channel], .cube red varies fastest
    domain_min: tuple[float, float, float] = (0.0, 0.0, 0.0)
    domain_max: tuple[float, float, float] = (1.0, 1.0, 1.0)


@dataclass(frozen=True, slots=True)
class BroadcastVideoQCReceipt:
    image: FloatArray
    illegal_pixel_fraction_before: float
    illegal_pixel_fraction_after: float
    sobel_structure_correlation: float


def render_physical_precipitation_layer(
    width_px: int,
    height_px: int,
    focal_length_y_px: float,
    particle_depth_m: float,
    terminal_velocity_m_s: float,
    *,
    frame_rate_fps: float,
    shutter_angle_deg: float = 180.0,
    wind_velocity_m_s: tuple[float, ...] = (0.0, 0.0),
    particle_count: int = 64,
    extinction_per_m: float = 0.0,
    seed: int = 0,
    particle_rgb: tuple[float, float, float] = (1.0, 1.0, 1.0),
) -> PrecipitationReceipt:
    """Render deterministic anti-aliased precipitation streaks in scene-linear light."""
    wind = np.asarray(wind_velocity_m_s, dtype=np.float64)
    color = np.asarray(particle_rgb, dtype=np.float64)
    if (
        min(width_px, height_px, particle_count) <= 0
        or not np.isfinite(focal_length_y_px)
        or focal_length_y_px <= 0
        or not np.isfinite(particle_depth_m)
        or particle_depth_m <= 0
        or not np.isfinite(terminal_velocity_m_s)
        or terminal_velocity_m_s < 0
        or not np.isfinite(frame_rate_fps)
        or frame_rate_fps <= 0
        or not np.isfinite(shutter_angle_deg)
        or not 0 < shutter_angle_deg <= 360
        or wind.shape != (2,)
        or np.any(~np.isfinite(wind))
        or not np.isfinite(extinction_per_m)
        or extinction_per_m < 0
        or color.shape != (3,)
        or np.any(~np.isfinite(color))
        or np.any(color < 0)
    ):
        raise KernelError("invalid physical precipitation parameters")
    exposure_s = shutter_angle_deg / 360.0 / frame_rate_fps
    vertical_length = focal_length_y_px * terminal_velocity_m_s * exposure_s / particle_depth_m
    displacement = np.array(
        [
            focal_length_y_px * wind[0] * exposure_s / particle_depth_m,
            focal_length_y_px * (terminal_velocity_m_s + wind[1]) * exposure_s / particle_depth_m,
        ]
    )
    total_length = float(np.linalg.norm(displacement))
    attenuation = float(np.exp(-extinction_per_m * particle_depth_m))
    layer = np.zeros((height_px, width_px, 3), dtype=np.float64)
    random = np.random.default_rng(seed)
    for _ in range(particle_count):
        x_start = float(random.uniform(0, width_px - 1))
        y_start = float(random.uniform(0, height_px - 1))
        steps = max(2, int(np.ceil(total_length * 2)) + 1)
        for fraction in np.linspace(0.0, 1.0, steps):
            x = x_start + displacement[0] * fraction
            y = y_start + displacement[1] * fraction
            x0, y0 = int(np.floor(x)), int(np.floor(y))
            tx, ty = x - x0, y - y0
            for px, py, weight in (
                (x0, y0, (1 - tx) * (1 - ty)),
                (x0 + 1, y0, tx * (1 - ty)),
                (x0, y0 + 1, (1 - tx) * ty),
                (x0 + 1, y0 + 1, tx * ty),
            ):
                if 0 <= px < width_px and 0 <= py < height_px:
                    layer[py, px] += color * (attenuation * weight / steps)
    return PrecipitationReceipt(
        layer, float(vertical_length), total_length, attenuation, particle_count
    )


def _validate_lut(lut: CubeLUT) -> None:
    if (
        lut.size < 2
        or np.asarray(lut.values).shape != (lut.size, lut.size, lut.size, 3)
        or np.any(~np.isfinite(lut.values))
        or len(lut.domain_min) != 3
        or len(lut.domain_max) != 3
        or np.any(~np.isfinite(lut.domain_min))
        or np.any(~np.isfinite(lut.domain_max))
        or np.any(np.asarray(lut.domain_max) <= np.asarray(lut.domain_min))
    ):
        raise KernelError("invalid 3D cube LUT lattice or domain")


def identity_cube_lut(size: int = 2) -> CubeLUT:
    if size < 2:
        raise KernelError("3D LUT size must be at least two")
    axis = np.linspace(0.0, 1.0, size)
    values = np.empty((size, size, size, 3), dtype=np.float64)
    for blue, b in enumerate(axis):
        for green, g in enumerate(axis):
            for red, r in enumerate(axis):
                values[blue, green, red] = (r, g, b)
    return CubeLUT(size, values)


def parse_cube_lut(text: str) -> CubeLUT:
    """Parse deterministic .cube 3D LUT data (red-fastest lattice ordering)."""
    size: int | None = None
    domain_min, domain_max = (0.0, 0.0, 0.0), (1.0, 1.0, 1.0)
    samples: list[tuple[float, float, float]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.upper().startswith("TITLE"):
            continue
        fields = line.split()
        directive = fields[0].upper()
        if directive == "LUT_1D_SIZE":
            raise KernelError("1D .cube LUTs are not supported by the 3D parser")
        if directive == "LUT_3D_SIZE":
            if len(fields) != 2 or size is not None:
                raise KernelError("malformed or duplicate LUT_3D_SIZE directive")
            try:
                size = int(fields[1])
            except ValueError as exc:
                raise KernelError("LUT_3D_SIZE must be an integer") from exc
            if size < 2:
                raise KernelError("3D LUT size must be at least two")
            continue
        if directive in {"DOMAIN_MIN", "DOMAIN_MAX"}:
            if len(fields) != 4:
                raise KernelError(f"{directive} requires three values")
            try:
                values = tuple(float(value) for value in fields[1:])
            except ValueError as exc:
                raise KernelError(f"invalid {directive} values") from exc
            if not np.all(np.isfinite(values)):
                raise KernelError(f"invalid {directive} values")
            if directive == "DOMAIN_MIN":
                domain_min = cast(tuple[float, float, float], values)
            else:
                domain_max = cast(tuple[float, float, float], values)
            continue
        try:
            sample = tuple(float(value) for value in fields)
        except ValueError as exc:
            raise KernelError("invalid .cube lattice sample") from exc
        if len(sample) != 3 or not np.all(np.isfinite(sample)):
            raise KernelError("each .cube lattice sample must contain three finite values")
        samples.append(sample)
    if size is None or len(samples) != size**3:
        raise KernelError("3D LUT must declare a size and exactly size cubed samples")
    ordered = np.asarray(samples, dtype=np.float64).reshape(size, size, size, 3)
    result = CubeLUT(size, ordered, domain_min, domain_max)
    _validate_lut(result)
    return result


def serialize_cube_lut(lut: CubeLUT, *, title: str = "NEXUS Chronos LUT") -> str:
    _validate_lut(lut)
    lines = [f'TITLE "{title.replace(chr(34), chr(39))}"', f"LUT_3D_SIZE {lut.size}"]
    lines.append("DOMAIN_MIN " + " ".join(f"{value:.9g}" for value in lut.domain_min))
    lines.append("DOMAIN_MAX " + " ".join(f"{value:.9g}" for value in lut.domain_max))
    for blue in range(lut.size):
        for green in range(lut.size):
            for red in range(lut.size):
                lines.append(" ".join(f"{value:.9g}" for value in lut.values[blue, green, red]))
    return "\n".join(lines) + "\n"


def _apply_lut(rgb: FloatArray, lut: CubeLUT) -> FloatArray:
    _validate_lut(lut)
    domain_min = np.asarray(lut.domain_min)
    domain_span = np.asarray(lut.domain_max) - domain_min
    scaled = (rgb - domain_min) / domain_span * (lut.size - 1)
    scaled = np.clip(scaled, 0, lut.size - 1)
    base = np.floor(scaled).astype(np.int64)
    base = np.minimum(base, lut.size - 2)
    fraction = scaled - base
    flat_base, flat_fraction = base.reshape(-1, 3), fraction.reshape(-1, 3)
    output = np.zeros((flat_base.shape[0], 3), dtype=np.float64)
    for blue in (0, 1):
        for green in (0, 1):
            for red in (0, 1):
                weight = (
                    (flat_fraction[:, 0] if red else 1 - flat_fraction[:, 0])
                    * (flat_fraction[:, 1] if green else 1 - flat_fraction[:, 1])
                    * (flat_fraction[:, 2] if blue else 1 - flat_fraction[:, 2])
                )
                output += (
                    lut.values[
                        flat_base[:, 2] + blue,
                        flat_base[:, 1] + green,
                        flat_base[:, 0] + red,
                    ]
                    * weight[:, None]
                )
    return output.reshape(rgb.shape)


def apply_asc_cdl_and_3d_lut(
    scene_linear_rgb: FloatArray,
    *,
    slope: tuple[float, ...] = (1.0, 1.0, 1.0),
    offset: tuple[float, ...] = (0.0, 0.0, 0.0),
    power: tuple[float, ...] = (1.0, 1.0, 1.0),
    saturation: float = 1.0,
    lut: CubeLUT | None = None,
) -> FloatArray:
    """Apply ASC-CDL slope/offset/power/saturation and optional trilinear 3D LUT."""
    rgb = np.asarray(scene_linear_rgb, dtype=np.float64)
    slope_array, offset_array, power_array = map(
        lambda value: np.asarray(value, dtype=np.float64), (slope, offset, power)
    )
    if (
        rgb.ndim < 1
        or rgb.shape[-1] != 3
        or np.any(~np.isfinite(rgb))
        or any(
            array.shape != (3,) or np.any(~np.isfinite(array))
            for array in (slope_array, offset_array, power_array)
        )
        or np.any(slope_array < 0)
        or np.any(power_array <= 0)
        or not np.isfinite(saturation)
        or saturation < 0
    ):
        raise KernelError("invalid ASC-CDL RGB data or controls")
    cdl = np.power(np.maximum(rgb * slope_array + offset_array, 0.0), power_array)
    luminance = cdl @ _LUMA
    saturated = luminance[..., None] + saturation * (cdl - luminance[..., None])
    return (
        _apply_lut(saturated, lut) if lut is not None else np.asarray(saturated, dtype=np.float64)
    )


def _sobel(luminance: FloatArray) -> tuple[FloatArray, FloatArray]:
    p = np.pad(luminance, ((1, 1), (1, 1)), mode="edge")
    gx = (
        -p[:-2, :-2] + p[:-2, 2:] - 2 * p[1:-1, :-2] + 2 * p[1:-1, 2:] - p[2:, :-2] + p[2:, 2:]
    ) / 8.0
    gy = (
        -p[:-2, :-2] - 2 * p[:-2, 1:-1] - p[:-2, 2:] + p[2:, :-2] + 2 * p[2:, 1:-1] + p[2:, 2:]
    ) / 8.0
    return gx, gy


def legalize_broadcast_rgb_ebu_r103(
    rgb: FloatArray,
) -> BroadcastVideoQCReceipt:
    """Compress RGB excursions toward legal luminance/chroma while preserving hue."""
    image = np.asarray(rgb, dtype=np.float64)
    if image.ndim != 3 or image.shape[2] != 3 or np.any(~np.isfinite(image)):
        raise KernelError("broadcast legalizer requires a finite H x W x 3 RGB image")
    illegal = np.any((image < 0.0) | (image > 1.0), axis=2)
    fraction_before = float(np.mean(illegal))
    luminance = image @ _LUMA
    low_luma, high_luma = float(np.min(luminance)), float(np.max(luminance))
    if low_luma < 0.0 or high_luma > 1.0:
        if high_luma - low_luma > 1e-15:
            # An affine luminance shoulder preserves Sobel structure while
            # mapping the observed signal range to legal black/white.
            legal_luminance = (luminance - low_luma) / (high_luma - low_luma)
        else:
            legal_luminance = np.clip(luminance, 0.0, 1.0)
    else:
        legal_luminance = luminance.copy()
    chroma = image - luminance[..., None]
    scale = np.ones_like(luminance)
    for channel in range(3):
        component = chroma[..., channel]
        positive = component > 1e-15
        negative = component < -1e-15
        limit = np.ones_like(scale)
        limit[positive] = (1.0 - legal_luminance[positive]) / component[positive]
        limit[negative] = (0.0 - legal_luminance[negative]) / component[negative]
        scale = np.minimum(scale, np.clip(limit, 0.0, 1.0))
    # A quadratic shoulder eases chroma compression near gamut boundaries.
    scale = np.clip(scale - 0.15 * (1.0 - scale) ** 2, 0.0, 1.0)
    output = np.clip(legal_luminance[..., None] + scale[..., None] * chroma, 0.0, 1.0)
    illegal_after = float(np.mean(np.any((output < 0.0) | (output > 1.0), axis=2)))
    before_luma, after_luma = image @ _LUMA, output @ _LUMA
    before_gx, before_gy = _sobel(before_luma)
    after_gx, after_gy = _sobel(after_luma)
    before_gradient = np.concatenate((before_gx.ravel(), before_gy.ravel()))
    after_gradient = np.concatenate((after_gx.ravel(), after_gy.ravel()))
    if np.std(before_gradient) <= 1e-15 or np.std(after_gradient) <= 1e-15:
        correlation = 1.0 if np.allclose(before_gradient, after_gradient) else 0.0
    else:
        correlation = float(np.corrcoef(before_gradient, after_gradient)[0, 1])
    return BroadcastVideoQCReceipt(
        np.asarray(output, dtype=np.float64), fraction_before, illegal_after, correlation
    )
