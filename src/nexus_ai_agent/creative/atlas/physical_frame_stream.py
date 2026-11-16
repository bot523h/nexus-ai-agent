"""WAVE 31 — physically driven frame stream for the ATLAS master render.

Every frame is computed from first principles, deterministically, with no external
test pattern and no per-frame file on disk:

* **Sea surface.** A fetch-limited JONSWAP spectrum
  (:func:`spectral_ocean_swe.jonswap_spectrum`) is evolved to ``t = frame_index / fps``
  by :func:`spectral_ocean_swe.spectral_height_field`. Surface normals are finite
  differences of that height field.
* **Water.** Schlick Fresnel reflectance of the sky seen in the mirror direction, a
  Blinn-Phong sun glint, and a dark deep-water body term.
* **Sky.** A single-scattering Rayleigh *approximation*: the optical depth scales as
  ``lambda^-4`` (0.0965 at 550 nm at sea level), the phase function is
  ``3/(16 pi) (1 + cos^2 gamma)``, and view and sun paths use air mass ``1/cos(zenith)``.
  It is not a radiative-transfer solver.
* **Output.** Linear RGB is exposed, tone-mapped and gamma-encoded to 8-bit sRGB.
  Pixels are produced in bands of :data:`BAND_ROWS` rows, so transient memory does not
  grow with the frame height.

The camera sits ``camera_height_m`` above mean sea level, looks along +z, and has a
vertical field of view of :data:`CAMERA_FOV_DEG`. The horizon is the middle row.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from functools import lru_cache
from typing import Final

import numpy as np

from nexus_ai_agent.creative.atlas import aces_hald_film_optic as film
from nexus_ai_agent.creative.atlas import spectral_ocean_swe as _ocean

__all__ = [
    "FrameError",
    "OceanState",
    "AtmosphereState",
    "spectrum_for",
    "generate_physical_atlas_frame_linear_rgb",
    "generate_physical_atlas_frame_rgb24",
    "MAX_FRAME_SIDE",
    "BAND_ROWS",
    "CAMERA_FOV_DEG",
]

MAX_FRAME_SIDE: Final[int] = 512
BAND_ROWS: Final[int] = 32
CAMERA_FOV_DEG: Final[float] = 50.0
WATER_F0: Final[float] = 0.02
RAYLEIGH_TAU_550: Final[float] = 0.0965
WAVELENGTHS_NM: Final[tuple[float, float, float]] = (650.0, 550.0, 450.0)
BODY_RGB: Final[tuple[float, float, float]] = (0.004, 0.012, 0.02)
GLINT_EXPONENT: Final[float] = 400.0
SUN_AZIMUTH_DEG: Final[float] = 20.0
GAMMA_INV: Final[float] = 1.0 / 2.2


class FrameError(ValueError):
    """Raised for an invalid frame request or an unresolvable physical state."""


def _finite_float(value: object, name: str) -> float:
    if isinstance(value, (bool, str, bytes)):
        raise FrameError(f"{name} must be a finite real number")
    try:
        num = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise FrameError(f"{name} must be a finite real number") from exc
    if not math.isfinite(num):
        raise FrameError(f"{name} must be finite")
    return num


def _finite_vec2(value: object, name: str) -> tuple[float, float]:
    try:
        items = list(value)  # type: ignore[call-overload]
    except TypeError as exc:
        raise FrameError(f"{name} must be a 2-vector") from exc
    if len(items) != 2:
        raise FrameError(f"{name} must be a 2-vector")
    return (_finite_float(items[0], f"{name}[0]"), _finite_float(items[1], f"{name}[1]"))


@dataclass(frozen=True)
class OceanState:
    """Sea state: wind, fetch, frame rate and the periodic grid that carries the surface.

    The peak wavenumber must sit at most at half the grid's Nyquist wavenumber
    (at least four cells per peak wavelength), otherwise the fetch is refused rather
    than rendered aliased.
    """

    wind_speed: float = 10.0
    fetch: float = 1.0e5
    wind_dir: tuple[float, float] = (1.0, 0.0)
    fps: float = 24.0
    grid: int = 256
    extent: float = 180.0

    def __post_init__(self) -> None:
        wind_speed = _finite_float(self.wind_speed, "wind_speed")
        if not (0.1 <= wind_speed <= 60.0):
            raise FrameError("wind_speed must be finite and in [0.1, 60] m/s")
        fetch = _finite_float(self.fetch, "fetch")
        if not (1.0e3 <= fetch <= 1.0e7):
            raise FrameError("fetch must be finite and in [1e3, 1e7] m")
        fps = _finite_float(self.fps, "fps")
        if not (0.0 < fps <= 240.0):
            raise FrameError("fps must be finite and in (0, 240]")
        if (
            not isinstance(self.grid, int)
            or isinstance(self.grid, bool)
            or not (16 <= self.grid <= _ocean.MAX_GRID)
        ):
            raise FrameError(f"grid must be in [16, {_ocean.MAX_GRID}]")
        extent = _finite_float(self.extent, "extent")
        if extent <= 0.0:
            raise FrameError("extent must be positive and finite")
        wd = _finite_vec2(self.wind_dir, "wind_dir")
        if math.hypot(wd[0], wd[1]) < 1e-12:
            raise FrameError("wind_dir must be non-zero")
        omega_p = 22.0 * ((_ocean.GRAVITY**2) / (wind_speed * fetch)) ** (1.0 / 3.0)
        k_p = omega_p * omega_p / _ocean.GRAVITY
        k_nyquist = math.pi * self.grid / extent
        if k_p > 0.5 * k_nyquist:
            raise FrameError(
                f"peak wavenumber {k_p:.3f} rad/m exceeds half the grid Nyquist "
                f"{k_nyquist:.3f} rad/m; increase grid or extent, or fetch"
            )


@dataclass(frozen=True)
class AtmosphereState:
    """Sun and camera. ``sun_elevation_deg`` is measured above the horizon."""

    sun_elevation_deg: float = 18.0
    turbidity: float = 1.0
    sun_intensity: float = 20.0
    camera_height_m: float = 3.0
    sun_azimuth_deg: float = SUN_AZIMUTH_DEG
    fov_deg: float = CAMERA_FOV_DEG

    def __post_init__(self) -> None:
        sun_elevation_deg = _finite_float(self.sun_elevation_deg, "sun_elevation_deg")
        if not (0.0 < sun_elevation_deg <= 90.0):
            raise FrameError("sun_elevation_deg must be finite and in (0, 90]")
        turbidity = _finite_float(self.turbidity, "turbidity")
        if turbidity <= 0.0:
            raise FrameError("turbidity must be positive and finite")
        sun_intensity = _finite_float(self.sun_intensity, "sun_intensity")
        if sun_intensity < 0.0:
            raise FrameError("sun_intensity must be non-negative and finite")
        camera_height_m = _finite_float(self.camera_height_m, "camera_height_m")
        if camera_height_m <= 0.0:
            raise FrameError("camera_height_m must be positive and finite")
        _finite_float(self.sun_azimuth_deg, "sun_azimuth_deg")
        fov_deg = _finite_float(self.fov_deg, "fov_deg")
        if not (0.0 < fov_deg < 180.0):
            raise FrameError("fov_deg must be finite and in (0, 180)")


@lru_cache(maxsize=8)
def spectrum_for(ocean_state: OceanState) -> _ocean.OceanSpectrum:
    """JONSWAP spectrum of ``ocean_state`` on its FFT wavenumber grid (cached)."""
    kx, ky = _ocean.fft_wavenumber_grid(ocean_state.grid, ocean_state.grid, ocean_state.extent)
    return _ocean.jonswap_spectrum(
        kx,
        ky,
        wind_speed=ocean_state.wind_speed,
        wind_dir=ocean_state.wind_dir,
        fetch=ocean_state.fetch,
    )


def _sun_direction(atmos: AtmosphereState) -> np.ndarray:
    el = math.radians(atmos.sun_elevation_deg)
    az = math.radians(atmos.sun_azimuth_deg)
    return np.array([math.cos(el) * math.sin(az), math.sin(el), math.cos(el) * math.cos(az)])


def _sky_rgb(
    directions: np.ndarray, sun: np.ndarray, intensity: float, turbidity: float
) -> np.ndarray:
    """Linear RGB of the single-scattering sky along unit ``directions`` (shape (..., 3))."""
    zenith_cos = np.maximum(directions[..., 1], 0.02)
    cos_gamma = np.clip(np.sum(directions * sun, axis=-1), -1.0, 1.0)
    phase = (3.0 / (16.0 * math.pi)) * (1.0 + cos_gamma * cos_gamma)
    sun_cos = max(float(sun[1]), 0.02)
    out = np.empty(directions.shape[:-1] + (3,), dtype=np.float64)
    for channel, wavelength in enumerate(WAVELENGTHS_NM):
        tau = RAYLEIGH_TAU_550 * turbidity * (550.0 / wavelength) ** 4
        view_path = tau / zenith_cos
        thin = -np.expm1(-view_path) / view_path
        out[..., channel] = intensity * phase * tau * thin * math.exp(-tau / sun_cos)
    return out


def _bilinear_normals(normals: np.ndarray, fx: np.ndarray, fz: np.ndarray) -> np.ndarray:
    """Periodic bilinear lookup of unit normals at grid coordinates ``(fx, fz)``."""
    n = normals.shape[0]
    x0 = np.floor(fx)
    z0 = np.floor(fz)
    wx = (fx - x0)[:, None]
    wz = (fz - z0)[:, None]
    ix0 = x0.astype(np.int64) % n
    iz0 = z0.astype(np.int64) % n
    ix1 = (ix0 + 1) % n
    iz1 = (iz0 + 1) % n
    top = (1.0 - wx) * normals[iz0, ix0] + wx * normals[iz0, ix1]
    bottom = (1.0 - wx) * normals[iz1, ix0] + wx * normals[iz1, ix1]
    blended = (1.0 - wz) * top + wz * bottom
    out: np.ndarray = blended / np.linalg.norm(blended, axis=-1, keepdims=True)
    return out


def _tone_map(linear: np.ndarray) -> np.ndarray:
    mapped = np.clip(1.0 - np.exp(-linear), 0.0, 1.0)
    return film.linear_to_srgb_oetf(mapped)


def _check_frame_request(frame_idx: int, total_frames: int, width: int, height: int) -> None:
    if not isinstance(frame_idx, int) or isinstance(frame_idx, bool):
        raise FrameError("frame_idx must be an int")
    if not isinstance(total_frames, int) or isinstance(total_frames, bool) or total_frames < 1:
        raise FrameError("total_frames must be >= 1")
    if not (0 <= frame_idx < total_frames):
        raise FrameError(f"frame_idx must be in [0, {total_frames})")
    for name, value in (("width", width), ("height", height)):
        if not isinstance(value, int) or isinstance(value, bool):
            raise FrameError(f"{name} must be an int")
        if not (8 <= value <= MAX_FRAME_SIDE):
            raise FrameError(f"{name} must be in [8, {MAX_FRAME_SIDE}]")


def generate_physical_atlas_frame_linear_rgb(
    frame_idx: int,
    width: int = 64,
    height: int = 64,
    ocean: OceanState | None = None,
    atmo: AtmosphereState | None = None,
    *,
    total_frames: int | None = None,
    seed: int = 0,
) -> np.ndarray:
    """Render frame ``frame_idx`` as non-negative ``float64`` linear sRGB/Rec.709 radiance.

    Returns a ``(height, width, 3)`` array of scene-referred linear radiance before
    any tone-mapping or display OETF.
    """
    ocean_state = ocean if ocean is not None else OceanState()
    atmosphere_state = atmo if atmo is not None else AtmosphereState()
    if not isinstance(ocean_state, OceanState):
        raise FrameError("ocean must be an OceanState")
    if not isinstance(atmosphere_state, AtmosphereState):
        raise FrameError("atmo must be an AtmosphereState")
    effective_total = (
        total_frames
        if total_frames is not None
        else (frame_idx + 1 if isinstance(frame_idx, int) and frame_idx >= 0 else 1)
    )
    _check_frame_request(frame_idx, effective_total, width, height)
    spectrum = spectrum_for(ocean_state)
    time_s = frame_idx / ocean_state.fps
    field = _ocean.spectral_height_field(
        spectrum, seed=seed, extent=ocean_state.extent, time=time_s
    )
    n = field.shape[0]
    dx = ocean_state.extent / n
    dh_dz = np.gradient(field, dx, axis=0)
    dh_dx = np.gradient(field, dx, axis=1)
    normals = np.stack((-dh_dx, np.ones_like(field), -dh_dz), axis=-1)
    normals /= np.linalg.norm(normals, axis=-1, keepdims=True)

    sun = _sun_direction(atmosphere_state)
    intensity = atmosphere_state.sun_intensity if sun[1] > 0.0 else 0.0
    turbidity = atmosphere_state.turbidity
    body = np.array(BODY_RGB) * intensity
    tan_half = math.tan(math.radians(atmosphere_state.fov_deg) / 2.0)
    aspect = width / height
    camera_h = atmosphere_state.camera_height_m
    out = np.empty((height, width, 3), dtype=np.float64)
    cols = np.arange(width)
    for row0 in range(0, height, BAND_ROWS):
        row1 = min(height, row0 + BAND_ROWS)
        rows = np.arange(row0, row1)
        dirs = np.empty((row1 - row0, width, 3), dtype=np.float64)
        dirs[..., 0] = ((2.0 * (cols + 0.5) / width - 1.0) * tan_half * aspect)[None, :]
        dirs[..., 1] = ((1.0 - 2.0 * (rows + 0.5) / height) * tan_half)[:, None]
        dirs[..., 2] = 1.0
        dirs /= np.linalg.norm(dirs, axis=-1, keepdims=True)
        linear = np.empty_like(dirs)
        below = dirs[..., 1] < 0.0
        sky = ~below
        if np.any(sky):
            linear[sky] = _sky_rgb(dirs[sky], sun, intensity, turbidity)
        if np.any(below):
            d = dirs[below]
            t_hit = camera_h / (-d[:, 1])
            px = np.mod(t_hit * d[:, 0], ocean_state.extent)
            pz = np.mod(t_hit * d[:, 2], ocean_state.extent)
            nrm = _bilinear_normals(normals, px / dx, pz / dx)
            cos_i = np.clip(-np.sum(d * nrm, axis=-1), 0.0, 1.0)
            fresnel = WATER_F0 + (1.0 - WATER_F0) * (1.0 - cos_i) ** 5
            dot_dn = np.sum(d * nrm, axis=-1, keepdims=True)
            reflected = d - 2.0 * dot_dn * nrm
            mirror = _sky_rgb(reflected, sun, intensity, turbidity)
            diffuse = body[None, :] * np.maximum(np.sum(nrm * sun, axis=-1), 0.0)[:, None]
            half_vec = sun[None, :] - d
            half_vec /= np.linalg.norm(half_vec, axis=-1, keepdims=True)
            glint = intensity * np.maximum(np.sum(nrm * half_vec, axis=-1), 0.0) ** GLINT_EXPONENT
            linear[below] = (
                fresnel[:, None] * mirror + (1.0 - fresnel)[:, None] * diffuse + glint[:, None]
            )
        out[row0:row1] = np.maximum(linear, 0.0)
    return out


def generate_physical_atlas_frame_rgb24(
    frame_idx: int,
    total_frames: int,
    width: int,
    height: int,
    ocean_state: OceanState,
    atmosphere_state: AtmosphereState,
    seed: int,
) -> bytes:
    """Render frame ``frame_idx`` of ``total_frames`` as raw ``rgb24`` bytes.

    The result is deterministic in every argument, has length ``width * height * 3``
    and is row-major ``(height, width, 3)`` ``uint8``. Changing the wind, the fetch or
    the time index changes the surface and therefore the pixels.
    """
    linear = generate_physical_atlas_frame_linear_rgb(
        frame_idx,
        width=width,
        height=height,
        ocean=ocean_state,
        atmo=atmosphere_state,
        total_frames=total_frames,
        seed=seed,
    )
    frame = np.empty((height, width, 3), dtype=np.uint8)
    for row0 in range(0, height, BAND_ROWS):
        row1 = min(height, row0 + BAND_ROWS)
        encoded = _tone_map(linear[row0:row1])
        frame[row0:row1] = np.round(encoded * 255.0).astype(np.uint8)
    return frame.tobytes()
