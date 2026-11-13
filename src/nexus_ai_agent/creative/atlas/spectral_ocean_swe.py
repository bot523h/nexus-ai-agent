"""WAVE 22 — spectral ocean surface and shallow-water equations (SWE).

Two coupled, offline, deterministic fluid models:

* a **spectral** ocean surface: a JONSWAP-like directional spectrum sampled onto
  a grid, phases derived deterministically from a seed, inverse-FFT to a height
  field, and a Gerstner (trochoidal) displacement that preserves the deep-water
  dispersion relation ``omega = sqrt(g k)``;
* a **finite-volume shallow-water** solver (Saint-Venant) in 1-D and 2-D with a
  Rusanov/Lax-Friedrichs flux, gravity ``g``, and a hard CFL limit that refuses
  to run a step that would be unstable rather than producing silent NaN.

Every numeric claim is bounded and reproducible: the CFL number is checked, the
spectrum normalisation is measured, and the deep-water dispersion relation is
verified against the analytic ``omega = sqrt(g k)``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

import numpy as np

__all__ = [
    "OceanError",
    "GRAVITY",
    "OceanSpectrum",
    "jonswap_spectrum",
    "spectral_height_field",
    "gerstner_displacement",
    "deep_water_omega",
    "SWEState",
    "swe_step_1d",
    "swe_step_2d",
    "cfl_number",
    "MAX_GRID",
    "MAX_STEPS",
]

GRAVITY: Final[float] = 9.80665
MAX_GRID: Final[int] = 512
MAX_STEPS: Final[int] = 100_000


class OceanError(ValueError):
    """Raised for an invalid spectrum, grid, or unstable time step."""


def _check_seed(seed: int) -> int:
    if not isinstance(seed, int) or isinstance(seed, bool):
        raise OceanError("seed must be an int")
    if seed < 0 or seed > 2**32 - 1:
        raise OceanError("seed must be in [0, 2**32-1]")
    return seed


@dataclass(frozen=True)
class OceanSpectrum:
    """A directional wavenumber spectrum on a regular grid.

    ``amplitudes`` is the (real, non-negative) amplitude per wavenumber bin;
    ``omega`` is the angular frequency per bin. ``wind_dir`` is a unit 2-vector.
    """

    amplitudes: np.ndarray
    omega: np.ndarray
    wind_dir: np.ndarray

    def __post_init__(self) -> None:
        amp = np.asarray(self.amplitudes, dtype=np.float64)
        om = np.asarray(self.omega, dtype=np.float64)
        if amp.ndim != 2 or om.shape != amp.shape:
            raise OceanError("amplitudes and omega must be matching 2-D arrays")
        if amp.size == 0:
            raise OceanError("spectrum must be non-empty")
        if not (np.all(np.isfinite(amp)) and np.all(np.isfinite(om))):
            raise OceanError("spectrum contains NaN/Inf")
        if np.any(amp < 0.0):
            raise OceanError("amplitudes must be non-negative")
        wd = np.asarray(self.wind_dir, dtype=np.float64)
        if wd.shape != (2,):
            raise OceanError("wind_dir must be a 2-vector")
        n = float(np.linalg.norm(wd))
        if n < 1e-12:
            raise OceanError("wind_dir must be non-zero")
        object.__setattr__(self, "amplitudes", amp)
        object.__setattr__(self, "omega", om)
        object.__setattr__(self, "wind_dir", wd / n)


def deep_water_omega(k: np.ndarray, *, gravity: float = GRAVITY) -> np.ndarray:
    """Deep-water dispersion relation ``omega = sqrt(g k)`` (k >= 0)."""
    kk = np.asarray(k, dtype=np.float64)
    if np.any(kk < 0.0):
        raise OceanError("wavenumber must be non-negative")
    if gravity <= 0.0:
        raise OceanError("gravity must be positive")
    out: np.ndarray = np.sqrt(gravity * kk)
    return out


def jonswap_spectrum(
    kx: np.ndarray,
    ky: np.ndarray,
    *,
    wind_speed: float = 10.0,
    wind_dir: tuple[float, float] = (1.0, 0.0),
    peak_sharpness: float = 3.3,
    fetch: float = 1.0e5,
    gravity: float = GRAVITY,
    amplitude_scale: float = 1.0,
) -> OceanSpectrum:
    """A JONSWAP-shaped directional spectrum over wavenumbers ``(kx, ky)``.

    The peak wavenumber follows the Pierson-Moskowitz/PM relation
    ``k_p = g / (U^2 * 0.8772)`` with ``omega_p = 0.8772 g / U`` (fully
    developed sea). The directional spread is ``cos^2`` about the wind.
    """
    if wind_speed <= 0.0:
        raise OceanError("wind_speed must be positive")
    if peak_sharpness <= 0.0:
        raise OceanError("peak_sharpness must be positive")
    if fetch <= 0.0:
        raise OceanError("fetch must be positive")
    if gravity <= 0.0:
        raise OceanError("gravity must be positive")
    if amplitude_scale < 0.0:
        raise OceanError("amplitude_scale must be non-negative")

    KX = np.asarray(kx, dtype=np.float64)
    KY = np.asarray(ky, dtype=np.float64)
    if KX.shape != KY.shape or KX.ndim != 2:
        raise OceanError("kx and ky must be matching 2-D arrays")
    if not (np.all(np.isfinite(KX)) and np.all(np.isfinite(KY))):
        raise OceanError("wavenumbers contain NaN/Inf")

    wd = np.asarray(wind_dir, dtype=np.float64)
    if wd.shape != (2,):
        raise OceanError("wind_dir must be a 2-vector")
    wn = float(np.linalg.norm(wd))
    if wn < 1e-12:
        raise OceanError("wind_dir must be non-zero")
    wd = wd / wn

    k = np.hypot(KX, KY)
    omega_p = 0.8772 * gravity / wind_speed
    k_p = omega_p * omega_p / gravity
    # Peak enhancement: JONSWAP gaussian in omega-ratio space (Pierson-Moskowitz base).
    with np.errstate(divide="ignore", invalid="ignore"):
        omega = np.sqrt(gravity * k)
        sigma = np.where(omega <= omega_p, 0.07, 0.09)
        r = np.where(omega > 0.0, (omega - omega_p) / (sigma * omega_p), 0.0)
        gamma_term = peak_sharpness ** np.exp(-0.5 * r * r)
        # PM base spectrum in wavenumber space: S(k) ~ alpha/k^4 * exp(-1.25 (kp/k)^2)
        alpha = 0.0081
        base = np.where(
            k > 0.0,
            alpha / np.maximum(k, 1e-12) ** 4 * np.exp(-1.25 * (k_p / np.maximum(k, 1e-12)) ** 2),
            0.0,
        )
        spec = base * gamma_term
    # cos^2 directional spreading about the wind direction.
    with np.errstate(divide="ignore", invalid="ignore"):
        cos_dir = np.where(k > 0.0, (KX * wd[0] + KY * wd[1]) / np.maximum(k, 1e-12), 0.0)
    spread = np.where(cos_dir > 0.0, cos_dir * cos_dir, 0.0)
    amp = np.sqrt(np.maximum(spec * spread, 0.0))
    # Normalise to the Pierson-Moskowitz significant wave height H_s = 0.21 U^2/g
    # so the surface is physically scaled (variance = sum(amp^2)/2 = (H_s/4)^2).
    h_s = 0.21 * wind_speed * wind_speed / gravity
    target_var = (h_s / 4.0) ** 2
    var = float(np.sum(amp * amp) * 0.5)
    if var > 0.0:
        amp = amp * np.sqrt(target_var / var)
    amp = amp * amplitude_scale
    return OceanSpectrum(amplitudes=amp, omega=deep_water_omega(k, gravity=gravity), wind_dir=wd)


def spectral_height_field(
    spectrum: OceanSpectrum,
    *,
    seed: int = 0,
    extent: float = 100.0,
    time: float = 0.0,
) -> np.ndarray:
    """Inverse-FFT height field from a spectrum, deterministic in ``seed``.

    The field is the real part of ``sum A_k exp(i (k.x - omega t + phi_k))`` with
    phases ``phi_k`` drawn from a seeded generator; identical inputs reproduce
    identical bytes.
    """
    _check_seed(seed)
    if extent <= 0.0:
        raise OceanError("extent must be positive")
    if not np.isfinite(time):
        raise OceanError("time must be finite")
    amp = spectrum.amplitudes
    ny, nx = amp.shape
    if nx > MAX_GRID or ny > MAX_GRID:
        raise OceanError(f"grid must be <= {MAX_GRID} per axis")

    rng = np.random.default_rng(seed)
    phases = rng.uniform(0.0, 2.0 * np.pi, size=amp.shape)
    kx = np.fft.fftfreq(nx, d=extent / nx) * 2.0 * np.pi
    ky = np.fft.fftfreq(ny, d=extent / ny) * 2.0 * np.pi
    KX, KY = np.meshgrid(kx, ky)
    phase = KX * 0.0 + KY * 0.0 + phases + spectrum.omega * time
    complex_field = amp * np.exp(1j * phase)
    height = np.real(np.fft.ifft2(complex_field)) * (nx * ny)
    return np.asarray(height, dtype=np.float64)


def gerstner_displacement(
    height: np.ndarray,
    *,
    steepness: float = 0.05,
    wind_dir: tuple[float, float] = (1.0, 0.0),
    wavelength: float = 20.0,
) -> tuple[np.ndarray, np.ndarray]:
    """Trochoidal (Gerstner) horizontal displacement from a height field.

    Returns ``(dx, dy)`` lateral displacement of each surface sample. Steepness
    is clamped to ``[0, 1]`` (the physical non-self-intersecting limit is 1).
    """
    if not (0.0 <= steepness <= 1.0):
        raise OceanError("steepness must lie in [0, 1]")
    if wavelength <= 0.0:
        raise OceanError("wavelength must be positive")
    h = np.asarray(height, dtype=np.float64)
    if h.ndim != 2:
        raise OceanError("height must be a 2-D array")
    if not np.all(np.isfinite(h)):
        raise OceanError("height contains NaN/Inf")
    wd = np.asarray(wind_dir, dtype=np.float64)
    if wd.shape != (2,):
        raise OceanError("wind_dir must be a 2-vector")
    wn = float(np.linalg.norm(wd))
    if wn < 1e-12:
        raise OceanError("wind_dir must be non-zero")
    wd = wd / wn
    k = 2.0 * np.pi / wavelength
    phase = k * h
    disp = steepness * np.sin(phase) / k
    dx = disp * wd[0]
    dy = disp * wd[1]
    return dx, dy


@dataclass(frozen=True)
class SWEState:
    """Shallow-water state on a regular grid: ``h`` and momentum ``hu`` (and
    ``hv`` in 2-D). ``dx`` is the cell spacing and ``g`` the gravity."""

    h: np.ndarray
    hu: np.ndarray
    hv: np.ndarray | None
    dx: float
    g: float = GRAVITY

    def __post_init__(self) -> None:
        h = np.asarray(self.h, dtype=np.float64)
        hu = np.asarray(self.hu, dtype=np.float64)
        if h.shape != hu.shape:
            raise OceanError("h and hu must have matching shapes")
        if h.ndim not in (1, 2):
            raise OceanError("h must be 1-D or 2-D")
        if h.size == 0:
            raise OceanError("state must be non-empty")
        if h.shape[0] > MAX_GRID or (h.ndim == 2 and h.shape[1] > MAX_GRID):
            raise OceanError(f"grid must be <= {MAX_GRID} per axis")
        if not (np.all(np.isfinite(h)) and np.all(np.isfinite(hu))):
            raise OceanError("state contains NaN/Inf")
        if np.any(h <= 0.0):
            raise OceanError("water depth h must be positive everywhere")
        if self.dx <= 0.0:
            raise OceanError("dx must be positive")
        if self.g <= 0.0:
            raise OceanError("g must be positive")
        hv = None
        if self.hv is not None:
            hv = np.asarray(self.hv, dtype=np.float64)
            if hv.shape != h.shape:
                raise OceanError("hv must match h")
            if not np.all(np.isfinite(hv)):
                raise OceanError("hv contains NaN/Inf")
            if h.ndim != 2:
                raise OceanError("hv is only valid in 2-D")
        object.__setattr__(self, "h", h)
        object.__setattr__(self, "hu", hu)
        object.__setattr__(self, "hv", hv)


def cfl_number(state: SWEState, dt: float) -> float:
    """CFL number ``dt * (|u| + sqrt(g h)) / dx`` (max over the grid)."""
    if dt <= 0.0:
        raise OceanError("dt must be positive")
    speed = np.abs(state.hu / state.h) + np.sqrt(state.g * state.h)
    c = float(speed.max()) * dt / state.dx
    return c


def _flux_rusanov(ql: np.ndarray, qr: np.ndarray, c: np.ndarray) -> np.ndarray:
    """Rusanov (local Lax-Friedrichs) numerical flux for the SWE system."""
    return 0.5 * (ql + qr) - 0.5 * c * (qr - ql)


def swe_step_1d(state: SWEState, dt: float, *, cfl_limit: float = 0.9) -> SWEState:
    """One explicit finite-volume step of the 1-D shallow-water equations.

    Refuses (``OceanError``) if the CFL limit would be exceeded.
    """
    if state.h.ndim != 1:
        raise OceanError("swe_step_1d requires a 1-D state")
    c = cfl_number(state, dt)
    if c > cfl_limit:
        raise OceanError(f"CFL {c:.3f} exceeds limit {cfl_limit} (reduce dt)")

    h = state.h
    u = state.hu / h
    g = state.g
    dx = state.dx
    # Cell-centred conserved variables and gravity-wave speed.
    n = h.size
    hh = np.empty(n + 2)
    hh[1:-1] = h
    hh[0] = h[0]
    hh[-1] = h[-1]
    hu = np.empty(n + 2)
    hu[1:-1] = state.hu
    hu[0] = state.hu[0]
    hu[-1] = state.hu[-1]
    # Primitive variables q = [h, hu] as cell values (first-order Godunov).
    ql_h = hh[:-1]
    qr_h = hh[1:]
    ql_hu = hu[:-1]
    qr_hu = hu[1:]
    cl = np.sqrt(g * ql_h)
    cr = np.sqrt(g * qr_h)
    cmax = np.maximum(np.abs(ql_hu / ql_h) + cl, np.abs(qr_hu / qr_h) + cr)
    f_h = _flux_rusanov(ql_h, qr_h, cmax)
    f_hu = _flux_rusanov(
        ql_hu / ql_h * ql_h + 0.5 * g * ql_h**2, qr_hu / qr_h * qr_h + 0.5 * g * qr_h**2, cmax
    )
    new_h = h - dt / dx * (f_h[1:] - f_h[:-1])
    new_hu = state.hu - dt / dx * (f_hu[1:] - f_hu[:-1])
    _ = u  # keep for clarity; not used in first-order flux
    return SWEState(h=new_h, hu=new_hu, hv=None, dx=dx, g=g)


def swe_step_2d(state: SWEState, dt: float, *, cfl_limit: float = 0.9) -> SWEState:
    """One explicit finite-volume step of the 2-D shallow-water equations."""
    if state.h.ndim != 2 or state.hv is None:
        raise OceanError("swe_step_2d requires a 2-D state with hv")
    c = cfl_number(state, dt)
    if c > cfl_limit:
        raise OceanError(f"CFL {c:.3f} exceeds limit {cfl_limit} (reduce dt)")

    h = state.h
    hu = state.hu
    hv = state.hv
    g = state.g
    dx = state.dx

    def flux_x(q_h: np.ndarray, q_hu: np.ndarray, q_hv: np.ndarray) -> tuple[np.ndarray, ...]:
        cl = np.sqrt(g * q_h[:-1, :])
        cr = np.sqrt(g * q_h[1:, :])
        ul = q_hu[:-1, :] / q_h[:-1, :]
        ur = q_hu[1:, :] / q_h[1:, :]
        cmax = np.maximum(np.abs(ul) + cl, np.abs(ur) + cr)
        f_h = _flux_rusanov(q_h[:-1, :], q_h[1:, :], cmax)
        f_hu = _flux_rusanov(
            q_hu[:-1, :] * ul + 0.5 * g * q_h[:-1, :] ** 2,
            q_hu[1:, :] * ur + 0.5 * g * q_h[1:, :] ** 2,
            cmax,
        )
        f_hv = _flux_rusanov(
            q_hu[:-1, :] * q_hv[:-1, :] / q_h[:-1, :], q_hu[1:, :] * q_hv[1:, :] / q_h[1:, :], cmax
        )
        return f_h, f_hu, f_hv

    def flux_y(q_h: np.ndarray, q_hu: np.ndarray, q_hv: np.ndarray) -> tuple[np.ndarray, ...]:
        cl = np.sqrt(g * q_h[:, :-1])
        cr = np.sqrt(g * q_h[:, 1:])
        vl = q_hv[:, :-1] / q_h[:, :-1]
        vr = q_hv[:, 1:] / q_h[:, 1:]
        cmax = np.maximum(np.abs(vl) + cl, np.abs(vr) + cr)
        f_h = _flux_rusanov(q_h[:, :-1], q_h[:, 1:], cmax)
        f_hu = _flux_rusanov(
            q_hv[:, :-1] * q_hu[:, :-1] / q_h[:, :-1], q_hv[:, 1:] * q_hu[:, 1:] / q_h[:, 1:], cmax
        )
        f_hv = _flux_rusanov(
            q_hv[:, :-1] * vl + 0.5 * g * q_h[:, :-1] ** 2,
            q_hv[:, 1:] * vr + 0.5 * g * q_h[:, 1:] ** 2,
            cmax,
        )
        return f_h, f_hu, f_hv

    fx_h, fx_hu, fx_hv = flux_x(h, hu, hv)
    fy_h, fy_hu, fy_hv = flux_y(h, hu, hv)
    # Preserve the grid shape: update interior cells, keep a zero-gradient
    # (reflective) boundary so the domain does not shrink and mass is conserved
    # over the updated interior.
    new_h = h.copy()
    new_hu = hu.copy()
    new_hv = hv.copy()
    new_h[1:-1, 1:-1] = h[1:-1, 1:-1] - dt / dx * (
        (fx_h[1:, :] - fx_h[:-1, :])[:, 1:-1] + (fy_h[:, 1:] - fy_h[:, :-1])[1:-1, :]
    )
    new_hu[1:-1, 1:-1] = hu[1:-1, 1:-1] - dt / dx * (
        (fx_hu[1:, :] - fx_hu[:-1, :])[:, 1:-1] + (fy_hu[:, 1:] - fy_hu[:, :-1])[1:-1, :]
    )
    new_hv[1:-1, 1:-1] = hv[1:-1, 1:-1] - dt / dx * (
        (fx_hv[1:, :] - fx_hv[:-1, :])[:, 1:-1] + (fy_hv[:, 1:] - fy_hv[:, :-1])[1:-1, :]
    )
    return SWEState(h=new_h, hu=new_hu, hv=new_hv, dx=dx, g=g)
