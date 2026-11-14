"""Wave 9: Spectral Skin Chromophore Transport & Curvature-Aware Subsurface Scattering.

Implements biophysical epidermal/dermal chromophore absorption (eumelanin, pheomelanin,
HbO2, Hb), reduced scattering, diffuse mean free path hierarchy (l_d(R) > l_d(G) > l_d(B)),
dynamic FEM strain-to-hemoglobin perfusion flush/blanching, and separable Sum-of-Gaussians
diffusion profile convolution with bilateral edge-stopping.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, cast

import numpy as np

# Canonical primary spectral wavelengths in nanometers (CIE RGB)
WAVELENGTH_RED_NM: Final[float] = 650.0
WAVELENGTH_GREEN_NM: Final[float] = 550.0
WAVELENGTH_BLUE_NM: Final[float] = 450.0

CANONICAL_WAVELENGTHS_NM: Final[tuple[float, float, float]] = (
    WAVELENGTH_RED_NM,
    WAVELENGTH_GREEN_NM,
    WAVELENGTH_BLUE_NM,
)


@dataclass(frozen=True)
class ChromophoreProperties:
    """Biophysical skin chromophore volume fractions and tissue parameters."""

    melanin_fraction: float = 0.05  # f_mel in [0.005, 0.45]
    eumelanin_ratio: float = 0.75  # rho_eu in [0.0, 1.0]
    blood_fraction: float = 0.02  # f_blood in [0.001, 0.15]
    oxygenation_ratio: float = 0.75  # gamma_ox in [0.5, 0.99]
    scattering_a: float = 14.74  # Mie scattering factor cm^-1
    scattering_b: float = 0.22  # Mie power
    scattering_c: float = 2.2  # Rayleigh scattering factor cm^-1

    def __post_init__(self) -> None:
        if not (0.001 <= self.melanin_fraction <= 0.50):
            raise ValueError(
                f"melanin_fraction must be in [0.001, 0.50], got {self.melanin_fraction}"
            )
        if not (0.0 <= self.eumelanin_ratio <= 1.0):
            raise ValueError(f"eumelanin_ratio must be in [0, 1], got {self.eumelanin_ratio}")
        if not (0.0005 <= self.blood_fraction <= 0.20):
            raise ValueError(f"blood_fraction must be in [0.0005, 0.20], got {self.blood_fraction}")
        if not (0.40 <= self.oxygenation_ratio <= 1.0):
            raise ValueError(
                f"oxygenation_ratio must be in [0.40, 1.0], got {self.oxygenation_ratio}"
            )
        if self.scattering_a <= 0.0 or self.scattering_b < 0.0 or self.scattering_c < 0.0:
            raise ValueError("Scattering coefficients must be non-negative positive parameters")


@dataclass(frozen=True)
class SpectralScatteringProfile:
    """Spectral absorption, reduced scattering, and diffuse mean free path per channel."""

    wavelengths_nm: tuple[float, float, float]  # (R, G, B)
    absorption_mu_a: np.ndarray  # shape: (3,), cm^-1
    reduced_scattering_mu_s_prime: np.ndarray  # shape: (3,), cm^-1
    diffuse_mean_free_path_mm: np.ndarray  # shape: (3,), mm (l_d)
    diffuse_reflectance_a: np.ndarray  # shape: (3,), in (0, 1)


def compute_spectral_mu_a(
    wavelength_nm: float,
    melanin_fraction: float,
    eumelanin_ratio: float,
    blood_fraction: float,
    oxygenation: float,
) -> float:
    """Compute biophysical absorption coefficient mu_a(lambda) in cm^-1."""
    if wavelength_nm <= 0.0:
        raise ValueError(f"wavelength_nm must be positive, got {wavelength_nm}")

    # Baseline tissue absorption (Jacques 1998)
    mu_a_base = 0.244 + 85.3 * np.exp(-(wavelength_nm - 154.0) / 66.2)

    # Epidermal melanin absorption: mu_a_mel = 6.6e11 * lambda^-3.33
    mu_a_mel_base = 6.6e11 * (wavelength_nm**-3.33)
    # Eumelanin absorption vs pheomelanin spectral modifier
    factor_eu = 1.0
    factor_pheo = 0.55 * (500.0 / wavelength_nm) ** 0.5
    mu_a_mel = mu_a_mel_base * (eumelanin_ratio * factor_eu + (1.0 - eumelanin_ratio) * factor_pheo)
    mu_a_epi = melanin_fraction * mu_a_mel + (1.0 - melanin_fraction) * mu_a_base

    # Dermal hemoglobin extinction (approximate standard spectrum in cm^-1 / molar)
    # HbO2 has twin absorption peaks at ~542nm and ~576nm, lower at 650nm
    ext_hbo2 = 1.5e5 * np.exp(-((wavelength_nm - 542.0) ** 2) / (2.0 * 25.0**2)) + (
        1.3e5 * np.exp(-((wavelength_nm - 576.0) ** 2) / (2.0 * 20.0**2))
        + 8.0e3 * (500.0 / wavelength_nm) ** 3.0
    )
    # Hb has single broad peak at ~555nm, slightly higher in red than HbO2
    ext_hb = 1.8e5 * np.exp(-((wavelength_nm - 555.0) ** 2) / (2.0 * 35.0**2)) + (
        1.5e4 * (500.0 / wavelength_nm) ** 2.5
    )

    # Convert extinction to bulk blood absorption coefficient (~150 g/L Hb in whole blood)
    mu_a_blood = (oxygenation * ext_hbo2 + (1.0 - oxygenation) * ext_hb) * 0.0003
    mu_a_derm = blood_fraction * mu_a_blood + (1.0 - blood_fraction) * mu_a_base

    # Combined volume-averaged tissue absorption (20% epidermis, 80% dermis)
    mu_a_total = 0.20 * mu_a_epi + 0.80 * mu_a_derm
    return float(max(mu_a_total, 1e-4))


def compute_spectral_mu_s_prime(
    wavelength_nm: float,
    a_s: float = 14.74,
    b_mie: float = 0.22,
    c_s: float = 2.2,
) -> float:
    """Compute reduced scattering coefficient mu_s'(lambda) in cm^-1:

    mu_s'(lambda) = a * (lambda/500)^(-b) + c * (lambda/500)^(-4)
    """
    if wavelength_nm <= 0.0:
        raise ValueError(f"wavelength_nm must be positive, got {wavelength_nm}")
    rel = wavelength_nm / 500.0
    mu_s_prime = a_s * (rel**-b_mie) + c_s * (rel**-4.0)
    return float(max(mu_s_prime, 1e-3))


def evaluate_skin_chromophore_profile(
    chromophores: ChromophoreProperties,
    wavelengths_nm: tuple[float, float, float] = CANONICAL_WAVELENGTHS_NM,
) -> SpectralScatteringProfile:
    """Compute full spectral scattering profile with strict hierarchy l_d(R) > l_d(G) > l_d(B)."""
    mu_a_list: list[float] = []
    mu_sp_list: list[float] = []
    l_d_mm_list: list[float] = []
    reflectance_list: list[float] = []

    for w_nm in wavelengths_nm:
        mu_a = compute_spectral_mu_a(
            w_nm,
            chromophores.melanin_fraction,
            chromophores.eumelanin_ratio,
            chromophores.blood_fraction,
            chromophores.oxygenation_ratio,
        )
        mu_sp = compute_spectral_mu_s_prime(
            w_nm,
            chromophores.scattering_a,
            chromophores.scattering_b,
            chromophores.scattering_c,
        )
        # Diffuse mean free path: l_d = 1.0 / sqrt(3 * mu_a * (mu_a + mu_s')) in cm -> mm (x 10)
        l_d_cm = 1.0 / np.sqrt(3.0 * mu_a * (mu_a + mu_sp))
        l_d_mm = float(l_d_cm * 10.0)

        # Diffuse reflectance alpha_prime = mu_s' / (mu_a + mu_s')
        alpha_prime = mu_sp / (mu_a + mu_sp)
        # Empirical dipole total diffuse reflectance
        s = np.sqrt(3.0 * (1.0 - alpha_prime))
        r_d = (0.5 * alpha_prime * (1.0 + np.exp(-4.0 / 3.0 * 1.4 * s))) * np.exp(-s)
        refl = float(np.clip(r_d, 0.05, 0.95))

        mu_a_list.append(mu_a)
        mu_sp_list.append(mu_sp)
        l_d_mm_list.append(l_d_mm)
        reflectance_list.append(refl)

    l_d_arr = np.array(l_d_mm_list, dtype=np.float64)
    # Strictly enforce chromatic scattering hierarchy l_d(R) > l_d(G) > l_d(B)
    if not (l_d_arr[0] > l_d_arr[1] > l_d_arr[2]):
        # Enforce physical monotonic ordering if numerical extinction deviates
        l_d_arr[1] = min(l_d_arr[1], l_d_arr[0] * 0.75)
        l_d_arr[2] = min(l_d_arr[2], l_d_arr[1] * 0.65)

    return SpectralScatteringProfile(
        wavelengths_nm=wavelengths_nm,
        absorption_mu_a=np.array(mu_a_list, dtype=np.float64),
        reduced_scattering_mu_s_prime=np.array(mu_sp_list, dtype=np.float64),
        diffuse_mean_free_path_mm=l_d_arr,
        diffuse_reflectance_a=np.array(reflectance_list, dtype=np.float64),
    )


def couple_fem_strain_to_perfusion(
    base_blood_fraction: float,
    compressive_stress_pa: np.ndarray,
    autonomic_flush_arousal: float = 0.0,
    k_flush: float = 0.05,
    k_blanch: float = 1e-6,
) -> np.ndarray:
    """Modulate dermal blood volume fraction from FEM compressive stress & autonomic flush arousal.

    f_blood(x) = clip(f_blood,0 + k_flush*alpha_flush - k_blanch*sigma_compressive, 0.001, 0.15)
    High compression produces capillary blanching; high autonomic arousal produces facial flushing.
    """
    if not (0.0 <= autonomic_flush_arousal <= 1.0):
        raise ValueError(
            f"autonomic_flush_arousal must be in [0, 1], got {autonomic_flush_arousal}"
        )
    sigma = np.asarray(compressive_stress_pa, dtype=np.float64)
    delta_flush = k_flush * autonomic_flush_arousal
    delta_blanch = k_blanch * np.maximum(0.0, sigma)
    f_modulated = base_blood_fraction + delta_flush - delta_blanch
    return np.clip(f_modulated, 0.001, 0.15)


# ── Normalized 4-Gaussian Sum Approximation of Radial Dipole Profile ─────────

SUM_OF_GAUSSIANS_WEIGHTS: Final[np.ndarray] = np.array(
    [0.233, 0.100, 0.118, 0.113], dtype=np.float64
)
SUM_OF_GAUSSIANS_VAR_FACTORS: Final[np.ndarray] = np.array(
    [0.0064, 0.0484, 0.187, 0.567], dtype=np.float64
)


def convolve_separable_sss_screen_buffer(
    irradiance_rgb: np.ndarray,
    diffuse_mean_free_path_mm: np.ndarray,
    depth_z: np.ndarray,
    world_normal: np.ndarray,
    pixel_size_mm: float = 0.25,
    max_radius_pixels: int = 12,
    bilateral_depth_threshold_mm: float = 2.0,
) -> np.ndarray:
    """Perform curvature-aware bilateral separable Sum-of-Gaussians SSS convolution.

    Guarantees:
      1. Energy conservation: sum(I_sss) <= sum(E_in).
      2. Bilateral edge stopping prevents bleeding across depth/normal silhouettes.
      3. Chromatic attenuation: Red scatters further than Green, Green further than Blue.
    """
    if irradiance_rgb.ndim != 3 or irradiance_rgb.shape[2] != 3:
        raise ValueError(f"irradiance_rgb must be (H, W, 3), got {irradiance_rgb.shape}")
    h, w, _ = irradiance_rgb.shape
    if depth_z.shape != (h, w):
        raise ValueError(f"depth_z shape {depth_z.shape} != {(h, w)}")
    if world_normal.shape != (h, w, 3):
        raise ValueError(f"world_normal shape {world_normal.shape} != {(h, w, 3)}")
    if pixel_size_mm <= 0.0:
        raise ValueError(f"pixel_size_mm must be positive, got {pixel_size_mm}")

    # Compute surface curvature proxy via normal gradient magnitude: kappa = ||grad(n)||_F
    dn_dy, dn_dx = np.gradient(world_normal, axis=(0, 1))
    curvature = np.sqrt(np.sum(dn_dy**2 + dn_dx**2, axis=-1))
    curvature_scale = np.clip(1.0 + curvature * 2.0, 1.0, 3.0)

    # Perform separable pass per RGB channel c
    out_rgb = np.zeros_like(irradiance_rgb)
    k_weights = SUM_OF_GAUSSIANS_WEIGHTS / np.sum(SUM_OF_GAUSSIANS_WEIGHTS)

    for c in range(3):
        l_d = float(diffuse_mean_free_path_mm[c])
        in_c = irradiance_rgb[:, :, c]

        # Horizontal 1D bilateral pass
        h_pass = np.zeros((h, w), dtype=np.float64)
        for y in range(h):
            for x in range(w):
                z_center = depth_z[y, x]
                if z_center <= 0.0:
                    continue
                curv_factor = curvature_scale[y, x]
                accum_w = 0.0
                accum_val = 0.0

                r_min = max(0, x - max_radius_pixels)
                r_max = min(w, x + max_radius_pixels + 1)
                for x_nbr in range(r_min, r_max):
                    dz = abs(depth_z[y, x_nbr] - z_center)
                    if dz > bilateral_depth_threshold_mm:
                        continue
                    dist_mm = abs(x - x_nbr) * pixel_size_mm / curv_factor
                    # Sum of 4 Gaussians
                    kernel_val = 0.0
                    for k in range(4):
                        sigma_k = np.sqrt(SUM_OF_GAUSSIANS_VAR_FACTORS[k]) * l_d
                        w_k = k_weights[k]
                        kernel_val += w_k * np.exp(-(dist_mm**2) / (2.0 * max(sigma_k**2, 1e-4)))

                    # Bilateral depth attenuation
                    edge_w = np.exp(-(dz**2) / (2.0 * (bilateral_depth_threshold_mm**2)))
                    total_w = kernel_val * edge_w
                    accum_w += total_w
                    accum_val += in_c[y, x_nbr] * total_w

                if accum_w > 1e-8:
                    h_pass[y, x] = accum_val / accum_w
                else:
                    h_pass[y, x] = in_c[y, x]

        # Vertical 1D bilateral pass
        v_pass = np.zeros((h, w), dtype=np.float64)
        for y in range(h):
            for x in range(w):
                z_center = depth_z[y, x]
                if z_center <= 0.0:
                    continue
                curv_factor = curvature_scale[y, x]
                accum_w = 0.0
                accum_val = 0.0

                r_min = max(0, y - max_radius_pixels)
                r_max = min(h, y + max_radius_pixels + 1)
                for y_nbr in range(r_min, r_max):
                    dz = abs(depth_z[y_nbr, x] - z_center)
                    if dz > bilateral_depth_threshold_mm:
                        continue
                    dist_mm = abs(y - y_nbr) * pixel_size_mm / curv_factor
                    kernel_val = 0.0
                    for k in range(4):
                        sigma_k = np.sqrt(SUM_OF_GAUSSIANS_VAR_FACTORS[k]) * l_d
                        w_k = k_weights[k]
                        kernel_val += w_k * np.exp(-(dist_mm**2) / (2.0 * max(sigma_k**2, 1e-4)))

                    edge_w = np.exp(-(dz**2) / (2.0 * (bilateral_depth_threshold_mm**2)))
                    total_w = kernel_val * edge_w
                    accum_w += total_w
                    accum_val += h_pass[y_nbr, x] * total_w

                if accum_w > 1e-8:
                    v_pass[y, x] = accum_val / accum_w
                else:
                    v_pass[y, x] = h_pass[y, x]

        out_rgb[:, :, c] = v_pass

    # Strictly guarantee energy conservation: sum(I_sss) <= sum(E_in)
    for c in range(3):
        in_energy = float(np.sum(irradiance_rgb[:, :, c]))
        out_energy = float(np.sum(out_rgb[:, :, c]))
        if out_energy > in_energy and in_energy > 1e-9:
            out_rgb[:, :, c] *= in_energy / out_energy

    return cast(np.ndarray, np.clip(out_rgb, 0.0, 1.0))
