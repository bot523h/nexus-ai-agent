"""Law 9 — Clausius-Duhem thermodynamic arrow of time and retarded causality.

AI video accidentally runs local physics backward: smoke sucks into chimneys,
splashes un-splash and ripples converge before impact. This module audits
Gibbs-Shannon spatial entropy and Boussinesq thermal centre-of-mass
monotonicity, restores forward evolution with unconditional Fourier
advection-diffusion-buoyancy integration, and verifies retarded Green's
function wavefront causality.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

FloatArray = npt.NDArray[np.float64]


class ThermodynamicError(ValueError):
    """Raised for invalid scalar fields or transport parameters."""


@dataclass(frozen=True)
class ArrowAudit:
    """Result of auditing and healing the arrow of time."""

    entropy_before: FloatArray
    entropy_after: FloatArray
    centre_of_mass_before: FloatArray
    centre_of_mass_after: FloatArray
    violations_before: tuple[str, ...]
    violations_after: tuple[str, ...]
    healed_frames: FloatArray

    @property
    def healed(self) -> bool:
        return not self.violations_after


def _validate_frames(frames: FloatArray) -> FloatArray:
    data = np.asarray(frames, dtype=np.float64)
    if data.ndim != 3:
        raise ThermodynamicError("frames must have shape (T, H, W)")
    if data.shape[0] < 1 or data.shape[1] < 1 or data.shape[2] < 1:
        raise ThermodynamicError("frames must be non-empty")
    if not np.all(np.isfinite(data)):
        raise ThermodynamicError("frames must be finite")
    if np.any(data < 0.0):
        raise ThermodynamicError("scalar density fields must be non-negative")
    return data


def spatial_entropy(field: FloatArray) -> float | FloatArray:
    """Gibbs-Shannon spatial entropy of a normalised scalar density field."""
    data = np.asarray(field, dtype=np.float64)
    if data.ndim < 2:
        raise ThermodynamicError("field must have at least two dimensions")
    if not np.all(np.isfinite(data)) or np.any(data < 0.0):
        raise ThermodynamicError("field must be finite and non-negative")
    total = np.sum(data, axis=(-2, -1), keepdims=True)
    safe_total = np.where(total > 0.0, total, 1.0)
    probability = data / safe_total
    with np.errstate(divide="ignore", invalid="ignore"):
        terms = np.where(probability > 0.0, probability * np.log(probability), 0.0)
    entropy = -np.sum(terms, axis=(-2, -1))
    return float(entropy) if np.ndim(entropy) == 0 else entropy


def _vertical_centre_of_mass(field: FloatArray) -> float | FloatArray:
    rows = field.shape[-2]
    weights = np.arange(rows, dtype=np.float64).reshape((1,) * (field.ndim - 2) + (rows, 1))
    total = np.sum(field, axis=(-2, -1), keepdims=True)
    safe_total = np.where(total > 0.0, total, 1.0)
    centre = np.sum(field * weights, axis=(-2, -1), keepdims=True) / safe_total
    centre = np.squeeze(centre, axis=(-2, -1))
    return float(centre) if np.ndim(centre) == 0 else centre


def fourier_advection_diffusion(
    field: FloatArray,
    velocity: tuple[float, float] | FloatArray,
    diffusivity: float,
    dt: float,
    dx: float,
) -> FloatArray:
    """Unconditional Fourier-domain advection-diffusion step."""
    data = np.asarray(field, dtype=np.float64)
    if data.ndim != 2:
        raise ThermodynamicError("field must be two-dimensional")
    if not np.all(np.isfinite(data)):
        raise ThermodynamicError("field must be finite")
    speed = np.asarray(velocity, dtype=np.float64)
    if speed.shape != (2,) or not np.all(np.isfinite(speed)):
        raise ThermodynamicError("velocity must be a finite 2-vector")
    if diffusivity < 0.0 or not np.isfinite(diffusivity):
        raise ThermodynamicError("diffusivity must be non-negative and finite")
    if dt <= 0.0 or dx <= 0.0:
        raise ThermodynamicError("dt and dx must be positive")
    rows, cols = data.shape
    freq_r = np.fft.fftfreq(rows, d=dx)[:, None]
    freq_c = np.fft.fftfreq(cols, d=dx)[None, :]
    phase = -2.0j * np.pi * (speed[0] * freq_r + speed[1] * freq_c) * dt
    decay = np.exp(-diffusivity * 4.0 * np.pi**2 * (freq_r**2 + freq_c**2) * dt)
    spectrum = np.fft.fft2(data)
    return np.real(np.fft.ifft2(spectrum * np.exp(phase) * decay))


def retarded_wavefront(
    shape: tuple[int, int],
    source: tuple[float, float],
    impact_time_s: float,
    c_max: float,
    *,
    dx: float = 1.0,
    dt: float = 1.0,
    frames: int = 5,
    amplitude: float = 1.0,
    wavenumber: float = 1.0,
) -> FloatArray:
    """Build a causal retarded wavefront from an impact event."""
    if len(shape) != 2 or shape[0] <= 0 or shape[1] <= 0:
        raise ThermodynamicError("shape must be two positive dimensions")
    if c_max <= 0.0 or dx <= 0.0 or dt <= 0.0:
        raise ThermodynamicError("wave speed, dx and dt must be positive")
    if frames < 1 or impact_time_s < 0.0:
        raise ThermodynamicError("frames must be positive and impact time non-negative")
    rows, cols = shape
    grid_r, grid_c = np.meshgrid(
        np.arange(rows, dtype=np.float64) * dx,
        np.arange(cols, dtype=np.float64) * dx,
        indexing="ij",
    )
    radius = np.hypot(grid_r - source[0], grid_c - source[1])
    output = np.zeros((frames, rows, cols), dtype=np.float64)
    for index in range(frames):
        elapsed = index * dt - impact_time_s
        if elapsed < 0.0:
            continue
        front = c_max * elapsed
        support = radius <= front
        output[index] = np.where(support, amplitude * np.cos(wavenumber * (radius - front)), 0.0)
    return output


def verify_retarded_causality(
    wave: FloatArray,
    source: tuple[float, float],
    impact_time_s: float,
    c_max: float,
    *,
    dx: float = 1.0,
    dt: float = 1.0,
    tolerance: float = 1e-12,
) -> bool:
    """Verify strict causal support of a retarded wavefield."""
    data = np.asarray(wave, dtype=np.float64)
    if data.ndim != 3:
        raise ThermodynamicError("wave must have shape (T, H, W)")
    if c_max <= 0.0 or dx <= 0.0 or dt <= 0.0:
        raise ThermodynamicError("wave speed, dx and dt must be positive")
    rows, cols = data.shape[1], data.shape[2]
    grid_r, grid_c = np.meshgrid(
        np.arange(rows, dtype=np.float64) * dx,
        np.arange(cols, dtype=np.float64) * dx,
        indexing="ij",
    )
    radius = np.hypot(grid_r - source[0], grid_c - source[1])
    for index in range(data.shape[0]):
        elapsed = index * dt - impact_time_s
        if elapsed < 0.0:
            if float(np.max(np.abs(data[index]))) > tolerance:
                return False
            continue
        outside = radius > c_max * elapsed + 1e-12
        if np.any(np.abs(data[index][outside]) > tolerance):
            return False
    return True


def _violations(
    entropy: FloatArray, centre: FloatArray, *, tolerance: float, buoyancy: float
) -> tuple[str, ...]:
    violations: list[str] = []
    if entropy.size > 1 and np.any(np.diff(entropy) < -tolerance):
        violations.append("entropy_decrease")
    if buoyancy > 0.0 and centre.size > 1 and np.any(np.diff(centre) > tolerance):
        violations.append("buoyancy_reversal")
    return tuple(violations)


def audit_and_enforce_thermodynamic_arrow(
    frames: FloatArray,
    *,
    dx: float = 1.0,
    dt: float = 1.0,
    diffusivity: float = 0.05,
    velocity: tuple[float, float] = (0.0, 0.0),
    buoyancy: float = 0.0,
    tolerance: float = 1e-9,
) -> ArrowAudit:
    """Audit and restore the thermodynamic arrow of time for a scalar sequence."""
    data = _validate_frames(frames)
    if dx <= 0.0 or dt <= 0.0:
        raise ThermodynamicError("dx and dt must be positive")
    if diffusivity < 0.0 or not np.isfinite(diffusivity):
        raise ThermodynamicError("diffusivity must be non-negative and finite")
    if buoyancy < 0.0 or not np.isfinite(buoyancy):
        raise ThermodynamicError("buoyancy must be non-negative and finite")
    if tolerance < 0.0:
        raise ThermodynamicError("tolerance must be non-negative")

    entropy_before = np.asarray([spatial_entropy(frame) for frame in data], dtype=np.float64)
    centre_before = np.asarray(
        [_vertical_centre_of_mass(frame) for frame in data], dtype=np.float64
    )
    violations_before = _violations(
        entropy_before, centre_before, tolerance=tolerance, buoyancy=buoyancy
    )

    healed = np.empty_like(data)
    healed[0] = data[0]
    shift = int(round(buoyancy * dt / dx))
    for index in range(1, data.shape[0]):
        advanced = fourier_advection_diffusion(healed[index - 1], velocity, diffusivity, dt, dx)
        advanced = np.clip(advanced, 0.0, None)
        if shift > 0:
            advanced = np.roll(advanced, -shift, axis=0)
        healed[index] = advanced

    entropy_after = np.asarray([spatial_entropy(frame) for frame in healed], dtype=np.float64)
    centre_after = np.asarray(
        [_vertical_centre_of_mass(frame) for frame in healed], dtype=np.float64
    )
    violations_after = _violations(
        entropy_after, centre_after, tolerance=tolerance, buoyancy=buoyancy
    )
    return ArrowAudit(
        entropy_before=entropy_before,
        entropy_after=entropy_after,
        centre_of_mass_before=centre_before,
        centre_of_mass_after=centre_after,
        violations_before=violations_before,
        violations_after=violations_after,
        healed_frames=healed,
    )
