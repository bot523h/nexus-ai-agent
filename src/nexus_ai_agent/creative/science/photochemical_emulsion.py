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
        if not 0.0 < self.d_min < self.d_max:
            raise ValueError("require 0 < d_min < d_max")
        if self.log10_h0 != self.log10_h0:  # NaN guard
            raise ValueError("log10_h0 must be finite")
        if not math.isfinite(self.log10_h0):
            raise ValueError("log10_h0 must be finite")
        if self.gamma_ref <= 0.0:
            raise ValueError("gamma_ref must be positive")
        if self.t_ref <= 0.0:
            raise ValueError("t_ref must be positive (Kelvin)")
        if not math.isfinite(self.ea):
            raise ValueError("ea must be finite")

    @property
    def midpoint(self) -> float:
        return 0.5 * (self.d_min + self.d_max)


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
    if temperature <= 0.0 or t_ref <= 0.0:
        raise ValueError("temperatures must be positive (Kelvin)")
    if gamma_ref <= 0.0:
        raise ValueError("gamma_ref must be positive")
    if not math.isfinite(r) or r <= 0.0:
        raise ValueError("R must be positive and finite")
    if not math.isfinite(ea):
        raise ValueError("ea must be finite")
    return float(gamma_ref * math.exp(-ea / r * (1.0 / temperature - 1.0 / t_ref)))


def layer_gamma(layer: EmulsionLayer, temperature: float, r: float = R_GAS) -> float:
    """Arrhenius slope of ``layer`` at ``temperature``."""
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
    if layer is None:
        raise TypeError("layer must be an EmulsionLayer")
    if not math.isfinite(log10_h_eff):
        raise ValueError("log10_h_eff must be finite")
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
    if not math.isfinite(log10_h_eff):
        raise ValueError("log10_h_eff must be finite")
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
    if not math.isfinite(intensity) or intensity <= 0.0:
        raise ValueError("intensity must be positive and finite")
    if not math.isfinite(time) or time <= 0.0:
        raise ValueError("time must be positive and finite")
    if not math.isfinite(p):
        raise ValueError("p must be finite")
    if not PAIR_LOW <= p <= PAIR_HIGH:
        raise ValueError(f"p must be in [{PAIR_LOW}, {PAIR_HIGH}] (Schwarzschild band); got {p!r}")
    return float(intensity * (time**p))


def dir_reaction_diffusion(
    density: np.ndarray,
    kappa: float,
    clip_lo: float,
    clip_hi: float,
) -> NDArray[np.float64]:
    """DIR-coupler reaction–diffusion pass (Eberhard effect).

    ``D_final = clip(D - kappa * nabla^2 D, clip_lo, clip_hi)``.

    Uses a 4-neighbour finite-difference Laplacian along every axis (1D or 2D),
    with zero-gradient boundaries (Neumann).  Result is clipped into
    ``[clip_lo, clip_hi]``.
    """
    if not math.isfinite(kappa):
        raise ValueError("kappa must be finite")
    if not math.isfinite(clip_lo) or not math.isfinite(clip_hi):
        raise ValueError("clip bounds must be finite")
    if clip_lo >= clip_hi:
        raise ValueError("require clip_lo < clip_hi")
    arr = np.asarray(density, dtype=float)
    if arr.ndim < 1 or arr.ndim > 2:
        raise ValueError("density must be a 1D or 2D grid")
    if arr.size == 0:
        raise ValueError("density must not be empty")

    if arr.ndim == 1:
        n = arr.shape[0]
        laplacian = np.zeros_like(arr)
        if n > 2:
            laplacian[1:-1] = arr[:-2] - 2.0 * arr[1:-1] + arr[2:]
        # Edges keep zero-Neumann (no-flux) semantics: laplacian = 0.
    else:
        rows, cols = arr.shape
        laplacian = np.zeros_like(arr)
        if rows > 2 and cols > 2:
            laplacian[1:-1, 1:-1] = (
                arr[:-2, 1:-1]
                + arr[2:, 1:-1]
                + arr[1:-1, :-2]
                + arr[1:-1, 2:]
                - 4.0 * arr[1:-1, 1:-1]
            )
        # Edges keep zero-Neumann (no-flux) semantics: laplacian = 0.

    changed = arr - kappa * laplacian
    return np.asarray(np.clip(changed, clip_lo, clip_hi), dtype=np.float64)


def dir_film_state(
    density: np.ndarray,
    kappa: float,
    clip_lo: float,
    clip_hi: float,
) -> DirFilmState:
    """Structured wrapper: returns the density, Laplacian and clipped result."""
    arr = np.asarray(density, dtype=float)
    if arr.size == 0:
        raise ValueError("density must not be empty")
    if arr.ndim == 1:
        lap = np.zeros_like(arr)
        if arr.size > 2:
            lap[1:-1] = arr[:-2] - 2.0 * arr[1:-1] + arr[2:]
        # Edges keep zero-Neumann (no-flux) semantics: laplacian = 0.
    else:
        rows, cols = arr.shape
        lap = np.zeros_like(arr)
        if rows > 2 and cols > 2:
            lap[1:-1, 1:-1] = (
                arr[:-2, 1:-1]
                + arr[2:, 1:-1]
                + arr[1:-1, :-2]
                + arr[1:-1, 2:]
                - 4.0 * arr[1:-1, 1:-1]
            )
        # Edges keep zero-Neumann (no-flux) semantics: laplacian = 0.
    final = np.clip(arr - kappa * lap, clip_lo, clip_hi)
    return DirFilmState(arr, lap, final, clip_lo, clip_hi)


def beer_lambert_rgb(
    dye_matrix: np.ndarray,
    d_final: np.ndarray,
) -> NDArray[np.float64]:
    """Beer–Lambert transmission through a dye filter.

    ``T_RGB = 10^(-M_dye @ D_final)`` with result in ``(0, 1]`` and clipped for
    numerical safety at ``1e-300``.  ``M_dye`` is a 3x3 dye extinction matrix.
    """
    M = np.asarray(dye_matrix, dtype=float)
    D = np.asarray(d_final, dtype=float).ravel()
    if M.shape != (3, 3):
        raise ValueError("dye_matrix must be 3x3 (RGB channels)")
    if D.shape != (3,):
        raise ValueError("d_final must have exactly 3 entries (RGB)")
    if not (np.isfinite(M).all() and np.isfinite(D).all()):
        raise ValueError("dye matrix and density must be finite")
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
