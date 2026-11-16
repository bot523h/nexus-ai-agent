"""Snell caustics, unpolarized Fresnel, and deterministic film response."""

from __future__ import annotations

from typing import cast

import numpy as np

from .log_chromophore_tomography_baker import IntrinsicBiophysicalVault
from .riemannian_strain_wrinkle_tensor import RiemannianStrainWrinkleTensor
from .screened_poisson_skin_transport import ScreenedPoissonSkinTransport


class OcularCausticsAndFilmChemistry:
    @staticmethod
    def pupil_diameter(illumination: float, t: float = 0) -> float:
        return float(
            np.clip(5.5 - 1.1 * np.log1p(max(0, illumination)) + 0.08 * np.sin(np.pi * t), 1.5, 6.0)
        )

    @staticmethod
    def caustic(shape: tuple[int, int], light_x: float = -1) -> np.ndarray:
        """Trace a corneal ray bundle and conserve transmitted Fresnel flux when splatting."""
        if min(shape) < 2:
            raise ValueError("caustic image dimensions must be at least 2x2")
        height, width = shape
        y, x = np.mgrid[-1 : 1 : complex(height), -1 : 1 : complex(width)]
        rho2 = x * x + y * y
        inside = rho2 < 0.96
        z = np.sqrt(np.maximum(1 - rho2, 1e-8))
        normal = np.stack((x, y, z), axis=-1)
        incident_dir = np.asarray((-0.34 * np.sign(light_x), 0.0, -1.0), dtype=np.float64)
        incident_dir /= np.linalg.norm(incident_dir)
        incident = np.broadcast_to(incident_dir, (*shape, 3))
        n_air, n_cornea = 1.0, 1.376
        eta = n_air / n_cornea
        cos_i = np.clip(-np.sum(incident * normal, axis=-1), 0.0, 1.0)
        sin2_t = eta**2 * (1 - cos_i**2)
        cos_t = np.sqrt(np.maximum(0.0, 1 - sin2_t))
        refracted = eta * incident + (eta * cos_i[..., None] - cos_t[..., None]) * normal
        refracted /= np.maximum(np.linalg.norm(refracted, axis=-1, keepdims=True), 1e-12)
        rs = ((n_air * cos_i - n_cornea * cos_t) / (n_air * cos_i + n_cornea * cos_t + 1e-12)) ** 2
        rp = ((n_air * cos_t - n_cornea * cos_i) / (n_air * cos_t + n_cornea * cos_i + 1e-12)) ** 2
        fresnel = np.clip(0.5 * (rs + rp), 0.0, 1.0)
        transmission = 1 - fresnel
        plane_z = -0.35
        distance = (plane_z - z) / np.where(
            np.abs(refracted[..., 2]) < 1e-8, -1e-8, refracted[..., 2]
        )
        hit_x = x + distance * refracted[..., 0]
        hit_y = y + distance * refracted[..., 1]
        valid = (
            inside & (distance > 0) & (hit_x >= -1) & (hit_x <= 1) & (hit_y >= -1) & (hit_y <= 1)
        )
        px = np.clip((hit_x + 1) * (width - 1) / 2, 0, width - 1)
        py = np.clip((hit_y + 1) * (height - 1) / 2, 0, height - 1)
        x0, y0 = px.astype(np.intp), py.astype(np.intp)
        x1, y1 = np.minimum(x0 + 1, width - 1), np.minimum(y0 + 1, height - 1)
        dx, dy = px - x0, py - y0
        energy = transmission * cos_i * inside
        flux = np.zeros(shape, dtype=np.float64)
        mask = valid
        np.add.at(flux, (y0[mask], x0[mask]), (energy * (1 - dx) * (1 - dy))[mask])
        np.add.at(flux, (y0[mask], x1[mask]), (energy * dx * (1 - dy))[mask])
        np.add.at(flux, (y1[mask], x0[mask]), (energy * (1 - dx) * dy)[mask])
        np.add.at(flux, (y1[mask], x1[mask]), (energy * dx * dy)[mask])
        # First Purkinje reflection is Fresnel-weighted and remains on the corneal side.
        catch_x = -0.55 * np.sign(light_x)
        purkinje = np.sum(fresnel * inside) * np.exp(
            -((x - catch_x) ** 2 + (y + 0.38) ** 2) / 0.008
        )
        return cast(np.ndarray, (flux + purkinje).astype(np.float32))

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
        grain_sigma = np.sqrt(np.maximum(density * (1 - density), 0))
        rng = np.random.default_rng(seed)
        out = (
            OcularCausticsAndFilmChemistry.halation(density)
            + rng.normal(0, 1, density.shape).astype(np.float32) * grain_sigma * 0.012
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
        t: float = 0.0,
        hmac_key: bytes | None = None,
        heart_rate_bpm: float = 72.0,
        sympathetic_flush: float = 0.0,
        blood_oxygenation: float = 0.97,
        flow: np.ndarray | None = None,
        include_caustics: bool = True,
    ) -> dict[str, object]:
        if not vault.verify(hmac_key=hmac_key):
            raise ValueError("Invalid vault seal")
        time_sec = t
        mel = np.abs(vault.melanin).astype(np.float32)
        hem = np.abs(vault.hemoglobin).astype(np.float32)
        flush = np.clip(1 + 0.45 * sympathetic_flush, 0.4, 1.6)
        oxygen = np.clip(blood_oxygenation, 0.70, 0.99)
        hb_o = oxygen * np.array((0.08, 0.24, 0.10), np.float32)
        hb_d = (1 - oxygen) * np.array((0.20, 0.72, 0.66), np.float32)
        mel_spectrum = np.array((0.22, 0.45, 0.82), np.float32)
        attenuation = np.exp(
            -0.035 * mel[..., None] * mel_spectrum - 0.08 * hem[..., None] * flush * (hb_o + hb_d)
        )
        rgb = vault.albedo * vault.shading[..., None] * attenuation
        h, w = rgb.shape[:2]
        displacement = (
            np.zeros((h, w, 2), np.float32) if flow is None else np.asarray(flow, dtype=np.float32)
        )
        mechanics = RiemannianStrainWrinkleTensor().analyze(displacement, surface_depth=vault.depth)
        normal = RiemannianStrainWrinkleTensor.perturb_normals(vault.normals, mechanics["wrinkle"])
        warped_rgb = RiemannianStrainWrinkleTensor.warp(rgb, displacement)
        warped_normal = RiemannianStrainWrinkleTensor.warp(normal, displacement)
        roughness = RiemannianStrainWrinkleTensor.warp(
            np.asarray(mechanics["roughness"], np.float32), displacement
        )
        ao = RiemannianStrainWrinkleTensor.warp(
            np.asarray(mechanics["ao"], np.float32), displacement
        )
        transported = ScreenedPoissonSkinTransport().shade(
            warped_rgb, heart_rate_bpm, t, sympathetic_flush, blood_oxygenation
        )
        illumination = np.maximum(
            ScreenedPoissonSkinTransport.spherical_harmonic(warped_normal, light), 0
        )
        ndotv = np.clip(warped_normal[..., 2], 0, 1)
        fresnel = 0.04 + 0.96 * (1 - ndotv) ** 5
        specular = fresnel * np.exp(-4 * np.maximum(roughness, 0)) * ao
        lit = transported * illumination[..., None] + specular[..., None] * 0.04
        if include_caustics:
            ocular = cls.caustic(
                (h, w), -1 if light is None else float(np.sign(np.asarray(light)[0, 3]))
            )
            lit = lit + ocular[..., None] * 0.015
        halated = cls.halation(np.clip(lit, 0, 1))
        frame = cls.film(halated, vault.depth, focus, int(time_sec * 1000))
        return {
            "rgb": np.rint(frame * 255).astype(np.uint8),
            "float32": frame,
            "metadata": {
                "model": "intrinsic-six-stage-cpu",
                "focus": focus,
                "time": time_sec,
                "heart_rate_bpm": heart_rate_bpm,
                "sympathetic_flush": sympathetic_flush,
                "blood_oxygenation": blood_oxygenation,
                "include_caustics": include_caustics,
            },
        }
