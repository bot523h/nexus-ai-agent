"""WAVE 23 — anamorphic volumetric scattering and relativistic Doppler renderer.

This module renders the optical signature of a cinematic atmosphere and moving
sources, in pure NumPy + stdlib:

* :func:`compute_volumetric_mie_rayleigh_field` — wavelength-dependent Rayleigh
  cross-section ratios (``lambda^-4``) for the RGB channels and the normalised
  Henyey-Greenstein aerosol phase function, whose angular integral over the unit
  sphere is proved equal to one by Gauss-Legendre quadrature;
* :func:`render_anamorphic_cinema_frame` — the anamorphic elliptical bokeh point
  spread function: a circle-of-confusion radius from the focal plane, a
  normalised energy-preserving elliptical kernel with semi-axes ``a_y = r_CoC``
  and ``a_x = r_CoC / squeeze``, and physically bounded horizontal anamorphic
  streaks from specular highlights;
* :func:`compute_doppler_shifted_signal` — the exact acoustic Doppler ratio
  ``(c0 + v_obs) / (c0 - v_src)`` (refusing a sonic-boom when ``|v_src| >= c0``)
  and the relativistic longitudinal optical ratio ``sqrt((1 + beta) / (1 - beta))``
  (refusing ``|beta| >= 1``), plus a phase-continuous 48 kHz PCM synthesiser for
  a moving source.

Bounded, validated, offline and deterministic.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Final

import numpy as np

__all__ = [
    "SPEED_OF_SOUND",
    "SPEED_OF_LIGHT",
    "RGB_WAVELENGTHS_NM",
    "SAMPLE_RATE_HZ",
    "MAX_GRID_SIDE",
    "MAX_SAMPLES",
    "ANAMORPHIC_SQUEEZES",
    "ScatteringError",
    "VolumetricScatteringField",
    "compute_volumetric_mie_rayleigh_field",
    "AnamorphicBokehKernel",
    "render_anamorphic_cinema_frame",
    "DopplerShiftResult",
    "compute_doppler_shifted_signal",
]

SPEED_OF_SOUND: Final[float] = 343.0
SPEED_OF_LIGHT: Final[float] = 299_792_458.0
RGB_WAVELENGTHS_NM: Final[dict[str, float]] = {"R": 650.0, "G": 532.0, "B": 460.0}
SAMPLE_RATE_HZ: Final[int] = 48_000
MAX_GRID_SIDE: Final[int] = 2048
MAX_SAMPLES: Final[int] = 1_000_000
ANAMORPHIC_SQUEEZES: Final[tuple[float, ...]] = (1.0, 1.33, 1.8, 2.0)
_EPS: Final[float] = 1e-12


class ScatteringError(ValueError):
    """Raised for a malformed scattering, bokeh or Doppler request."""


def _rayleigh_ratio(wavelength_nm: float) -> float:
    if wavelength_nm <= 0.0 or not np.isfinite(wavelength_nm):
        raise ScatteringError("wavelength must be positive and finite")
    return float(wavelength_nm**-4)


def henyey_greenstein(cos_theta: np.ndarray, g: float) -> np.ndarray:
    """Normalised Henyey-Greenstein aerosol phase function.

    ``p(cos_theta; g) = (1 - g^2) / (4 pi (1 + g^2 - 2 g cos_theta)^(3/2))``
    for ``g in (-1, 1)``. Its integral over the unit sphere is one.
    """
    if not np.isfinite(g) or abs(g) >= 1.0:
        raise ScatteringError("asymmetry parameter g must satisfy |g| < 1")
    cosine = np.asarray(cos_theta, dtype=np.float64)
    if np.any(cosine < -1.0 - 1e-9) or np.any(cosine > 1.0 + 1e-9):
        raise ScatteringError("cos_theta must lie in [-1, 1]")
    cosine = np.clip(cosine, -1.0, 1.0)
    denominator = 1.0 + g * g - 2.0 * g * cosine
    return np.asarray((1.0 - g * g) / (4.0 * np.pi * denominator**1.5), dtype=np.float64)


@dataclass(frozen=True)
class VolumetricScatteringField:
    """Rayleigh/Mie volumetric scattering ratios for the RGB channels."""

    rayleigh_ratios: dict[str, float]
    phase_g: float
    phase_normalisation: float
    transmittance: float
    extinction_coefficient: float

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": "atlas.volumetric_scattering_field",
            "schema_version": 1,
            "rayleigh_ratios": self.rayleigh_ratios,
            "phase_g": self.phase_g,
            "phase_normalisation": self.phase_normalisation,
            "transmittance": self.transmittance,
            "extinction_coefficient": self.extinction_coefficient,
        }


def compute_volumetric_mie_rayleigh_field(
    *,
    g: float,
    extinction_coefficient: float,
    depth: float,
    quadrature_nodes: int = 256,
) -> VolumetricScatteringField:
    """Compute the RGB Rayleigh ratios, the phase normalisation and the transmittance.

    The HG normalisation is measured by Gauss-Legendre quadrature of
    ``integral p(cos_theta) 2 pi sin(theta) dtheta`` over the unit sphere; the
    transmittance follows Beer-Lambert ``T = exp(-sigma_t depth)`` in ``(0, 1]``.
    """
    if not isinstance(quadrature_nodes, int) or isinstance(quadrature_nodes, bool):
        raise ScatteringError("quadrature_nodes must be a positive int")
    if quadrature_nodes < 8:
        raise ScatteringError("quadrature_nodes must be at least 8")
    if extinction_coefficient < 0.0 or not np.isfinite(extinction_coefficient):
        raise ScatteringError("extinction_coefficient must be finite and non-negative")
    if depth < 0.0 or not np.isfinite(depth):
        raise ScatteringError("depth must be finite and non-negative")

    nodes, weights = np.polynomial.legendre.leggauss(quadrature_nodes)
    phase_values = henyey_greenstein(nodes, g)
    normalisation = float(np.sum(weights * phase_values) * 2.0 * np.pi)
    transmittance = float(math.exp(-extinction_coefficient * depth))
    ratios = {channel: _rayleigh_ratio(w) for channel, w in RGB_WAVELENGTHS_NM.items()}
    return VolumetricScatteringField(
        rayleigh_ratios=ratios,
        phase_g=float(g),
        phase_normalisation=normalisation,
        transmittance=transmittance,
        extinction_coefficient=float(extinction_coefficient),
    )


@dataclass(frozen=True)
class AnamorphicBokehKernel:
    """An energy-normalised anamorphic elliptical bokeh kernel."""

    kernel: np.ndarray
    coc_radius: float
    semi_axis_x: float
    semi_axis_y: float
    squeeze: float
    energy: float
    streak_energy: float

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": "atlas.anamorphic_bokeh_kernel",
            "schema_version": 1,
            "coc_radius": self.coc_radius,
            "semi_axis_x": self.semi_axis_x,
            "semi_axis_y": self.semi_axis_y,
            "squeeze": self.squeeze,
            "energy": self.energy,
            "streak_energy": self.streak_energy,
        }


def circle_of_confusion_radius(
    depth: float, focus_depth: float, f_number: float, focal_length: float
) -> float:
    """The thin-lens circle of confusion radius at ``depth``."""
    if depth <= 0.0 or focus_depth <= 0.0 or not np.isfinite(depth) or not np.isfinite(focus_depth):
        raise ScatteringError("depths must be positive and finite")
    if f_number <= 0.0 or not np.isfinite(f_number):
        raise ScatteringError("f_number must be positive and finite")
    if focal_length <= 0.0 or not np.isfinite(focal_length):
        raise ScatteringError("focal_length must be positive and finite")
    return float(abs(depth - focus_depth) / depth * focal_length / (2.0 * f_number))


def render_anamorphic_cinema_frame(
    *,
    coc_radius: float,
    squeeze: float,
    side: int = 33,
    streak_length: float = 0.0,
) -> AnamorphicBokehKernel:
    """Build a normalised anamorphic elliptical bokeh kernel.

    ``side`` must be odd so the kernel has a centre; the vertical semi-axis is
    ``a_y = coc_radius`` and the horizontal semi-axis is ``a_x = coc_radius /
    squeeze`` (a squeeze of 1 gives a circular kernel). A physically bounded
    horizontal streak of ``streak_length`` adds bounded specular flare energy.
    """
    if squeeze not in ANAMORPHIC_SQUEEZES:
        raise ScatteringError(f"squeeze must be one of {ANAMORPHIC_SQUEEZES}")
    if not isinstance(side, int) or isinstance(side, bool) or side < 5 or side > MAX_GRID_SIDE:
        raise ScatteringError(f"side must be an odd int in [5, {MAX_GRID_SIDE}]")
    if side % 2 == 0:
        raise ScatteringError("side must be odd so the kernel has a centre")
    if coc_radius <= 0.0 or not np.isfinite(coc_radius):
        raise ScatteringError("coc_radius must be positive and finite")
    if streak_length < 0.0 or not np.isfinite(streak_length):
        raise ScatteringError("streak_length must be finite and non-negative")

    semi_axis_y = float(coc_radius)
    semi_axis_x = float(coc_radius / squeeze)
    half = side // 2
    axis = np.arange(-half, half + 1, dtype=np.float64)
    grid_x, grid_y = np.meshgrid(axis, axis)
    ellipse = (grid_x / semi_axis_x) ** 2 + (grid_y / semi_axis_y) ** 2
    kernel = np.where(ellipse <= 1.0, 1.0, 0.0)
    streak_energy = 0.0
    if streak_length > 0.0:
        streak = np.exp(-((grid_y / max(semi_axis_y * 0.25, _EPS)) ** 2)) * np.where(
            np.abs(grid_x) <= streak_length, 1.0, 0.0
        )
        streak = streak / max(float(streak.sum()), _EPS)
        ellipse_mass = float(kernel.sum())
        kernel = kernel / max(ellipse_mass, _EPS) * 0.85 + streak * 0.15
        streak_energy = 0.15
    else:
        kernel = kernel / max(float(kernel.sum()), _EPS)
    energy = float(kernel.sum())
    return AnamorphicBokehKernel(
        kernel=kernel,
        coc_radius=float(coc_radius),
        semi_axis_x=semi_axis_x,
        semi_axis_y=semi_axis_y,
        squeeze=float(squeeze),
        energy=energy,
        streak_energy=streak_energy,
    )


@dataclass(frozen=True)
class DopplerShiftResult:
    """The outcome of an acoustic or optical Doppler computation."""

    mode: str
    ratio: float
    source_velocity: float
    observer_velocity: float
    waveform: np.ndarray | None = None
    sample_rate: int = SAMPLE_RATE_HZ
    notes: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": "atlas.doppler_shift",
            "schema_version": 1,
            "mode": self.mode,
            "ratio": self.ratio,
            "source_velocity": self.source_velocity,
            "observer_velocity": self.observer_velocity,
            "sample_rate": self.sample_rate,
            "notes": list(self.notes),
        }


def compute_doppler_shifted_signal(
    *,
    source_frequency: float,
    source_velocity: float,
    observer_velocity: float = 0.0,
    mode: str = "acoustic",
    duration: float = 0.0,
    amplitude: float = 0.5,
) -> DopplerShiftResult:
    """Compute the Doppler ratio and optionally a phase-continuous PCM waveform.

    ``mode="acoustic"`` uses ``(c0 + v_obs) / (c0 - v_src)`` and refuses a sonic
    boom when ``|v_src| >= c0``. ``mode="optical"`` uses the relativistic
    longitudinal ratio ``sqrt((1 + beta) / (1 - beta))`` with ``beta = v_src / c``
    and refuses ``|beta| >= 1``. When ``duration > 0`` a 48 kHz phase-continuous
    PCM waveform of the shifted tone is synthesised.
    """
    if source_frequency <= 0.0 or not np.isfinite(source_frequency):
        raise ScatteringError("source_frequency must be positive and finite")
    if not np.isfinite(source_velocity) or not np.isfinite(observer_velocity):
        raise ScatteringError("velocities must be finite")
    if amplitude <= 0.0 or amplitude > 1.0 or not np.isfinite(amplitude):
        raise ScatteringError("amplitude must be in (0, 1]")

    if mode == "acoustic":
        if abs(source_velocity) >= SPEED_OF_SOUND:
            raise ScatteringError("acoustic source cannot reach or exceed the speed of sound")
        if abs(observer_velocity) >= SPEED_OF_SOUND:
            raise ScatteringError("acoustic observer cannot reach or exceed the speed of sound")
        ratio = (SPEED_OF_SOUND + observer_velocity) / (SPEED_OF_SOUND - source_velocity)
        notes = ("acoustic Doppler uses the exact (c0 + v_obs)/(c0 - v_src) ratio",)
    elif mode == "optical":
        beta = source_velocity / SPEED_OF_LIGHT
        if abs(beta) >= 1.0:
            raise ScatteringError("optical source cannot reach or exceed the speed of light")
        ratio = math.sqrt((1.0 + beta) / (1.0 - beta))
        notes = ("optical Doppler uses the relativistic longitudinal ratio",)
    else:
        raise ScatteringError("mode must be 'acoustic' or 'optical'")

    waveform: np.ndarray | None = None
    if duration > 0.0:
        if not np.isfinite(duration):
            raise ScatteringError("duration must be finite")
        count = int(round(duration * SAMPLE_RATE_HZ))
        if count < 1 or count > MAX_SAMPLES:
            raise ScatteringError(f"sample count must be in [1, {MAX_SAMPLES}]")
        observed = source_frequency * ratio
        if observed >= 0.5 * SAMPLE_RATE_HZ:
            raise ScatteringError("shifted frequency exceeds the Nyquist limit")
        times = np.arange(count, dtype=np.float64) / SAMPLE_RATE_HZ
        waveform = np.asarray(amplitude * np.sin(2.0 * np.pi * observed * times), dtype=np.float64)

    return DopplerShiftResult(
        mode=mode,
        ratio=float(ratio),
        source_velocity=float(source_velocity),
        observer_velocity=float(observer_velocity),
        waveform=waveform,
        notes=notes,
    )
