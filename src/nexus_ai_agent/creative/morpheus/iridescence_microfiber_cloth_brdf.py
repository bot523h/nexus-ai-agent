"""Wave-Optics Airy Thin-Film Iridescence and Estevez-Kulla Charlie Sheen.

Wave 7 of NAGAR MORPHEUS:
Provides dielectric thin-film wave interference via exact Airy summation over
RGB wavelengths (650, 550, 450 nm), proving [0, 1] bounds and d=0 collapse to
bare substrate reflectance, coupled with Estevez & Kulla Charlie microfiber sheen
NDF and Neubelt-Pettineo soft-cloth grazing retro-reflection.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

import numpy as np


@dataclass(frozen=True)
class ThinFilmProperties:
    """Dielectric thin-film interference layer parameters."""

    n1_ambient: float = 1.0  # air IOR
    n2_film: float = 1.4  # thin-film layer IOR
    n3_substrate: float = 1.6  # base fabric substrate IOR
    thickness_nm: float = 400.0  # film thickness in nanometers

    def __post_init__(self) -> None:
        if self.n1_ambient <= 0.0 or self.n2_film <= 0.0 or self.n3_substrate <= 0.0:
            raise ValueError("Refractive indices must be strictly positive")
        if not (0.0 <= self.thickness_nm <= 2000.0):
            raise ValueError(f"thickness_nm must be in [0, 2000], got {self.thickness_nm}")


@dataclass(frozen=True)
class MicrofiberSheenProperties:
    """Estevez-Kulla Charlie microfiber sheen cloth parameters."""

    sheen_color: np.ndarray  # shape: (3,), RGB in [0.0, 1.0]
    roughness: float = 0.3  # in [0.01, 1.0]

    def __post_init__(self) -> None:
        if (
            self.sheen_color.shape != (3,)
            or (self.sheen_color < 0.0).any()
            or (self.sheen_color > 1.0).any()
        ):
            raise ValueError("sheen_color must be (3,) with values in [0, 1]")
        if not (0.01 <= self.roughness <= 1.0):
            raise ValueError(f"roughness must be in [0.01, 1.0], got {self.roughness}")


@dataclass(frozen=True)
class ClothBRDFResult:
    """Outputs from combined iridescence and microfiber cloth BRDF evaluation."""

    airy_reflectance_rgb: np.ndarray  # shape: (3,), in [0.0, 1.0]
    charlie_sheen_radiance: np.ndarray  # shape: (3,)
    combined_radiance: np.ndarray  # shape: (3,)


def evaluate_airy_thin_film_reflectance(
    theta_in: float,
    film: ThinFilmProperties,
    wavelengths_nm: tuple[float, float, float] = (650.0, 550.0, 450.0),
) -> np.ndarray:
    """Evaluate spectral wave-optics Airy thin-film reflectance for Red, Green, Blue.

    Guarantees:
      1. Reflectance R(lambda) in [0.0, 1.0] for all wavelengths and incident angles.
      2. As thickness d -> 0 nm, R collapses smoothly to bare substrate Fresnel reflectance.
      3. Incident angle shifts spectral reflectance hue deterministically.
    """
    cos_theta1 = float(np.clip(np.cos(theta_in), 1e-6, 1.0))
    sin_theta1 = float(np.sqrt(max(1.0 - cos_theta1**2, 0.0)))

    n1, n2, n3 = film.n1_ambient, film.n2_film, film.n3_substrate
    d = film.thickness_nm

    # Snell's law in medium 2 (film)
    sin_theta2 = (n1 / n2) * sin_theta1
    if sin_theta2 > 1.0:
        return np.ones(3, dtype=np.float64)  # total internal reflection
    cos_theta2 = float(np.sqrt(max(1.0 - sin_theta2**2, 0.0)))

    # Snell's law in medium 3 (substrate)
    sin_theta3 = (n1 / n3) * sin_theta1
    cos_theta3 = float(np.sqrt(max(1.0 - min(sin_theta3**2, 1.0), 0.0)))

    # Optical path difference: Delta = 2 * n2 * d * cos(theta2)
    opd = 2.0 * n2 * d * cos_theta2

    # Fresnel amplitude reflection coefficients at interfaces 1-2 and 2-3
    # s-polarization (TE)
    r12_s = (n1 * cos_theta1 - n2 * cos_theta2) / max(n1 * cos_theta1 + n2 * cos_theta2, 1e-12)
    r23_s = (n2 * cos_theta2 - n3 * cos_theta3) / max(n2 * cos_theta2 + n3 * cos_theta3, 1e-12)

    # p-polarization (TM)
    r12_p = (n2 * cos_theta1 - n1 * cos_theta2) / max(n2 * cos_theta1 + n1 * cos_theta2, 1e-12)
    r23_p = (n3 * cos_theta2 - n2 * cos_theta3) / max(n3 * cos_theta2 + n2 * cos_theta3, 1e-12)

    rgb_reflectance = np.zeros(3, dtype=np.float64)

    for i, w_nm in enumerate(wavelengths_nm):
        # Phase difference delta = 2 * pi * Delta / lambda
        delta = (2.0 * np.pi * opd) / w_nm

        # Airy summation formula:
        # R = (r12^2 + r23^2 + 2*r12*r23*cos(delta)) / (1 + r12^2*r23^2 + 2*r12*r23*cos(delta))
        num_s = r12_s**2 + r23_s**2 + 2.0 * r12_s * r23_s * np.cos(delta)
        den_s = 1.0 + (r12_s**2) * (r23_s**2) + 2.0 * r12_s * r23_s * np.cos(delta)
        r_s = num_s / max(den_s, 1e-12)

        num_p = r12_p**2 + r23_p**2 + 2.0 * r12_p * r23_p * np.cos(delta)
        den_p = 1.0 + (r12_p**2) * (r23_p**2) + 2.0 * r12_p * r23_p * np.cos(delta)
        r_p = num_p / max(den_p, 1e-12)

        # Unpolarized light: average of s and p
        r_mean = 0.5 * (r_s + r_p)
        rgb_reflectance[i] = float(np.clip(r_mean, 0.0, 1.0))

    return rgb_reflectance


def evaluate_charlie_microfiber_sheen(
    normal: np.ndarray,
    view_dir: np.ndarray,
    light_dir: np.ndarray,
    sheen: MicrofiberSheenProperties,
) -> np.ndarray:
    """Evaluate Estevez-Kulla Charlie microfiber sheen NDF with soft-cloth visibility."""
    n = normal / np.linalg.norm(normal)
    v = view_dir / np.linalg.norm(view_dir)
    l_u = light_dir / np.linalg.norm(light_dir)

    n_dot_l = float(np.clip(np.dot(n, l_u), 0.0, 1.0))
    n_dot_v = float(np.clip(np.dot(n, v), 0.0, 1.0))

    if n_dot_l <= 1e-6 or n_dot_v <= 1e-6:
        return np.zeros(3, dtype=np.float64)

    h_norm = float(np.linalg.norm(l_u + v))
    h = (l_u + v) / h_norm if h_norm > 1e-12 else n
    n_dot_h = float(np.clip(np.dot(n, h), 0.0, 1.0))

    # sin(theta_h) = sqrt(1 - (N.H)^2)
    sin_theta_h = float(np.sqrt(max(1.0 - n_dot_h**2, 1e-6)))

    r = sheen.roughness
    inv_r = 1.0 / r

    # Charlie NDF: D(h) = ((2 + 1/r) * sin(theta_h)^(1/r)) / (2 * pi)
    d_charlie = ((2.0 + inv_r) * (sin_theta_h**inv_r)) / (2.0 * np.pi)

    # Neubelt & Pettineo soft-cloth visibility:
    # V_cloth = 1 / (4 * (N.L + N.V - N.L * N.V))
    v_cloth = 1.0 / (4.0 * max(n_dot_l + n_dot_v - n_dot_l * n_dot_v, 1e-7))

    f_sheen = d_charlie * v_cloth * sheen.sheen_color
    return cast(np.ndarray, f_sheen)


def evaluate_cloth_iridescent_sheen_brdf(
    normal: np.ndarray,
    view_dir: np.ndarray,
    light_dir: np.ndarray,
    film: ThinFilmProperties,
    sheen: MicrofiberSheenProperties,
    light_radiance: np.ndarray,
) -> ClothBRDFResult:
    """Evaluate combined iridescent thin-film reflectance and microfiber cloth sheen."""
    n = normal / np.linalg.norm(normal)
    v = view_dir / np.linalg.norm(view_dir)
    l_u = light_dir / np.linalg.norm(light_dir)

    n_dot_v = float(np.clip(np.dot(n, v), 0.0, 1.0))
    n_dot_l = float(np.clip(np.dot(n, l_u), 0.0, 1.0))

    theta_v = float(np.arccos(n_dot_v))
    airy_r = evaluate_airy_thin_film_reflectance(theta_v, film)

    sheen_brdf = evaluate_charlie_microfiber_sheen(n, v, l_u, sheen)
    sheen_rad = sheen_brdf * n_dot_l * light_radiance

    combined = (airy_r + sheen_brdf) * n_dot_l * light_radiance

    return ClothBRDFResult(
        airy_reflectance_rgb=airy_r,
        charlie_sheen_radiance=sheen_rad,
        combined_radiance=combined,
    )
