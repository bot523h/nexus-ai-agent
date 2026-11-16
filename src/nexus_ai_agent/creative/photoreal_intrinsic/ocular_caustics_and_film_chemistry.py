"""Stylized, deterministic ocular optics and film response operators."""

from __future__ import annotations

from typing import cast

import numpy as np

from .log_chromophore_tomography_baker import IntrinsicBiophysicalVault


class OcularCausticsAndFilmChemistry:
    @staticmethod
    def pupil_diameter(illumination: float, t: float = 0) -> float:
        return float(
            np.clip(5.5 - 1.1 * np.log1p(max(0, illumination)) + 0.08 * np.sin(np.pi * t), 1.5, 6.0)
        )

    @staticmethod
    def caustic(shape: tuple[int, int], light_x: float = -1) -> np.ndarray:
        y, x = np.mgrid[-1 : 1 : complex(shape[0]), -1 : 1 : complex(shape[1])]
        return cast(
            np.ndarray,
            (np.exp(-((x + np.sign(light_x) * 0.35) ** 2 + y * y) / 0.12)).astype(np.float32),
        )

    @staticmethod
    def halation(image: np.ndarray) -> np.ndarray:
        a = np.asarray(image, dtype=np.float32)
        lum = a.mean(-1)
        edge = np.maximum(lum - OcularCausticsAndFilmChemistry._blur(lum), 0)
        return cast(np.ndarray, a + edge[..., None] * np.array([0.18, 0.05, 0.01], np.float32))

    @staticmethod
    def _blur(a: np.ndarray) -> np.ndarray:
        p = np.pad(a, 1, mode="edge")
        return cast(
            np.ndarray,
            (p[:-2, 1:-1] + p[2:, 1:-1] + p[1:-1, :-2] + p[1:-1, 2:] + 4 * p[1:-1, 1:-1]) / 8,
        )

    @staticmethod
    def film(
        image: np.ndarray, depth: np.ndarray | None = None, focus: float = 1.0, seed: int = 0
    ) -> np.ndarray:
        a = np.asarray(image, dtype=np.float32)
        density = 1 / (1 + np.exp(-8 * (a - 0.5)))
        1 / (1 + np.exp(-8 * (0.5 - 0.5)))
        dmax = 1.0
        variance = np.sqrt(np.maximum(density * (1 - density / dmax), 0))
        rng = np.random.default_rng(seed)
        out = (
            OcularCausticsAndFilmChemistry.halation(density)
            + rng.normal(0, 1, density.shape).astype(np.float32) * variance * 0.012
        )
        if depth is not None:
            coc = np.clip(np.abs(depth - focus) / np.maximum(depth, 1e-3), 0, 1)
            out = out * (1 - 0.1 * coc[..., None])
        return cast(np.ndarray, np.clip(out, 0, 1).astype(np.float32))

    @classmethod
    def compile(
        cls,
        vault: IntrinsicBiophysicalVault,
        light: np.ndarray | None = None,
        focus: float = 1.0,
        t: float = 0,
    ) -> dict[str, object]:
        from .screened_poisson_skin_transport import ScreenedPoissonSkinTransport

        image = np.asarray(vault.albedo, dtype=np.float32)
        n = np.asarray(vault.normals)
        shading = ScreenedPoissonSkinTransport.spherical_harmonic(n, light)
        frame = cls.film(
            np.clip(image * np.maximum(shading[..., None], 0), 0, 1),
            vault.depth,
            focus,
            int(t * 1000),
        )
        return {
            "rgb": np.rint(frame * 255).astype(np.uint8),
            "float32": frame,
            "metadata": {"model": "deterministic-cpu-preview", "focus": focus, "time": t},
        }
