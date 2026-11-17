"""WAVE 23 — buoyant pyro (smoke/fire) CFD and blackbody radiation.

An offline, deterministic, bounded 2-D "pyro" solver in the stable-fluids
tradition, plus the exact blackbody radiation laws used to shade it:

* **Blackbody physics** — Planck's spectral radiance, Wien's displacement law,
  the Stefan-Boltzmann flux, and a CIE-style temperature-to-RGB mapping. These
  are closed-form; the tests check them against their analytic limits.
* **Semi-Lagrangian advection** — unconditionally stable back-tracing, so a
  large ``dt`` cannot blow the field up (it can only be inaccurate, which is
  reported as such).
* **Divergence-free projection** — a bounded Jacobi pressure solve that removes
  the velocity divergence left by advection and buoyancy.
* **Boussinesq buoyancy + vorticity confinement** — temperature drives an
  upward force; confinement re-injects the small-scale swirls that numerical
  diffusion removes, with a *declared* confinement strength.

Nothing here claims a physically calibrated fire: it is a bounded numerical
model with a measured divergence residual and reported iteration counts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

import numpy as np

__all__ = [
    "PyroError",
    "PLANCK_H",
    "PLANCK_C",
    "BOLTZMANN_K",
    "WIEN_B",
    "STEFAN_SIGMA",
    "planck_spectral_radiance",
    "wien_peak_wavelength_nm",
    "stefan_boltzmann_flux",
    "blackbody_rgb",
    "PyroField",
    "advect_semi_lagrangian",
    "project_divergence_free",
    "add_buoyancy",
    "vorticity_confinement",
    "pyro_step",
    "MAX_GRID",
    "MAX_JACOBI_ITERS",
]

#: Planck constant (J*s).
PLANCK_H: Final[float] = 6.62607015e-34
#: Speed of light (m/s).
PLANCK_C: Final[float] = 2.99792458e8
#: Boltzmann constant (J/K).
BOLTZMANN_K: Final[float] = 1.380649e-23
#: Wien displacement constant (m*K).
WIEN_B: Final[float] = 2.897771955e-3
#: Stefan-Boltzmann constant (W/m^2/K^4).
STEFAN_SIGMA: Final[float] = 5.670374419e-8

MAX_GRID: Final[int] = 256
MAX_JACOBI_ITERS: Final[int] = 4096


class PyroError(ValueError):
    """Raised for an invalid temperature, field, or out-of-range parameter."""


def planck_spectral_radiance(wavelength_m: float | np.ndarray, temperature_k: float) -> np.ndarray:
    """Planck spectral radiance ``B_lambda(T)`` in ``W m^-2 sr^-1 m^-1``.

    ``B_lambda = (2 h c^2 / lambda^5) / (exp(h c / (lambda k T)) - 1)``.
    """
    if not np.isfinite(temperature_k) or temperature_k <= 0.0:
        raise PyroError("temperature must be finite and positive")
    lam = np.asarray(wavelength_m, dtype=np.float64)
    if np.any(lam <= 0.0):
        raise PyroError("wavelength must be positive")
    x = PLANCK_H * PLANCK_C / (lam * BOLTZMANN_K * temperature_k)
    # Guard the exponent against overflow for very small wavelengths.
    with np.errstate(over="ignore"):
        denom = np.expm1(np.minimum(x, 700.0))
    out: np.ndarray = (2.0 * PLANCK_H * PLANCK_C**2 / lam**5) / denom
    return out


def wien_peak_wavelength_nm(temperature_k: float) -> float:
    """Wien displacement law: ``lambda_max = b / T`` in nanometres."""
    if not np.isfinite(temperature_k) or temperature_k <= 0.0:
        raise PyroError("temperature must be finite and positive")
    return WIEN_B / temperature_k * 1e9


def stefan_boltzmann_flux(temperature_k: float) -> float:
    """Total emitted flux ``sigma T^4`` in ``W/m^2``."""
    if not np.isfinite(temperature_k) or temperature_k < 0.0:
        raise PyroError("temperature must be finite and non-negative")
    return STEFAN_SIGMA * temperature_k**4


def _planck_at_nm(lam_nm: float, t: float) -> float:
    return float(planck_spectral_radiance(lam_nm * 1e-9, t))


#: CIE-1931-ish response bands approximated by the sRGB primaries' peaks.
_R_BAND: Final[float] = 600.0
_G_BAND: Final[float] = 550.0
_B_BAND: Final[float] = 450.0


def blackbody_rgb(temperature_k: float, *, gamma: float = 2.2) -> np.ndarray:
    """Approximate sRGB colour of a blackbody at ``temperature_k``.

    Each channel samples Planck's law at its primary band and the result is
    normalised so the brightest channel is 1.0. This is an *approximation* of
    the CIE pipeline, not a colourimetric certificate.
    """
    if not np.isfinite(temperature_k) or temperature_k <= 0.0:
        raise PyroError("temperature must be finite and positive")
    if gamma <= 0.0:
        raise PyroError("gamma must be positive")
    r = _planck_at_nm(_R_BAND, temperature_k)
    g = _planck_at_nm(_G_BAND, temperature_k)
    b = _planck_at_nm(_B_BAND, temperature_k)
    rgb = np.array([r, g, b], dtype=np.float64)
    rgb = rgb / float(rgb.max())
    out: np.ndarray = np.clip(rgb ** (1.0 / gamma), 0.0, 1.0)
    return out


@dataclass(frozen=True)
class PyroField:
    """A 2-D pyro state: velocity ``(2, ny, nx)``, temperature and density."""

    velocity: np.ndarray
    temperature: np.ndarray
    density: np.ndarray

    def __post_init__(self) -> None:
        v = np.asarray(self.velocity, dtype=np.float64)
        t = np.asarray(self.temperature, dtype=np.float64)
        d = np.asarray(self.density, dtype=np.float64)
        if v.ndim != 3 or v.shape[0] != 2:
            raise PyroError("velocity must be (2, ny, nx)")
        if t.shape != v.shape[1:] or d.shape != t.shape:
            raise PyroError("temperature and density must match the velocity grid")
        if t.size == 0:
            raise PyroError("pyro field must be non-empty")
        if v.shape[1] > MAX_GRID or v.shape[2] > MAX_GRID:
            raise PyroError(f"grid must be <= {MAX_GRID} per axis")
        for arr, name in ((v, "velocity"), (t, "temperature"), (d, "density")):
            if not np.all(np.isfinite(arr)):
                raise PyroError(f"{name} contains NaN/Inf")
        if np.any(t < 0.0):
            raise PyroError("temperature must be non-negative")
        if np.any(d < 0.0):
            raise PyroError("density must be non-negative")
        object.__setattr__(self, "velocity", v)
        object.__setattr__(self, "temperature", t)
        object.__setattr__(self, "density", d)

    @property
    def shape(self) -> tuple[int, int]:
        return (self.temperature.shape[0], self.temperature.shape[1])


def _bilinear_sample(field: np.ndarray, xs: np.ndarray, ys: np.ndarray) -> np.ndarray:
    ny, nx = field.shape
    x0 = np.floor(xs).astype(np.int64)
    y0 = np.floor(ys).astype(np.int64)
    x1 = x0 + 1
    y1 = y0 + 1
    wx = xs - x0
    wy = ys - y0
    x0c = np.clip(x0, 0, nx - 1)
    x1c = np.clip(x1, 0, nx - 1)
    y0c = np.clip(y0, 0, ny - 1)
    y1c = np.clip(y1, 0, ny - 1)
    s00 = field[y0c, x0c]
    s10 = field[y0c, x1c]
    s01 = field[y1c, x0c]
    s11 = field[y1c, x1c]
    out: np.ndarray = (
        s00 * (1 - wx) * (1 - wy) + s10 * wx * (1 - wy) + s01 * (1 - wx) * wy + s11 * wx * wy
    )
    return out


def advect_semi_lagrangian(quantity: np.ndarray, velocity: np.ndarray, dt: float) -> np.ndarray:
    """Advect ``quantity`` by ``velocity`` with back-tracing (unconditionally stable)."""
    q = np.asarray(quantity, dtype=np.float64)
    v = np.asarray(velocity, dtype=np.float64)
    if q.ndim != 2:
        raise PyroError("quantity must be 2-D")
    if v.ndim != 3 or v.shape[0] != 2 or v.shape[1:] != q.shape:
        raise PyroError("velocity must be (2, ny, nx) matching quantity")
    if dt < 0.0 or not np.isfinite(dt):
        raise PyroError("dt must be finite and non-negative")
    ny, nx = q.shape
    jj, ii = np.meshgrid(np.arange(ny), np.arange(nx), indexing="ij")
    x = ii - dt * v[0]
    y = jj - dt * v[1]
    return _bilinear_sample(q, x, y)


def _divergence(v: np.ndarray) -> np.ndarray:
    """Periodic forward-difference divergence, consistent with the backward
    gradient used in the projection (so the pressure Laplacian is a true
    5-point stencil and the solver does not decouple into checkerboards)."""
    u = v[0]
    w = v[1]
    du = np.empty_like(u)
    du[:, :-1] = u[:, 1:] - u[:, :-1]
    du[:, -1] = u[:, 0] - u[:, -1]
    dw = np.empty_like(w)
    dw[:-1, :] = w[1:, :] - w[:-1, :]
    dw[-1, :] = w[0, :] - w[-1, :]
    out: np.ndarray = du + dw
    return out


def project_divergence_free(
    velocity: np.ndarray, *, iters: int = 40, tolerance: float = 1e-5
) -> tuple[np.ndarray, float, int]:
    """Remove the velocity divergence with a periodic red-black Gauss-Seidel
    pressure solve on the 5-point Laplacian.

    Returns ``(velocity, residual, iters_used)`` where ``residual`` is the
    measured ``max|div v|`` (forward difference) after projection. The solver is
    bounded by ``iters``; the residual is reported so a truncated solve is never
    mistaken for a divergence-free field.
    """
    v = np.asarray(velocity, dtype=np.float64)
    if v.ndim != 3 or v.shape[0] != 2:
        raise PyroError("velocity must be (2, ny, nx)")
    if iters < 1 or iters > MAX_JACOBI_ITERS:
        raise PyroError(f"iters must be in [1, {MAX_JACOBI_ITERS}]")
    if tolerance <= 0.0:
        raise PyroError("tolerance must be positive")

    ny, nx = v.shape[1], v.shape[2]
    div = _divergence(v)
    p = np.zeros((ny, nx), dtype=np.float64)
    ii, jj = np.meshgrid(np.arange(ny), np.arange(nx), indexing="ij")
    red = (ii + jj) % 2 == 0
    used = 0
    for k in range(iters):
        used = k + 1
        p_prev = p
        p_new = p.copy()
        neigh = np.roll(p, 1, 0) + np.roll(p, -1, 0) + np.roll(p, 1, 1) + np.roll(p, -1, 1)
        p_new[red] = 0.25 * (neigh[red] - div[red])
        p = p_new
        neigh = np.roll(p, 1, 0) + np.roll(p, -1, 0) + np.roll(p, 1, 1) + np.roll(p, -1, 1)
        p_new = p.copy()
        p_new[~red] = 0.25 * (neigh[~red] - div[~red])
        p = p_new
        if float(np.abs(p - p_prev).max()) < tolerance:
            break
    # Periodic backward-difference gradient.
    gx = np.empty_like(p)
    gx[:, 1:] = p[:, 1:] - p[:, :-1]
    gx[:, 0] = p[:, 0] - p[:, -1]
    gy = np.empty_like(p)
    gy[1:, :] = p[1:, :] - p[:-1, :]
    gy[0, :] = p[0, :] - p[-1, :]
    out = v.copy()
    out[0] -= gx
    out[1] -= gy
    residual = float(np.abs(_divergence(out)).max())
    return out, residual, used


def add_buoyancy(
    velocity: np.ndarray, temperature: np.ndarray, *, dt: float, coefficient: float = 1.0
) -> np.ndarray:
    """Boussinesq buoyancy: upward force proportional to temperature.

    Screen coordinates use ``+y`` down, so buoyancy is negative in the ``v``
    component (upward).
    """
    v = np.asarray(velocity, dtype=np.float64)
    t = np.asarray(temperature, dtype=np.float64)
    if v.ndim != 3 or v.shape[1:] != t.shape:
        raise PyroError("velocity must be (2, ny, nx) matching temperature")
    if coefficient < 0.0:
        raise PyroError("coefficient must be non-negative")
    if dt < 0.0 or not np.isfinite(dt):
        raise PyroError("dt must be finite and non-negative")
    out = v.copy()
    out[1] -= coefficient * t * dt
    return out


def vorticity_confinement(velocity: np.ndarray, *, dt: float, epsilon: float = 0.5) -> np.ndarray:
    """Re-inject small-scale swirls via vorticity confinement."""
    v = np.asarray(velocity, dtype=np.float64)
    if v.ndim != 3 or v.shape[0] != 2:
        raise PyroError("velocity must be (2, ny, nx)")
    if epsilon < 0.0:
        raise PyroError("epsilon must be non-negative")
    if dt < 0.0 or not np.isfinite(dt):
        raise PyroError("dt must be finite and non-negative")
    du_dy = np.gradient(v[0], axis=0)
    dv_dx = np.gradient(v[1], axis=1)
    omega = dv_dx - du_dy
    abs_w = np.abs(omega)
    gx = np.gradient(abs_w, axis=1)
    gy = np.gradient(abs_w, axis=0)
    norm = np.sqrt(gx * gx + gy * gy) + 1e-12
    nx = gx / norm
    ny = gy / norm
    out = v.copy()
    out[0] += epsilon * dt * (ny * omega)
    out[1] += epsilon * dt * (-nx * omega)
    return out


def pyro_step(
    field: PyroField,
    *,
    dt: float = 0.1,
    buoyancy: float = 1.0,
    confinement: float = 0.5,
    projection_iters: int = 40,
    dissipation: float = 0.0,
) -> tuple[PyroField, float, int]:
    """One pyro step: advect, add buoyancy, confine vorticity, project.

    Returns ``(field, divergence_residual, projection_iters_used)``.
    """
    if dt <= 0.0 or not np.isfinite(dt):
        raise PyroError("dt must be finite and positive")
    if not (0.0 <= dissipation <= 1.0):
        raise PyroError("dissipation must lie in [0, 1]")
    t = field.temperature
    d = field.density
    # Advect each velocity component using the full velocity field.
    vx = advect_semi_lagrangian(field.velocity[0], field.velocity, dt)
    vy = advect_semi_lagrangian(field.velocity[1], field.velocity, dt)
    v = np.stack([vx, vy], axis=0)
    t = advect_semi_lagrangian(t, field.velocity, dt)
    d = advect_semi_lagrangian(d, field.velocity, dt)
    v = add_buoyancy(v, t, dt=dt, coefficient=buoyancy)
    v = vorticity_confinement(v, dt=dt, epsilon=confinement)
    v, residual, used = project_divergence_free(v, iters=projection_iters)
    if dissipation > 0.0:
        t = t * (1.0 - dissipation)
        d = d * (1.0 - dissipation)
    return PyroField(velocity=v, temperature=t, density=d), residual, used
