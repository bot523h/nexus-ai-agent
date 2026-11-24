"""Silver-halide photochemical emulsion sensitometry (AgX / DIR coupler chain).

This module is the mathematical reference for silver-halide sensitometry used by
NAGAR's cinema-science path.  It is implemented counterfactually against the
original (silent, human-operated) design so that every contract below can be
checked in CI.

Formulations
------------
Hurter–Driffield ``D``–``log10 H`` curve per emulsion layer (cyan / magenta / yellow):

.. math::

    D(\\log_{10} H_{\\mathrm{eff}})
        = D_{\\min} + \\frac{D_{\\max} - D_{\\min}}
        {1 + \\exp\\!\\left(
            -\\frac{4\\ln(10)\\,\\gamma(T)}{D_{\\max} - D_{\\min}}
            (\\log_{10} H_{\\mathrm{eff}} - \\log_{10} H_0)\\right)}

The inflection point sits at ``log10 H = log10 H_0`` where the slope is exactly
``gamma(T)`` with respect to natural-log stimulus, ``ln H``::

    \\frac{dD}{d\\ln H_{\\mathrm{eff}}}\\big|_{H_0} = \\gamma(T)

(which is equivalently ``ln(10) * gamma(T)`` with respect to ``log10 H``).

Reciprocity (Schwarzschild / Bunsen–Roscoe law)::

    H_eff = I * t^p,   I > 0, t > 0, p in [0.5, 1.0]

Arrhenius kinetics of the developer slope (Arrhenius)::

    gamma(T) = gamma_ref * exp( -E_a / R * (1/T - 1/T_ref) )

DIR-coupler reaction–diffusion (Eberhard effect) over a 1D/2D grid::

    D_final = clip(D - kappa * nabla^2 D, D_min, D_max)

Optical transmission of the dyed three-layer filter (Beer–Lambert), with
``M`` the 3x3 dye-extinction matrix and ``D`` the final sensitometric density::

    T_RGB = 10^(-M_dye @ D_final)  in (0, 1]^3

This module is pure and framework-free (NumPy + stdlib): it performs no I/O,
declares no globals beyond immutable constants, and every function is
total and deterministic.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

#: Molar gas constant in J mol^-1 K^-1 (exact, IAU 2019).
R_GAS = 8.314462618
#: Speed of light in m/s (exact, SI 2019).
C_LIGHT = 299_792_458.0
#: Planck constant in J s (exact, SI 2019).
H_PLANCK = 6.626_070_15e-34
#: Boltzmann constant in J/K (exact, SI 2019).
KB_BOLTZMANN = 1.380_649e-23
#: Avogadro-free physical constant: 1 hPa-m2 equivalent is not needed here.

#: Reciprocity exponent domain (validated by Schwarzschild / Bunsen–Roscoe).
PAIR_LOW = 0.5
PAIR_HIGH = 1.0


@dataclass(frozen=True, slots=True)
class EmulsionLayer:
    """A single silver-halide colour-sensitive emulsion layer.

    Parameters
    ----------
    name: ``"cyan" | "magenta" | "yellow"`` (canonical), any string is accepted.
    d_min:
        Fog density, ``0 < D_min`` (the density at ``H -> 0``).
    d_max:
        Shoulder density, ``D_min < D_max`` (the asymptote at ``H -> infinity``).
    log10_h0:
        Stimulus (``log10 H_eff``) where the curve passes through its inflection
        and the slope is ``gamma``; i.e. ``D(log10_h0)`` is the midpoint.
    gamma_ref:
        Armour slope at the reference temperature ``t_ref``.
    ea:
        Apparent activation energy of the developer slope in J/mol.
    t_ref:
        Reference temperature in Kelvin for ``gamma_ref``.
    """

    name: str
    d_min: float = 0.05
    d_max: float = 3.0
    log10_h0: float = 2.0
    gamma_ref: float = 2.0
    ea: float = 42_000.0
    t_ref: float = 293.15  # 20 °C

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise TypeError("name must be a non-empty string")
        object.__setattr__(self, "d_min", _require_real_number(self.d_min, "d_min"))
        object.__setattr__(self, "d_max", _require_real_number(self.d_max, "d_max"))
        object.__setattr__(self, "log10_h0", _require_real_number(self.log10_h0, "log10_h0"))
        object.__setattr__(self, "gamma_ref", _require_real_number(self.gamma_ref, "gamma_ref"))
        object.__setattr__(self, "t_ref", _require_real_number(self.t_ref, "t_ref"))
        object.__setattr__(self, "ea", _require_real_number(self.ea, "ea"))
        if not 0.0 < self.d_min < self.d_max:
            raise ValueError("require 0 < d_min < d_max")
        if self.gamma_ref <= 0.0:
            raise ValueError("gamma_ref must be positive")
        if self.t_ref <= 0.0:
            raise ValueError("t_ref must be positive (Kelvin)")

    @property
    def midpoint(self) -> float:
        return 0.5 * (self.d_min + self.d_max)


def _require_real_number(value: object, name: str) -> float:
    """Reject bool, non-number or non-finite input; return a float."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a real number")
    out = float(value)
    if not math.isfinite(out):
        raise ValueError(f"{name} must be finite")
    return out


def _require_finite_array(value: object, name: str) -> NDArray[np.float64]:
    """Reject bool/non-numeric/empty/non-finite array input; return a float64 array."""
    if type(value) is bool:
        raise TypeError(f"{name} must be a real numeric array")
    try:
        arr = np.asarray(value, dtype=float)
    except (TypeError, ValueError) as exc:
        raise TypeError(
            f"{name} must be a real numeric array (got {type(value).__name__})"
        ) from exc
    if arr.size == 0:
        raise ValueError(f"{name} must not be empty")
    if not np.all(np.isfinite(arr)):
        raise ValueError(f"{name} must contain only finite values")
    return np.asarray(arr, dtype=float)


#: Canonical silver-halide layer set used by the cream/cinema science path.
CANONICAL_LAYERS: tuple[EmulsionLayer, ...] = (
    EmulsionLayer("cyan", d_min=0.05, d_max=3.0, log10_h0=2.0, gamma_ref=2.4, ea=40_000.0),
    EmulsionLayer("magenta", d_min=0.06, d_max=2.9, log10_h0=2.2, gamma_ref=1.9, ea=42_000.0),
    EmulsionLayer("yellow", d_min=0.04, d_max=2.8, log10_h0=1.8, gamma_ref=1.6, ea=43_500.0),
)


@dataclass(frozen=True, slots=True)
class DirFilmState:
    """Result of one reaction–diffusion pass over a grid."""

    density: NDArray[np.float64]
    laplacian: NDArray[np.float64]
    final_density: NDArray[np.float64]
    clip_lo: float
    clip_hi: float


def arrhenius_gamma(
    gamma_ref: float,
    ea: float,
    t_ref: float,
    temperature: float,
    r: float = R_GAS,
) -> float:
    """Arrhenius slope of the developer gamma at ``temperature``.

    ``gamma(T) = gamma_ref * exp( -ea / R * (1/T - 1/t_ref) )``.
    Physically: higher temperature raises the slope (steeper, higher-contrast curve).
    """
    gamma_ref = _require_real_number(gamma_ref, "gamma_ref")
    r = _require_real_number(r, "R")
    ea = _require_real_number(ea, "ea")
    t_ref = _require_real_number(t_ref, "t_ref")
    temperature = _require_real_number(temperature, "temperature")
    if temperature <= 0.0 or t_ref <= 0.0:
        raise ValueError("temperatures must be positive (Kelvin)")
    if gamma_ref <= 0.0:
        raise ValueError("gamma_ref must be positive")
    if r <= 0.0:
        raise ValueError("R must be positive and finite")
    return float(gamma_ref * math.exp(-ea / r * (1.0 / temperature - 1.0 / t_ref)))


def layer_gamma(layer: EmulsionLayer, temperature: float, r: float = R_GAS) -> float:
    """Arrhenius slope of ``layer`` at ``temperature``."""
    if not isinstance(layer, EmulsionLayer):
        raise TypeError("layer must be an EmulsionLayer")
    return arrhenius_gamma(layer.gamma_ref, layer.ea, layer.t_ref, temperature, r=r)


def hurter_driffield_density(
    layer: EmulsionLayer,
    log10_h_eff: float,
    temperature: float,
    r: float = R_GAS,
) -> float:
    """Hurter–Driffield ``D-log10 H`` curve.

    At ``log10 H_eff == layer.log10_h0`` the curve passes through its midpoint
    (``(D_min + D_max) / 2``) and the tangent slope ``dD/d ln H`` is exactly
    ``gamma(T)``.
    """
    if not isinstance(layer, EmulsionLayer):
        raise TypeError("layer must be an EmulsionLayer")
    log10_h_eff = _require_real_number(log10_h_eff, "log10_h_eff")
    temperature = _require_real_number(temperature, "temperature")
    r = _require_real_number(r, "r")
    gamma = layer_gamma(layer, temperature, r=r)
    delta = layer.d_max - layer.d_min
    a = 4.0 * math.log(10.0) * gamma / delta
    u = log10_h_eff - layer.log10_h0
    # Clamp the exponential argument to avoid overflow on extreme inputs.
    arg = -a * u
    clipped = max(-700.0, min(700.0, arg))
    return float(layer.d_min + delta / (1.0 + math.exp(clipped)))


def hurt_driffield_slope_log10(
    layer: EmulsionLayer,
    log10_h_eff: float,
    temperature: float,
    r: float = R_GAS,
) -> float:
    """Analytic derivative ``dD/d log10 H_eff`` at any stimulus.

    Equals ``ln(10) * gamma(T)`` exactly at the inflection (``log10 H = log10 H_0``).
    """
    if not isinstance(layer, EmulsionLayer):
        raise TypeError("layer must be an EmulsionLayer")
    log10_h_eff = _require_real_number(log10_h_eff, "log10_h_eff")
    temperature = _require_real_number(temperature, "temperature")
    r = _require_real_number(r, "r")
    gamma = layer_gamma(layer, temperature, r=r)
    delta = layer.d_max - layer.d_min
    a = 4.0 * math.log(10.0) * gamma / delta
    u = log10_h_eff - layer.log10_h0
    arg = -a * u
    clipped = max(-700.0, min(700.0, arg))
    ez = math.exp(clipped)
    f = 1.0 / (1.0 + ez)  # logistic f
    return float(delta * a * f * (1.0 - f))


def hurt_driffield_slope_ln(
    layer: EmulsionLayer,
    log10_h_eff: float,
    temperature: float,
    r: float = R_GAS,
) -> float:
    """Analytic derivative ``dD/d ln H_eff`` at any stimulus.

    Equals exactly ``gamma(T)`` at the inflection.
    """
    return hurt_driffield_slope_log10(layer, log10_h_eff, temperature, r=r) / math.log(10.0)


def schwarzschild_reciprocity(
    intensity: float,
    time: float,
    p: float = 1.0,
) -> float:
    """Effective exposure ``H_eff = I * t^p`` (reciprocity).

    Schwarzschild (Bunsen–Roscoe) law: equal effective exposure at
    ``p = 1`` (reciprocity) holds for the model only as long as ``p`` is in
    ``[0.5, 1.0]``; outside this band the reciprocity law fails (reciprocity
    failure, sensory reciprocity / Schwarzschild effect).

    Parameters
    ----------
    intensity: irradiance or broader exposure proxy, ``> 0``.
    time: exposure time, ``> 0``.
    p: Schwarzschild reciprocity exponent, ``[0.5, 1.0]``.
    """
    intensity = _require_real_number(intensity, "intensity")
    time = _require_real_number(time, "time")
    p = _require_real_number(p, "p")
    if intensity <= 0.0:
        raise ValueError("intensity must be positive and finite")
    if time <= 0.0:
        raise ValueError("time must be positive and finite")
    if not PAIR_LOW <= p <= PAIR_HIGH:
        raise ValueError(f"p must be in [{PAIR_LOW}, {PAIR_HIGH}] (Schwarzschild band); got {p!r}")
    return float(intensity * (time**p))


def _neumann_laplacian_1d(arr: NDArray[np.float64], inv_dx_sq: float) -> NDArray[np.float64]:
    """1-D Laplacian with zero-gradient Neumann boundaries (mirror ghost cells).

    ``u_{-1} = u_{1}`` and ``u_{N} = u_{N-2}``: left edge
    ``2 (u_1 - u_0) / dx^2``, right edge ``2 (u_{N-2} - u_{N-1}) / dx^2``, interior
    standard ``(u_{i-1} - 2 u_i + u_{i+1}) / dx^2``.
    """
    n = arr.shape[0]
    laplacian = np.zeros_like(arr)
    if n < 2:
        return laplacian
    laplacian[0] = 2.0 * (arr[1] - arr[0]) * inv_dx_sq
    laplacian[-1] = 2.0 * (arr[-2] - arr[-1]) * inv_dx_sq
    if n > 2:
        laplacian[1:-1] = (arr[:-2] - 2.0 * arr[1:-1] + arr[2:]) * inv_dx_sq
    return laplacian


def _neumann_laplacian(arr: NDArray[np.float64], grid_spacing: float) -> NDArray[np.float64]:
    """2-D zero-flux Neumann Laplacian (sum of 1-D Neumann operators).

    For ``H >= 2`` and ``W >= 2``: each row uses the 1-D Neumann operator along a
    row; each column uses the 1-D Neumann operator along a column; the two are
    summed.  Corners (e.g. ``(0, 0)``) carry both Neumann contributions.  For
    degenerate grids (``1 x N``, ``N x 1``, ``1 x 1``) fall back to the 1-D
    Neumann operator along the non-degenerate axis (``0`` for all-zeros).
    """
    inv_dx_sq = 1.0 / (grid_spacing * grid_spacing)
    if arr.ndim == 1:
        return _neumann_laplacian_1d(arr, inv_dx_sq)
    if arr.ndim == 2 and arr.shape[0] >= 2 and arr.shape[1] >= 2:
        rows, cols = arr.shape
        laplacian = np.zeros_like(arr)
        for i in range(rows):
            laplacian[i, :] = _neumann_laplacian_1d(arr[i, :], inv_dx_sq)
        for j in range(cols):
            laplacian[:, j] = laplacian[:, j] + _neumann_laplacian_1d(arr[:, j], inv_dx_sq)
        return laplacian
    if arr.ndim == 2 and arr.shape[0] == 1 and arr.shape[1] >= 2:
        return _neumann_laplacian_1d(arr[0, :], inv_dx_sq).reshape(1, -1)
    if arr.ndim == 2 and arr.shape[1] == 1 and arr.shape[0] >= 2:
        return _neumann_laplacian_1d(arr[:, 0], inv_dx_sq).reshape(-1, 1)
    return np.zeros_like(arr)


def _validate_reaction_diffusion(
    density: np.ndarray,
    kappa: float,
    clip_lo: float,
    clip_hi: float,
    grid_spacing: float,
    time_step: float,
) -> tuple[NDArray[np.float64], float, float, float, float, float]:
    """Shared validation core for the DIR reaction–diffusion family (fail-closed).

    Rejects NaN/Inf/non-numeric input, non-finite or misordered clip bounds,
    negative/non-finite kappa, invalid grid_spacing/time_step and CFL
    violations (1-D ``r <= 0.5``; 2-D ``r <= 0.25``).
    """
    arr = _require_finite_array(density, "density")
    if arr.ndim < 1 or arr.ndim > 2:
        raise ValueError("density must be a 1D or 2D grid")
    kappa_f = _require_real_number(kappa, "kappa")
    if kappa_f < 0.0:
        raise ValueError("kappa must be non-negative (diffusion, not anti-diffusion)")
    clip_lo_f = _require_real_number(clip_lo, "clip_lo")
    clip_hi_f = _require_real_number(clip_hi, "clip_hi")
    if clip_lo_f >= clip_hi_f:
        raise ValueError("require clip_lo < clip_hi")
    grid_spacing_f = _require_real_number(grid_spacing, "grid_spacing")
    if grid_spacing_f <= 0.0:
        raise ValueError("grid_spacing must be positive and finite")
    time_step_f = _require_real_number(time_step, "time_step")
    if time_step_f <= 0.0:
        raise ValueError("time_step must be positive and finite")
    r = kappa_f * time_step_f / (grid_spacing_f * grid_spacing_f)
    cfl_limit = 0.5 if arr.ndim == 1 else 0.25
    if r > cfl_limit:
        raise ValueError(
            f"CFL violation: kappa * dt / dx^2 = {r:.6f} > {cfl_limit} "
            f"({arr.ndim}D explicit diffusion); refusing the unstable step"
        )
    return arr, kappa_f, clip_lo_f, clip_hi_f, grid_spacing_f, time_step_f


def dir_reaction_diffusion(
    density: np.ndarray,
    kappa: float,
    clip_lo: float,
    clip_hi: float,
    *,
    grid_spacing: float = 1.0,
    time_step: float = 1.0,
) -> NDArray[np.float64]:
    """DIR-coupler reaction–diffusion pass (Eberhard effect).

    ``D_final = clip(D + kappa * dt * nabla^2 D / dx^2, clip_lo, clip_hi)``

    Physical explicit-Euler diffusion (forward in time where gradients are
    smoothed out): interior Laplacian uses the standard discrete stencil
    ``sum(neighbors) - d * D_i`` (4-neighbourhood in 2D, 2-neighbourhood in 1D),
    ``nabla^2 D = (neighbours - d*D_i) / dx^2`` with zero-gradient Neumann
    boundaries (mirror ghost cells: left edge ``2 (u_1 - u_0) / dx^2``, right edge
    ``2 (u_{N-2} - u_{N-1}) / dx^2``; 2-D sums of the two 1-D Neumann operators).

    Stability is fail-closed: the explicit Euler step requires the CFL
    condition ``kappa * dt / dx^2 <= 0.5`` (1D) or ``<= 0.25`` (2D); a grid
    that violates it is refused.  ``clip_lo``/``clip_hi`` must be finite with
    ``clip_lo < clip_hi`` (invalid or non-finite clip bounds are rejected).
    """
    arr, k, lo, hi, dx, dt = _validate_reaction_diffusion(
        density, kappa, clip_lo, clip_hi, grid_spacing, time_step
    )

    laplacian = _neumann_laplacian(arr, dx)

    changed = arr + k * dt * laplacian
    return np.asarray(np.clip(changed, lo, hi), dtype=np.float64)


def dir_film_state(
    density: np.ndarray,
    kappa: float,
    clip_lo: float,
    clip_hi: float,
) -> DirFilmState:
    """Structured wrapper: returns the density, Laplacian and clipped result.

    Validates exactly like :func:`dir_reaction_diffusion` (shared validation
    core); the grid uses the canonical ``grid_spacing = 1.0`` and
    ``time_step = 1.0``, so the effective CFL bound is ``kappa <= 0.5`` (1D) or
    ``kappa <= 0.25`` (2D).
    """
    arr, k, lo, hi, _dx, _dt = _validate_reaction_diffusion(
        density, kappa, clip_lo, clip_hi, 1.0, 1.0
    )
    laplacian = _neumann_laplacian(arr, 1.0)
    final = np.clip(arr + k * laplacian, lo, hi)
    return DirFilmState(np.asarray(arr, dtype=np.float64), laplacian, final, lo, hi)


def beer_lambert_rgb(dye_matrix: object, d_final: object) -> NDArray[np.float64]:
    """Beer–Lambert transmission through a dye filter.

    ``T_RGB = 10^(-M_dye @ D_final)`` with result in ``(0, 1]`` and clipped for
    numerical safety at ``1e-300``.  ``M_dye`` is a 3x3 dye extinction matrix.
    """
    M = _require_finite_array(dye_matrix, "dye_matrix")
    D = _require_finite_array(d_final, "d_final")
    if M.ndim != 2 or M.shape != (3, 3):
        raise ValueError("dye_matrix must be 3x3 (RGB channels)")
    D = D.ravel()
    if D.shape != (3,):
        raise ValueError("d_final must have exactly 3 entries (RGB)")
    if (M < 0.0).any():
        raise ValueError("dye extinction coefficients must be non-negative")
    if (D < 0.0).any():
        raise ValueError("density must be non-negative")
    log_t = -(M @ D)
    # log_t <= 0 always (M, D >= 0); clamp to avoid underflow for huge optical depth.
    log_t = np.maximum(log_t, -300.0)
    return np.asarray(np.clip(10.0**log_t, 1e-300, 1.0), dtype=np.float64)


def canonical_dir_chain(
    density: np.ndarray,
    kappa: float,
    dye_matrix: np.ndarray,
    clip_lo: float,
    clip_hi: float,
) -> NDArray[np.float64]:
    """Full DIR-coupler → Beer–Lambert chain over the same grid."""
    final = dir_reaction_diffusion(density, kappa, clip_lo, clip_hi)
    if final.ndim != 1 or final.size != 3:
        raise ValueError("DIR chain expects a 1D grid of 3 channels")
    return beer_lambert_rgb(dye_matrix, final)


__all__ = [
    "EmulsionLayer",
    "CANONICAL_LAYERS",
    "DirFilmState",
    "R_GAS",
    "C_LIGHT",
    "H_PLANCK",
    "KB_BOLTZMANN",
    "PAIR_LOW",
    "PAIR_HIGH",
    "arrhenius_gamma",
    "layer_gamma",
    "hurter_driffield_density",
    "hurt_driffield_slope_log10",
    "hurt_driffield_slope_ln",
    "schwarzschild_reciprocity",
    "dir_reaction_diffusion",
    "dir_film_state",
    "beer_lambert_rgb",
    "canonical_dir_chain",
]
