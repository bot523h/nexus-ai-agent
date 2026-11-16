"""Integral-image skin transport and inexpensive physiological shading."""

from __future__ import annotations

from typing import cast

import numpy as np


class ScreenedPoissonSkinTransport:
    weights = np.array((0.5218, 0.3341, 0.1441), np.float32)
    scales = np.array((0.618, 1.618, 3.618), np.float32)

    @staticmethod
    def _box(a: np.ndarray, r: int) -> np.ndarray:
        p = np.pad(a, ((r, r), (r, r)), mode="edge")
        ii = np.pad(p, ((1, 0), (1, 0))).cumsum(0).cumsum(1)
        return cast(
            np.ndarray,
            (
                ii[2 * r + 1 :, 2 * r + 1 :]
                - ii[: -2 * r - 1, 2 * r + 1 :]
                - ii[2 * r + 1 :, : -2 * r - 1]
                + ii[: -2 * r - 1, : -2 * r - 1]
            )
            / ((2 * r + 1) ** 2),
        )

    def transport(
        self, source: np.ndarray, radii: tuple[float, float, float] = (3.4, 1.35, 0.65)
    ) -> np.ndarray:
        a = np.asarray(source, dtype=np.float32)
        if a.ndim != 3 or a.shape[-1] != 3:
            raise ValueError("source must be RGB image")
        out = np.empty_like(a)
        for c, radius in enumerate(radii):
            out[..., c] = sum(
                float(w) * self._box(a[..., c], max(0, int(round(radius * float(s)))))
                for w, s in zip(self.weights, self.scales, strict=False)
            )
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
        a[..., 0] *= 1 + 0.22 * sympathetic_flush
        a[..., 1] *= 1 - 0.08 * sympathetic_flush
        a[..., 2] *= blood_oxygenation
        return cast(np.ndarray, a)

    @staticmethod
    def spherical_harmonic(normals: np.ndarray, light: np.ndarray | None = None) -> np.ndarray:
        n = np.asarray(normals, dtype=np.float32)
        light_matrix = (
            np.eye(4, dtype=np.float32) if light is None else np.asarray(light, dtype=np.float32)
        )
        return cast(
            np.ndarray,
            np.einsum(
                "...i,ij,...j->...",
                np.concatenate((n, np.ones((*n.shape[:-1], 1), np.float32)), -1),
                light_matrix,
                np.concatenate((n, np.ones((*n.shape[:-1], 1), np.float32)), -1),
            ),
        )
