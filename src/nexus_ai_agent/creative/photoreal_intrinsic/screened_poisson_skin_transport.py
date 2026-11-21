"""Three-scale Neumann-DCT transport with normalized image-space diffusion lengths."""

from __future__ import annotations

import math
from typing import cast

import numpy as np

from .log_chromophore_tomography_baker import dctn, idctn, validate_physiology


class ScreenedPoissonSkinTransport:
    """Solve weighted ``(I - sigma² Delta_N)u=S`` at three image-space scales.

    ``sigma`` is a normalized-image diffusion length (pixels in the sampled image
    coordinate system), not a dimensional tissue transport coefficient. The three
    weights and scale factors are phenomenological RGB skin-rendering parameters.
    """

    weights = np.array((0.5218, 0.3341, 0.1441), np.float64)
    # Scale factors are dimensionless multipliers of each normalized diffusion length.
    scales = np.array((0.618, 1.618, 3.618), np.float64)
    default_radii = (3.4, 1.35, 0.65)

    @staticmethod
    def solve_single_scale(source: np.ndarray, diffusion_length: float) -> np.ndarray:
        """Solve one Neumann-DCT scale; ``diffusion_length`` uses image coordinates."""
        _require_finite_array(source, name="source")
        _require_finite_scalar(diffusion_length, name="diffusion_length")
        field = np.asarray(source, dtype=np.float64)
        if field.ndim != 2 or min(field.shape, default=0) < 2:
            raise ValueError("source must be a 2-D field with dimensions at least 2x2")
        if not np.isfinite(field).all():
            raise ValueError("source values must be finite")
        if not math.isfinite(diffusion_length) or not 0 < diffusion_length <= 1e4:
            raise ValueError("diffusion_length must be finite and in (0, 1e4]")
        height, width = field.shape
        ky = 4 * np.sin(np.pi * np.arange(height) / (2 * height)) ** 2
        kx = 4 * np.sin(np.pi * np.arange(width) / (2 * width)) ** 2
        eigenvalues = ky[:, None] + kx[None, :]
        sigma_sq = diffusion_length * diffusion_length
        return idctn(dctn(field) / (1.0 + sigma_sq * eigenvalues))

    def transport_with_scale_components(
        self, irradiance: np.ndarray, radii: tuple[float, ...] = default_radii
    ) -> np.ndarray:
        """Return the three unweighted scale fields as ``(scale,H,W,C)`` float64."""
        _require_finite_array(irradiance, name="irradiance")
        _require_finite_array(radii, name="radii")
        source = np.asarray(irradiance, dtype=np.float64)
        if source.ndim != 3 or source.shape[-1] < 1:
            raise ValueError("source must have shape (H,W,C) with at least one channel")
        if source.shape[0] < 2 or source.shape[1] < 2:
            raise ValueError("source spatial dimensions must be at least 2x2")
        if not np.isfinite(source).all():
            raise ValueError("source values must be finite")
        try:
            radius_values = np.asarray(radii, dtype=np.float64)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "radii must be a one-dimensional sequence of positive numbers"
            ) from exc
        if radius_values.ndim != 1 or radius_values.size not in (1, source.shape[-1]):
            raise ValueError("radii must contain one value or one value per image channel")
        if not np.isfinite(radius_values).all() or np.any(radius_values <= 0):
            raise ValueError("all radii must be finite and strictly positive")
        if np.any(radius_values > 1e4):
            raise ValueError("radii must be no greater than 1e4 normalized units")
        components = np.empty((self.scales.size, *source.shape), dtype=np.float64)
        for channel in range(source.shape[-1]):
            radius = float(radius_values[0 if radius_values.size == 1 else channel])
            for scale_index, scale in enumerate(self.scales):
                sigma = radius / float(scale)
                components[scale_index, ..., channel] = self.solve_single_scale(
                    source[..., channel], sigma
                )
        return components

    def transport(self, source: np.ndarray, radii: tuple[float, ...] = default_radii) -> np.ndarray:
        """Return the weighted three-scale transport; Neumann boundaries preserve mean flux."""
        _require_finite_array(source, name="source")
        _require_finite_array(radii, name="radii")
        components = self.transport_with_scale_components(source, radii)
        combined = components[0]
        combined *= self.weights[0]
        for index in range(1, self.weights.size):
            components[index] *= self.weights[index]
            combined += components[index]
        return cast(np.ndarray, combined)

    def shade(
        self,
        source: np.ndarray,
        heart_rate_bpm: float = 72,
        t: float = 0,
        sympathetic_flush: float = 0,
        blood_oxygenation: float = 0.97,
        radii: tuple[float, ...] = default_radii,
    ) -> np.ndarray:
        _require_finite_array(source, name="source")
        _require_finite_scalar(heart_rate_bpm, name="heart_rate_bpm")
        _require_finite_scalar(t, name="t")
        _require_finite_scalar(sympathetic_flush, name="sympathetic_flush")
        _require_finite_scalar(blood_oxygenation, name="blood_oxygenation")
        _require_finite_array(radii, name="radii")
        validate_physiology(heart_rate_bpm, t, sympathetic_flush, blood_oxygenation)
        source_array = np.asarray(source)
        if source_array.ndim != 3 or source_array.shape[0] < 2 or source_array.shape[1] < 2:
            raise ValueError("source must be (H,W,C) with spatial dimensions at least 2x2")
        if not np.all(np.isfinite(source_array)):
            raise ValueError("source values must be finite")
        a = self.transport(source, radii)
        pulse_cycles = math.fmod(heart_rate_bpm * t / 60.0, 1.0)
        phase = 1 + 0.025 * math.sin(2 * math.pi * pulse_cycles)
        oxy = np.asarray((0.08, 0.24, 0.10), dtype=np.float32)
        deoxy = np.asarray((0.20, 0.72, 0.66), dtype=np.float32)
        absorption = blood_oxygenation * oxy + (1 - blood_oxygenation) * deoxy
        concentration = 0.08 * np.clip(1 + 0.45 * sympathetic_flush, 0.4, 1.6)
        if a.shape[-1] == 3:
            a *= (phase * np.exp(-concentration * absorption))[None, None, :]
        else:
            a *= phase * np.exp(-concentration * float(absorption.mean()))
        return a

    @staticmethod
    def spherical_harmonic(normals: np.ndarray, light: np.ndarray | None = None) -> np.ndarray:
        _require_finite_array(normals, name="normals")
        if light is not None:
            _require_finite_array(light, name="light")
        n = np.asarray(normals, dtype=np.float32)
        if n.ndim < 1 or n.shape[-1] != 3:
            raise ValueError("normals must end in three components")
        if light is None:
            direction = np.asarray((0.35, -0.45, 0.82), dtype=np.float32)
            direction /= np.linalg.norm(direction)
            light_matrix = np.zeros((4, 4), dtype=np.float32)
            light_matrix[:3, :3] = 0.2 * np.outer(direction, direction)
            linear = 0.3 * direction
            light_matrix[:3, 3] = linear / 2
            light_matrix[3, :3] = linear / 2
            light_matrix[3, 3] = 0.5
        else:
            light_matrix = np.asarray(light, dtype=np.float32)
            if light_matrix.shape != (4, 4) or not np.allclose(
                light_matrix, light_matrix.T, atol=1e-6
            ):
                raise ValueError("light matrix must be symmetric with shape 4x4")
        homogeneous = np.concatenate((n, np.ones((*n.shape[:-1], 1), np.float32)), -1)
        return cast(
            np.ndarray, np.einsum("...i,ij,...j->...", homogeneous, light_matrix, homogeneous)
        )


def _require_finite_scalar(value: object, *, name: str) -> None:
    """Numeric inputs never silently coerce booleans or non-finite values."""
    if isinstance(value, (bool, np.bool_)):
        raise TypeError(f"{name} must not be bool")
    if not isinstance(value, (int, float, np.integer, np.floating)):
        raise TypeError(f"{name} must be numeric")
    if not np.isfinite(value):
        raise ValueError(f"{name} must be finite")


def _require_finite_array(value: object, *, name: str) -> None:
    """Validate before dtype conversion can erase boolean evidence."""
    raw = value if isinstance(value, np.ndarray) else np.asarray(value, dtype=object)
    if raw.dtype.kind == "b" or (
        raw.dtype.kind == "O" and any(isinstance(item, (bool, np.bool_)) for item in raw.flat)
    ):
        raise TypeError(f"{name} must not contain bool")
    try:
        finite = np.all(np.isfinite(np.asarray(value, dtype=np.float64)))
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{name} must be a one-dimensional sequence of numbers") from exc
    if not finite:
        raise ValueError(f"{name} must be finite")


def solve_screened_poisson_neumann(source: np.ndarray, screening_lambda: float) -> np.ndarray:
    """DCT-II solve of (I-lambda*Delta_N)u=f, with unit pixel spacing."""
    _require_finite_scalar(screening_lambda, name="screening_lambda")
    _require_finite_array(source, name="source")
    if screening_lambda < 0:
        raise ValueError("screening_lambda must be nonnegative")
    field = np.asarray(source, dtype=np.float64)
    if field.ndim != 2 or min(field.shape, default=0) < 2:
        raise ValueError("source must be a 2-D field at least 2x2")
    height, width = field.shape
    mu_y = -4 * np.sin(np.pi * np.arange(height) / (2 * height)) ** 2
    mu_x = -4 * np.sin(np.pi * np.arange(width) / (2 * width)) ** 2
    denominator = 1 - screening_lambda * (mu_y[:, None] + mu_x[None, :])
    return idctn(dctn(field) / denominator)


def verify_screened_poisson_conservation(
    source: np.ndarray,
    screening_lambda: float,
) -> dict[str, float | bool]:
    """DC preservation and graph-Dirichlet dissipation for Neumann boundaries."""
    solution = solve_screened_poisson_neumann(source, screening_lambda)
    field = np.asarray(source, dtype=np.float64)

    def energy(value: np.ndarray) -> float:
        return float(np.sum(np.diff(value, axis=0) ** 2) + np.sum(np.diff(value, axis=1) ** 2))

    dc_error = abs(float(solution.mean() - field.mean()))
    before, after = energy(field), energy(solution)
    return {
        "dc_error": dc_error,
        "source_energy": before,
        "solution_energy": after,
        "verified": dc_error < 1e-10 and after <= before + 1e-10,
    }
