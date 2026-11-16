"""Solve the screened-Poisson skin transport equation with Neumann DCT-II boundaries."""

from __future__ import annotations

from typing import cast

import numpy as np

from .log_chromophore_tomography_baker import dctn, idctn, validate_physiology


class ScreenedPoissonSkinTransport:
    """DCT-II solution of (-Laplacian+kappa²)Phi=kappa²S, with Neumann edges."""

    weights = np.array((0.5218, 0.3341, 0.1441), np.float64)
    scales = np.array((0.618, 1.618, 3.618), np.float64)
    default_radii = (3.4, 1.35, 0.65)

    def transport(self, source: np.ndarray, radii: tuple[float, ...] = default_radii) -> np.ndarray:
        a = np.asarray(source, dtype=np.float64)
        if a.ndim != 3 or a.shape[-1] < 1:
            raise ValueError("source must have shape (H,W,C) with at least one channel")
        if a.shape[0] < 2 or a.shape[1] < 2:
            raise ValueError("source spatial dimensions must be at least 2x2")
        if not np.all(np.isfinite(a)):
            raise ValueError("source values must be finite")
        try:
            radius_values = np.asarray(radii, dtype=np.float64)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "radii must be a one-dimensional sequence of positive numbers"
            ) from exc
        if radius_values.ndim != 1 or radius_values.size not in (1, a.shape[-1]):
            raise ValueError("radii must contain one value or one value per image channel")
        if not np.isfinite(radius_values).all() or np.any(radius_values <= 0):
            raise ValueError("all radii must be finite and strictly positive")
        height, width, channels = a.shape
        ky = 4 * np.sin(np.pi * np.arange(height) / (2 * height)) ** 2
        kx = 4 * np.sin(np.pi * np.arange(width) / (2 * width)) ** 2
        eigenvalues = ky[:, None] + kx[None, :]
        result = np.empty_like(a, dtype=np.float32)
        for channel in range(channels):
            radius = float(radius_values[0 if radius_values.size == 1 else channel])
            source_hat = dctn(a[..., channel])
            solved = np.zeros((height, width), dtype=np.float64)
            for weight, scale in zip(self.weights, self.scales, strict=True):
                ratio = float(np.clip(float(scale) / radius, 1e-4, 1e4))
                kappa2 = ratio * ratio
                solved += weight * idctn(source_hat * (kappa2 / (eigenvalues + kappa2)))
            result[..., channel] = solved
        return result

    def shade(
        self,
        source: np.ndarray,
        heart_rate_bpm: float = 72,
        t: float = 0,
        sympathetic_flush: float = 0,
        blood_oxygenation: float = 0.97,
        radii: tuple[float, ...] = default_radii,
    ) -> np.ndarray:
        validate_physiology(heart_rate_bpm, t, sympathetic_flush, blood_oxygenation)
        source_array = np.asarray(source)
        if source_array.ndim != 3 or source_array.shape[0] < 2 or source_array.shape[1] < 2:
            raise ValueError("source must be (H,W,C) with spatial dimensions at least 2x2")
        if not np.all(np.isfinite(source_array)):
            raise ValueError("source values must be finite")
        a = self.transport(source, radii)
        phase = 1 + 0.025 * np.sin(2 * np.pi * heart_rate_bpm * t / 60)
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
