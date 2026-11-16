"""Snell caustics, unpolarized Fresnel, and deterministic film response."""

from __future__ import annotations

from typing import cast

import numpy as np

from .log_chromophore_tomography_baker import (
    IntrinsicBiophysicalVault,
    KeyPolicy,
    validate_physiology,
)
from .riemannian_strain_wrinkle_tensor import RiemannianStrainWrinkleTensor
from .screened_poisson_skin_transport import ScreenedPoissonSkinTransport


class OcularCausticsAndFilmChemistry:
    @staticmethod
    def pupil_diameter(illumination: float, t: float = 0) -> float:
        return float(
            np.clip(5.5 - 1.1 * np.log1p(max(0, illumination)) + 0.08 * np.sin(np.pi * t), 1.5, 6.0)
        )

    @staticmethod
    def fresnel_coefficients(cos_theta_i: np.ndarray | float) -> tuple[np.ndarray, np.ndarray]:
        cosine = np.clip(np.asarray(cos_theta_i, dtype=np.float64), 0.0, 1.0)
        n1, n2 = 1.0, 1.376
        eta = n1 / n2
        cos_theta_t = np.sqrt(np.maximum(0.0, 1.0 - eta**2 * (1.0 - cosine**2)))
        rs = ((n1 * cosine - n2 * cos_theta_t) / (n1 * cosine + n2 * cos_theta_t + 1e-12)) ** 2
        rp = ((n1 * cos_theta_t - n2 * cosine) / (n1 * cos_theta_t + n2 * cosine + 1e-12)) ** 2
        reflectance = np.clip(0.5 * (rs + rp), 0.0, 1.0)
        transmittance = 1.0 - reflectance
        return cast(np.ndarray, reflectance), cast(np.ndarray, transmittance)

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
        incident_x = -0.34 * np.sign(light_x) / np.sqrt(1.0 + 0.34**2)
        incident_z = -1.0 / np.sqrt(1.0 + 0.34**2)
        n_air, n_cornea = 1.0, 1.376
        eta = n_air / n_cornea
        cos_i = np.clip(-(incident_x * x + incident_z * z), 0.0, 1.0)
        sin2_t = eta**2 * (1 - cos_i**2)
        cos_t = np.sqrt(np.maximum(0.0, 1 - sin2_t))
        normal_scale = eta * cos_i - cos_t
        refracted_x = eta * incident_x + normal_scale * x
        refracted_z = eta * incident_z + normal_scale * z
        refracted_norm = np.sqrt(refracted_x**2 + refracted_z**2)
        refracted_x /= np.maximum(refracted_norm, 1e-12)
        refracted_z /= np.maximum(refracted_norm, 1e-12)
        fresnel, transmission = OcularCausticsAndFilmChemistry.fresnel_coefficients(cos_i)
        plane_z = -0.35
        distance = (plane_z - z) / np.where(np.abs(refracted_z) < 1e-8, -1e-8, refracted_z)
        hit_x = x + distance * refracted_x
        hit_y = y + distance * (normal_scale * y / np.maximum(refracted_norm, 1e-12))
        valid = (
            inside & (distance > 0) & (hit_x >= -1) & (hit_x <= 1) & (hit_y >= -1) & (hit_y <= 1)
        )
        px = np.clip((hit_x + 1) * (width - 1) / 2, 0, width - 1)
        py = np.clip((hit_y + 1) * (height - 1) / 2, 0, height - 1)
        x0, y0 = px.astype(np.intp), py.astype(np.intp)
        x1, y1 = np.minimum(x0 + 1, width - 1), np.minimum(y0 + 1, height - 1)
        dx, dy = px - x0, py - y0
        pixel_area = 4.0 / (height * width)
        ray_energy = transmission * cos_i * valid * pixel_area
        flux = np.zeros(shape, dtype=np.float64)
        np.add.at(flux, (y0[valid], x0[valid]), (ray_energy * (1 - dx) * (1 - dy))[valid])
        np.add.at(flux, (y0[valid], x1[valid]), (ray_energy * dx * (1 - dy))[valid])
        np.add.at(flux, (y1[valid], x0[valid]), (ray_energy * (1 - dx) * dy)[valid])
        np.add.at(flux, (y1[valid], x1[valid]), (ray_energy * dx * dy)[valid])
        reflected_budget = float(np.sum(fresnel * cos_i * inside) * pixel_area)
        catch_x = -0.55 * np.sign(light_x)
        kernel = np.exp(-((x - catch_x) ** 2 + (y + 0.38) ** 2) / 0.008)
        kernel /= max(float(kernel.sum()), 1e-12)
        purkinje_energy = reflected_budget * kernel
        # A fixed normalized-coordinate PSF makes the discrete caustic peak converge by scale.
        fy = np.fft.fftfreq(height)[:, None]
        fx = np.fft.fftfreq(width)[None, :]
        sigma_pixels = 0.012 * min(height, width)
        psf = np.exp(-2 * np.pi**2 * sigma_pixels**2 * (fy**2 + fx**2))
        flux = np.maximum(np.fft.ifft2(np.fft.fft2(flux) * psf).real, 0)
        # Radiance is pixel energy divided by pixel area; its area integral conserves flux.
        return cast(np.ndarray, ((flux + purkinje_energy) / pixel_area).astype(np.float32))

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
        rng = np.random.default_rng(int(seed) & 0xFFFFFFFFFFFFFFFF)
        out = (
            OcularCausticsAndFilmChemistry.halation(density)
            + rng.normal(0, 1, density.shape).astype(np.float32) * grain_sigma * 0.012
        )
        if depth is not None:
            if not np.isfinite(focus):
                raise ValueError("focus must be finite")
            depth_array = np.asarray(depth, dtype=np.float64)
            if depth_array.shape != a.shape[:2] or not np.isfinite(depth_array).all():
                raise ValueError("depth must be finite and match image spatial dimensions")
            depth_offset = float(np.mean(depth_array))
            centered_depth = depth_array - depth_offset
            centered_focus = focus
            coc = np.clip(
                np.abs(centered_depth - centered_focus) / np.maximum(np.abs(centered_depth), 1e-3),
                0,
                1,
            )
            out = out * (1 - 0.1 * coc[..., None])
        return cast(np.ndarray, np.clip(out, 0, 1).astype(np.float32))

    @staticmethod
    def _beer_lambert_attenuation(
        melanin: np.ndarray,
        hemoglobin: np.ndarray,
        flush: float,
        pulse_phase: float,
        oxygenation: float,
    ) -> np.ndarray:
        """Return exp(-sum(absorption)) with nonnegative chromophore densities."""
        mel = np.asarray(melanin, dtype=np.float32)
        hem = np.asarray(hemoglobin, dtype=np.float32)
        if np.any(mel < 0) or np.any(hem < 0):
            raise ValueError("chromophore concentrations must be nonnegative")
        hb_o = oxygenation * np.array((0.08, 0.24, 0.10), np.float32)
        hb_d = (1 - oxygenation) * np.array((0.20, 0.72, 0.66), np.float32)
        absorption = hb_o + hb_d
        exponent = np.empty((*mel.shape, 3), dtype=np.float32)
        exponent[...] = -0.035 * mel[..., None] * np.array((0.22, 0.45, 0.82), np.float32)
        np.exp(exponent, out=exponent)
        hem_factor = np.empty(hem.shape, dtype=np.float32)
        for channel in range(3):
            np.multiply(hem, -0.08 * flush * pulse_phase * absorption[channel], out=hem_factor)
            np.exp(hem_factor, out=hem_factor)
            exponent[..., channel] *= hem_factor
        return exponent

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
        key_policy: KeyPolicy = "auto",
        time_sec: float | None = None,
    ) -> dict[str, object]:
        time_value = t if time_sec is None else time_sec
        validate_physiology(heart_rate_bpm, time_value, sympathetic_flush, blood_oxygenation, focus)
        if not vault.verify(hmac_key=hmac_key, key_policy=key_policy):
            raise ValueError("Invalid vault seal")
        pulse_phase = 1 + 0.025 * np.sin(2 * np.pi * heart_rate_bpm * time_value / 60)
        flush = float(np.clip(1 + 0.45 * sympathetic_flush, 0.4, 1.6))
        attenuation = cls._beer_lambert_attenuation(
            vault.melanin, vault.hemoglobin, flush, pulse_phase, blood_oxygenation
        )
        # Reconstruct chromophore absorption over albedo only; stored shade is not reapplied.
        rgb = np.multiply(vault.albedo, attenuation, dtype=np.float32)
        del attenuation
        h, w = rgb.shape[:2]
        displacement = (
            np.zeros((h, w, 2), np.float32) if flow is None else np.asarray(flow, dtype=np.float32)
        )
        mechanics = RiemannianStrainWrinkleTensor().analyze(displacement, surface_depth=vault.depth)
        normal = RiemannianStrainWrinkleTensor.perturb_normals(vault.normals, mechanics["wrinkle"])
        warped_rgb = RiemannianStrainWrinkleTensor.warp(rgb, displacement)
        warped_normal = RiemannianStrainWrinkleTensor.warp(normal, displacement)
        reference_normal = np.zeros_like(warped_normal)
        reference_normal[..., 2] = 1.0
        warped_depth = RiemannianStrainWrinkleTensor.warp(vault.depth, displacement)
        roughness = RiemannianStrainWrinkleTensor.warp(
            np.asarray(mechanics["roughness"], np.float32), displacement
        )
        ao = RiemannianStrainWrinkleTensor.warp(
            np.asarray(mechanics["ao"], np.float32), displacement
        )
        transported = ScreenedPoissonSkinTransport().transport(warped_rgb)
        reference_illumination = np.maximum(
            ScreenedPoissonSkinTransport.spherical_harmonic(reference_normal, light), 1e-4
        )
        illumination = np.maximum(
            ScreenedPoissonSkinTransport.spherical_harmonic(warped_normal, light), 0
        )
        illumination_ratio = np.clip(illumination / reference_illumination, 0, 4)
        ndotv = np.clip(warped_normal[..., 2], 0, 1)
        fresnel = 0.04 + 0.96 * (1 - ndotv) ** 5
        specular = fresnel * np.exp(-4 * np.maximum(roughness, 0)) * ao
        lit = transported * illumination_ratio[..., None] + specular[..., None] * 0.04
        del (
            rgb,
            displacement,
            mechanics,
            normal,
            warped_rgb,
            warped_normal,
            reference_normal,
            roughness,
            ao,
            transported,
            reference_illumination,
            illumination,
            illumination_ratio,
            ndotv,
            fresnel,
            specular,
        )
        if include_caustics:
            ocular = cls.caustic(
                (h, w), -1 if light is None else float(np.sign(np.asarray(light)[0, 3]))
            )
            lit = lit + ocular[..., None] * 0.015
        halated = cls.halation(np.clip(lit, 0, 1))
        seed = int(round(float(time_value) * 1000.0)) & 0xFFFFFFFFFFFFFFFF
        frame = cls.film(halated, warped_depth, focus, seed)
        return {
            "rgb": np.rint(frame * 255).astype(np.uint8),
            "float32": frame,
            "metadata": {
                "model": "intrinsic-six-stage-cpu",
                "focus": focus,
                "time": time_value,
                "heart_rate_bpm": heart_rate_bpm,
                "sympathetic_flush": sympathetic_flush,
                "blood_oxygenation": blood_oxygenation,
                "include_caustics": include_caustics,
            },
        }
