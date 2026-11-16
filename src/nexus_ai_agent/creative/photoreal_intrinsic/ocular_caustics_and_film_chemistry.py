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
        if min(shape) < 2:
            raise ValueError("caustic image dimensions must be at least 2x2")
        y, x = np.mgrid[-1 : 1 : complex(shape[0]), -1 : 1 : complex(shape[1])]
        rho2 = x * x + y * y
        inside = rho2 < 0.96
        z = np.sqrt(np.maximum(1 - rho2, 1e-6))
        normal = np.stack((x, y, z), axis=-1)
        direction = np.asarray((-0.34 * np.sign(light_x), 0.0, -1.0), dtype=np.float64)
        direction /= np.linalg.norm(direction)
        incident = np.broadcast_to(direction, (*shape, 3))
        eta = 1.0 / 1.376
        cos_i = np.clip(-np.sum(incident * normal, axis=-1, keepdims=True), 0.0, 1.0)
        sin2_t = eta**2 * (1.0 - cos_i**2)
        refracted = eta * incident + (eta * cos_i - np.sqrt(np.maximum(0.0, 1.0 - sin2_t))) * normal
        refracted /= np.maximum(np.linalg.norm(refracted, axis=-1, keepdims=True), 1e-12)
        plane_z = -0.35
        distance = (plane_z - z) / np.where(
            np.abs(refracted[..., 2]) < 1e-6, -1e-6, refracted[..., 2]
        )
        hit_x = x + distance * refracted[..., 0]
        hit_y = y + distance * refracted[..., 1]
        valid = inside & (distance > 0)
        # Flux concentration at the caustic focus plus the first Purkinje surface catchlight.
        focus_x = float(np.median(hit_x[valid])) if np.any(valid) else 0.25 * -np.sign(light_x)
        focus_y = float(np.median(hit_y[valid])) if np.any(valid) else 0.0
        caustic = np.exp(-((x - focus_x) ** 2 + (y - focus_y) ** 2) / 0.035)
        catchlight = 0.35 * np.exp(-((x + 0.55 * np.sign(light_x)) ** 2 + (y + 0.38) ** 2) / 0.008)
        return cast(np.ndarray, (caustic + catchlight).astype(np.float32))

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
