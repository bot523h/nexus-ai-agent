"""Integral-image skin transport and inexpensive physiological shading."""

from __future__ import annotations

from typing import cast

import numpy as np


class ScreenedPoissonSkinTransport:
    weights = np.array((0.5218, 0.3341, 0.1441), np.float32)
    scales = np.array((0.618, 1.618, 3.618), np.float32)

    def transport(
        self, source: np.ndarray, radii: tuple[float, float, float] = (3.4, 1.35, 0.65)
    ) -> np.ndarray:
        a = np.asarray(source, dtype=np.float32)
        if a.ndim != 3 or a.shape[-1] != 3:
            raise ValueError("source must be RGB image")
        height, width, _ = a.shape
        ky = 2 - 2 * np.cos(2 * np.pi * np.fft.fftfreq(height))[:, None]
        kx = 2 - 2 * np.cos(2 * np.pi * np.fft.fftfreq(width))[None, :]
        laplace = ky + kx
        out = np.empty_like(a)
        for channel, radius in enumerate(radii):
            spectrum = np.fft.fft2(a[..., channel])
            result = np.zeros((height, width), dtype=np.float64)
            for weight, scale in zip(self.weights, self.scales, strict=True):
                kappa_squared = (float(scale) / radius) ** 2
                result += (
                    float(weight)
                    * np.fft.ifft2(spectrum * kappa_squared / (laplace + kappa_squared)).real
                )
            out[..., channel] = result
        return out

    def shade(
        self,
        source: np.ndarray,
        heart_rate_bpm: float = 72,
        t: float = 0,
        sympathetic_flush: float = 0,
        blood_oxygenation: float = 0.97,
    ) -> np.ndarray:
        if not -1 <= sympathetic_flush <= 1 or not 0.70 <= blood_oxygenation <= 0.99:
            raise ValueError("physiology outside supported range")
        phase = 1 + 0.025 * np.sin(2 * np.pi * heart_rate_bpm * t / 60)
        a = self.transport(source) * phase
        oxy = np.asarray((0.08, 0.24, 0.10), dtype=np.float32)
        deoxy = np.asarray((0.20, 0.72, 0.66), dtype=np.float32)
        absorption = blood_oxygenation * oxy + (1 - blood_oxygenation) * deoxy
        concentration = 0.08 * np.clip(1 + 0.45 * sympathetic_flush, 0.4, 1.6)
        a *= np.exp(-concentration * absorption)[None, None, :]
        return cast(np.ndarray, a)

    @staticmethod
    def spherical_harmonic(normals: np.ndarray, light: np.ndarray | None = None) -> np.ndarray:
        n = np.asarray(normals, dtype=np.float32)
        if light is None:
            direction = np.asarray((0.35, -0.45, 0.82), dtype=np.float32)
            direction /= np.linalg.norm(direction)
            quadratic = 0.2 * np.outer(direction, direction)
            linear = 0.3 * direction
            light_matrix = np.zeros((4, 4), dtype=np.float32)
            light_matrix[:3, :3] = quadratic
            light_matrix[:3, 3] = linear / 2
            light_matrix[3, :3] = linear / 2
            light_matrix[3, 3] = 0.5
        else:
            light_matrix = np.asarray(light, dtype=np.float32)
        return cast(
            np.ndarray,
            np.einsum(
                "...i,ij,...j->...",
                np.concatenate((n, np.ones((*n.shape[:-1], 1), np.float32)), -1),
                light_matrix,
                np.concatenate((n, np.ones((*n.shape[:-1], 1), np.float32)), -1),
            ),
        )
