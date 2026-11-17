"""WAVE 22 — spectral ocean surface and shallow-water equations (SWE).

Two coupled, offline, deterministic fluid models:

* a **spectral** ocean surface: a fetch-limited JONSWAP directional spectrum
  (Hasselmann et al. 1973) sampled onto an FFT wavenumber grid, a seeded
  Hermitian construction evolved in time and inverted to a real height field
  with ``irfft2``, and a Gerstner (trochoidal) displacement that preserves the
  deep-water dispersion relation ``omega = sqrt(g k)``;
* a **finite-volume shallow-water** solver (Saint-Venant) in 1-D and 2-D with a
  Rusanov/Lax-Friedrichs flux, gravity ``g``, and a hard CFL limit that refuses
  to run a step that would be unstable rather than producing silent NaN.

Every numeric claim is bounded and reproducible: the CFL number is checked, the
surface variance is the integral of the spectrum (so fetch changes it), and the
deep-water dispersion relation is verified against the analytic ``omega = sqrt(g k)``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final

import numpy as np

__all__ = [
    "OceanError",
    "GRAVITY",
    "OceanSpectrum",
    "jonswap_spectrum",
    "fft_wavenumber_grid",
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


def _finite_float(value: object, name: str) -> float:
    if isinstance(value, (bool, str, bytes)):
        raise OceanError(f"{name} must be a finite real number")
    try:
        num = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise OceanError(f"{name} must be a finite real number") from exc
    if not math.isfinite(num):
        raise OceanError(f"{name} must be finite")
    return num


def _float_array(value: object, name: str) -> np.ndarray:
    if isinstance(value, (bool, str, bytes)):
        raise OceanError(f"{name} must contain real numbers")
    if isinstance(value, (list, tuple)) and any(isinstance(x, (bool, str, bytes)) for x in value):
        raise OceanError(f"{name} must contain real numbers")
    try:
        raw = np.asarray(value)
        if raw.dtype.kind not in ("i", "u", "f"):
            raise OceanError(f"{name} must contain real numbers")
        arr = np.asarray(raw, dtype=np.float64)
    except (TypeError, ValueError) as exc:
        raise OceanError(f"{name} must contain real numbers") from exc
    if not np.all(np.isfinite(arr)):
        raise OceanError(f"{name} contains NaN/Inf")
    return arr


def _check_seed(seed: int) -> int:
    if not isinstance(seed, int) or isinstance(seed, bool):
        raise OceanError("seed must be an int")
    if seed < 0 or seed > 2**32 - 1:
        raise OceanError("seed must be in [0, 2**32-1]")
    return seed


@dataclass(frozen=True)
class OceanSpectrum:
    """A directional wavenumber spectrum on a regular grid.

    ``amplitudes`` is the (real, non-negative) energy amplitude per wavenumber bin,
    ``a_k = sqrt(S(k) dk_x dk_y)``, so ``variance = sum(a_k**2)`` is the total surface
    variance m0 in m^2. ``omega`` is the angular frequency per bin and ``wind_dir``
    is a unit 2-vector. ``peak_omega``, ``peak_wavenumber`` and ``phillips_alpha``
    are the fetch-dependent JONSWAP parameters the spectrum was built from.
    """

    amplitudes: np.ndarray
    omega: np.ndarray
    wind_dir: np.ndarray
    peak_omega: float = 0.0
    peak_wavenumber: float = 0.0
    phillips_alpha: float = 0.0

    @property
    def variance(self) -> float:
        """Total surface variance m0 = sum of squared bin amplitudes (m^2)."""
        return float(np.sum(self.amplitudes * self.amplitudes))

    @property
    def significant_wave_height(self) -> float:
        """Significant wave height ``H_s = 4 sqrt(m0)`` in metres."""
        return 4.0 * float(np.sqrt(self.variance))

    def __post_init__(self) -> None:
        amp = _float_array(self.amplitudes, "amplitudes")
        om = _float_array(self.omega, "omega")
        if amp.ndim != 2 or om.shape != amp.shape:
            raise OceanError("amplitudes and omega must be matching 2-D arrays")
        if amp.size == 0:
            raise OceanError("spectrum must be non-empty")
        if np.any(amp < 0.0):
            raise OceanError("amplitudes must be non-negative")
        wd = _float_array(self.wind_dir, "wind_dir")
        if wd.shape != (2,):
            raise OceanError("wind_dir must be a 2-vector")
        n = float(np.linalg.norm(wd))
        if n < 1e-12:
            raise OceanError("wind_dir must be non-zero")
        for name, val in (
            ("peak_omega", self.peak_omega),
            ("peak_wavenumber", self.peak_wavenumber),
            ("phillips_alpha", self.phillips_alpha),
        ):
            _finite_float(val, name)
        object.__setattr__(self, "amplitudes", amp)
        object.__setattr__(self, "omega", om)
        object.__setattr__(self, "wind_dir", wd / n)


def deep_water_omega(k: np.ndarray, *, gravity: float = GRAVITY) -> np.ndarray:
    """Deep-water dispersion relation ``omega = sqrt(g k)`` (k >= 0)."""
    kk = _float_array(k, "wavenumber")
    if np.any(kk < 0.0):
        raise OceanError("wavenumber must be non-negative")
    gravity = _finite_float(gravity, "gravity")
    if gravity <= 0.0:
        raise OceanError("gravity must be positive and finite")
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
    """Fetch-limited JONSWAP directional energy spectrum on wavenumbers ``(kx, ky)``.

    Hasselmann et al. (1973), with ``g = gravity``, ``U = wind_speed`` and
    ``F = fetch``:

    * dimensionless fetch ``x = max(g F / U^2, 1)``;
    * Phillips constant ``alpha = 0.076 x^-0.22``;
    * peak angular frequency ``omega_p = 22 (g^2 / (U F))^(1/3)``, peak wavenumber
      ``k_p = omega_p^2 / g``;
    * ``S(omega) = alpha g^2 omega^-5 exp(-1.25 (omega_p/omega)^4) gamma^r`` with
      ``r = exp(-(omega - omega_p)^2 / (2 sigma^2 omega_p^2))``, ``sigma = 0.07``
      below the peak and ``0.09`` above it, ``gamma = peak_sharpness``.

    The spectrum is converted to wavenumber with the deep-water Jacobian
    ``d omega / dk = g / (2 omega)`` and a ``cos^2`` directional spread about the
    wind, normalised over the full circle. The total variance is the integral of
    ``S(omega)``, so a longer fetch raises the energy and lowers the peak frequency.
    The bin spacing is inferred from ``kx``/``ky``. To feed
    :func:`spectral_height_field`, build the grid with :func:`fft_wavenumber_grid`
    using the same ``extent``, so the bins are the FFT bins of the height field.
    """
    wind_speed = _finite_float(wind_speed, "wind_speed")
    if wind_speed <= 0.0:
        raise OceanError("wind_speed must be positive and finite")
    peak_sharpness = _finite_float(peak_sharpness, "peak_sharpness")
    if peak_sharpness <= 0.0:
        raise OceanError("peak_sharpness must be positive and finite")
    fetch = _finite_float(fetch, "fetch")
    if fetch <= 0.0:
        raise OceanError("fetch must be positive and finite")
    gravity = _finite_float(gravity, "gravity")
    if gravity <= 0.0:
        raise OceanError("gravity must be positive and finite")
    amplitude_scale = _finite_float(amplitude_scale, "amplitude_scale")
    if amplitude_scale < 0.0:
        raise OceanError("amplitude_scale must be non-negative and finite")

    KX = _float_array(kx, "kx")
    KY = _float_array(ky, "ky")
    if KX.shape != KY.shape or KX.ndim != 2:
        raise OceanError("kx and ky must be matching 2-D arrays")

    wd = _float_array(wind_dir, "wind_dir")
    if wd.shape != (2,):
        raise OceanError("wind_dir must be a 2-vector")
    wn = float(np.linalg.norm(wd))
    if wn < 1e-12:
        raise OceanError("wind_dir must be non-zero")
    wd = wd / wn

    k = np.hypot(KX, KY)
    u10 = max(wind_speed, 0.1)
    x_tilde = max(gravity * fetch / (u10 * u10), 1.0)
    alpha_jonswap = 0.076 * x_tilde**-0.22
    omega_p = 22.0 * ((gravity * gravity) / (u10 * fetch)) ** (1.0 / 3.0)
    k_p = omega_p * omega_p / gravity
    dkx, dky = _grid_spacing(KX, KY)
    with np.errstate(divide="ignore", invalid="ignore"):
        omega = np.sqrt(gravity * k)
        sigma = np.where(omega <= omega_p, 0.07, 0.09)
        peak_enhancement = peak_sharpness ** np.exp(
            -((omega - omega_p) ** 2) / (2.0 * sigma**2 * omega_p**2)
        )
        safe_omega = np.where(omega > 0.0, omega, 1.0)
        base_omega = (
            alpha_jonswap
            * gravity
            * gravity
            * safe_omega**-5.0
            * np.exp(-1.25 * (omega_p / safe_omega) ** 4)
        )
        spectrum_omega = np.where(omega > 0.0, base_omega * peak_enhancement, 0.0)
        # Deep-water Jacobian d(omega)/dk = g / (2 omega) turns S(omega) into S(k).
        spectrum_k = spectrum_omega * gravity / (2.0 * safe_omega)
        safe_k = np.where(k > 0.0, k, 1.0)
        cos_dir = np.where(k > 0.0, (KX * wd[0] + KY * wd[1]) / safe_k, 0.0)
        directional = np.where(cos_dir > 0.0, (2.0 / np.pi) * cos_dir * cos_dir, 0.0)
        density = np.where(k > 0.0, spectrum_k * directional / safe_k, 0.0)
    energy_amp = np.sqrt(np.maximum(density * dkx * dky, 0.0)) * amplitude_scale
    return OceanSpectrum(
        amplitudes=energy_amp,
        omega=deep_water_omega(k, gravity=gravity),
        wind_dir=wd,
        peak_omega=float(omega_p),
        peak_wavenumber=float(k_p),
        phillips_alpha=float(alpha_jonswap),
    )


def _grid_spacing(KX: np.ndarray, KY: np.ndarray) -> tuple[float, float]:
    """Wavenumber bin spacing ``(dk_x, dk_y)`` of a regular grid (1 on a singleton axis)."""
    dkx = float(abs(KX[0, 1] - KX[0, 0])) if KX.shape[1] > 1 else 1.0
    dky = float(abs(KY[1, 0] - KY[0, 0])) if KX.shape[0] > 1 else 1.0
    if dkx <= 0.0 or dky <= 0.0:
        raise OceanError("wavenumber grid must have non-zero spacing")
    return dkx, dky


def fft_wavenumber_grid(nx: int, ny: int, extent: float) -> tuple[np.ndarray, np.ndarray]:
    """Wavenumber meshgrid ``(KX, KY)`` in FFT ordering for an ``extent``-metre domain."""
    for name, dim in (("nx", nx), ("ny", ny)):
        if not isinstance(dim, int) or isinstance(dim, bool):
            raise OceanError(f"{name} must be an int")
    if nx < 2 or ny < 2 or nx > MAX_GRID or ny > MAX_GRID:
        raise OceanError(f"grid must be in [2, {MAX_GRID}] per axis")
    extent = _finite_float(extent, "extent")
    if extent <= 0.0:
        raise OceanError("extent must be positive and finite")
    kx = np.fft.fftfreq(nx, d=extent / nx) * 2.0 * np.pi
    ky = np.fft.fftfreq(ny, d=extent / ny) * 2.0 * np.pi
    KX, KY = np.meshgrid(kx, ky)
    return KX, KY


def spectral_height_field(
    spectrum: OceanSpectrum,
    *,
    seed: int = 0,
    extent: float = 100.0,
    time: float = 0.0,
) -> np.ndarray:
    """Real surface height field (metres) of a spectrum at time ``time`` (seconds).

    Tessendorf-style Hermitian construction with a seeded Gaussian draw ``xi``:

    * ``h0(k) = a_k (xi_r + i xi_i) / 2``, so ``E|h0|^2 = a_k^2 / 2``;
    * ``h(k, t) = h0(k) exp(i omega_k t) + conj(h0(-k)) exp(-i omega_k t)``, which
      satisfies ``h(-k) = conj(h(k))`` by construction;
    * the real field is ``irfft2`` of the non-negative half-plane of ``h`` with the
      Nyquist bins zeroed for even dimensions (``nx`` even: column ``nx/2``;
      ``ny`` even: row ``ny/2``).

    The variance of the result has expectation ``spectrum.variance``, provided the
    spectrum was built on :func:`fft_wavenumber_grid` with the same ``extent``.
    Identical inputs reproduce identical bytes.
    """
    _check_seed(seed)
    extent = _finite_float(extent, "extent")
    if extent <= 0.0:
        raise OceanError("extent must be positive and finite")
    time = _finite_float(time, "time")
    amp = spectrum.amplitudes
    ny, nx = amp.shape
    if nx > MAX_GRID or ny > MAX_GRID:
        raise OceanError(f"grid must be <= {MAX_GRID} per axis")

    rng = np.random.default_rng(seed)
    xi = rng.standard_normal((ny, nx, 2))
    h0 = amp * (xi[..., 0] + 1j * xi[..., 1]) / 2.0
    neg_y = (-np.arange(ny)) % ny
    neg_x = (-np.arange(nx)) % nx
    h0_neg = np.conj(h0[np.ix_(neg_y, neg_x)])
    evolve = spectrum.omega * time
    hermitian = h0 * np.exp(1j * evolve) + h0_neg * np.exp(-1j * evolve)
    half = hermitian[:, : nx // 2 + 1].copy()
    if nx % 2 == 0:
        half[:, nx // 2] = 0.0
    if ny % 2 == 0:
        half[ny // 2, :] = 0.0
    height = np.fft.irfft2(half, s=(ny, nx)) * float(nx * ny)
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
    steepness = _finite_float(steepness, "steepness")
    if not (0.0 <= steepness <= 1.0):
        raise OceanError("steepness must be finite and lie in [0, 1]")
    wavelength = _finite_float(wavelength, "wavelength")
    if wavelength <= 0.0:
        raise OceanError("wavelength must be positive and finite")
    h = _float_array(height, "height")
    if h.ndim != 2:
        raise OceanError("height must be a 2-D array")
    wd = _float_array(wind_dir, "wind_dir")
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
        h = _float_array(self.h, "h")
        hu = _float_array(self.hu, "hu")
        if h.shape != hu.shape:
            raise OceanError("h and hu must have matching shapes")
        if h.ndim not in (1, 2):
            raise OceanError("h must be 1-D or 2-D")
        if h.size == 0:
            raise OceanError("state must be non-empty")
        if h.shape[0] > MAX_GRID or (h.ndim == 2 and h.shape[1] > MAX_GRID):
            raise OceanError(f"grid must be <= {MAX_GRID} per axis")
        if np.any(h <= 0.0):
            raise OceanError("water depth h must be positive everywhere")
        dx = _finite_float(self.dx, "dx")
        if dx <= 0.0:
            raise OceanError("dx must be positive and finite")
        g = _finite_float(self.g, "g")
        if g <= 0.0:
            raise OceanError("g must be positive and finite")
        hv = None
        if self.hv is not None:
            hv = _float_array(self.hv, "hv")
            if hv.shape != h.shape:
                raise OceanError("hv must match h")
            if h.ndim != 2:
                raise OceanError("hv is only valid in 2-D")
        object.__setattr__(self, "h", h)
        object.__setattr__(self, "hu", hu)
        object.__setattr__(self, "hv", hv)
        object.__setattr__(self, "dx", dx)
        object.__setattr__(self, "g", g)


def cfl_number(state: SWEState, dt: float) -> float:
    """CFL number ``dt * (|u| + sqrt(g h)) / dx`` (max over the grid)."""
    dt = _finite_float(dt, "dt")
    if dt <= 0.0:
        raise OceanError("dt must be positive and finite")
    speed = np.abs(state.hu / state.h) + np.sqrt(state.g * state.h)
    c = float(speed.max()) * dt / state.dx
    return c


def _flux_rusanov(ql: np.ndarray, qr: np.ndarray, c: np.ndarray) -> np.ndarray:
    """Rusanov (local Lax-Friedrichs) numerical flux for the SWE system."""
    out: np.ndarray = 0.5 * (ql + qr) - 0.5 * c * (qr - ql)
    return out


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
