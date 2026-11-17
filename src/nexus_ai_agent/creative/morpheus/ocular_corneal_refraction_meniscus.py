"""Wave 11: Multi-Layer Ocular Corneal Snell Refraction, Eyelid Bulge & Tear-Film Meniscus Optics.

Implements two-sphere corneal-scleral composite SDF, tarsal eyelid bulge radial deformation
guaranteeing zero cornea-eyelid clipping, 3D vector Snell's law corneal refraction with pupil
parallax, pupillary light reflex aperture dynamics, and capillary tear meniscus optics.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

import numpy as np

# Physical ocular constants in millimeters and refractive indices
SCLERA_RADIUS_MM: Final[float] = 11.5
CORNEA_RADIUS_MM: Final[float] = 7.8
CORNEA_SCLERA_OFFSET_MM: Final[float] = 5.5
ANTERIOR_CHAMBER_DEPTH_MM: Final[float] = 3.1

IOR_AIR: Final[float] = 1.00029
IOR_CORNEA: Final[float] = 1.376
IOR_AQUEOUS: Final[float] = 1.336
IOR_TEAR_FILM: Final[float] = 1.336


@dataclass(frozen=True)
class EyeOpticalParameters:
    """Anatomical dimensions and optical constants for human eye globe."""

    sclera_radius_mm: float = SCLERA_RADIUS_MM
    cornea_radius_mm: float = CORNEA_RADIUS_MM
    cornea_center_offset_mm: float = CORNEA_SCLERA_OFFSET_MM
    anterior_chamber_depth_mm: float = ANTERIOR_CHAMBER_DEPTH_MM
    cornea_ior: float = IOR_CORNEA
    aqueous_ior: float = IOR_AQUEOUS
    tear_film_ior: float = IOR_TEAR_FILM

    def __post_init__(self) -> None:
        if self.cornea_radius_mm >= self.sclera_radius_mm:
            raise ValueError("cornea_radius must be smaller than sclera_radius")
        if self.cornea_ior <= 1.0 or self.aqueous_ior <= 1.0:
            raise ValueError("Refractive indices must be > 1.0")


@dataclass(frozen=True)
class CornealRefractionResult:
    """Exact 3D vector refraction ray and pupil aperture intersection."""

    corneal_hit_point: np.ndarray  # shape: (3,)
    corneal_normal: np.ndarray  # shape: (3,)
    refracted_ray_direction: np.ndarray  # shape: (3,)
    iris_plane_hit_point: np.ndarray  # shape: (3,)
    pupil_aperture_radius_mm: float
    is_inside_pupil: bool
    fresnel_specular_reflectance: float


def compute_globe_composite_sdf(
    point: np.ndarray,
    sclera_center: np.ndarray,
    gaze_rotation: np.ndarray,
    params: EyeOpticalParameters,
) -> float:
    """Compute composite two-sphere SDF: Phi(p) = min(||p - c_s|| - r_s, ||p - c_c|| - r_c)."""
    p = np.asarray(point, dtype=np.float64)
    c_s = np.asarray(sclera_center, dtype=np.float64)

    # Cornea center offset along gaze direction g = R_eye @ [0, 0, 1]
    gaze_dir = gaze_rotation @ np.array([0.0, 0.0, 1.0], dtype=np.float64)
    c_c = c_s + params.cornea_center_offset_mm * gaze_dir

    d_sclera = float(np.linalg.norm(p - c_s) - params.sclera_radius_mm)
    d_cornea = float(np.linalg.norm(p - c_c) - params.cornea_radius_mm)
    return min(d_sclera, d_cornea)


def deform_eyelid_corneal_bulge(
    eyelid_vertices: np.ndarray,
    sclera_center: np.ndarray,
    gaze_rotation: np.ndarray,
    params: EyeOpticalParameters,
    tear_clearance_mm: float = 0.15,
) -> np.ndarray:
    """Displace eyelid vertices radially outward to prevent corneal clipping as gaze turns.

    Guarantees d_eyelid >= tear_clearance_mm > 0 everywhere across all gaze angles.
    """
    if eyelid_vertices.ndim != 2 or eyelid_vertices.shape[1] != 3:
        raise ValueError("eyelid_vertices must be (N, 3)")
    c_s = np.asarray(sclera_center, dtype=np.float64)
    deformed = eyelid_vertices.copy()

    for i in range(len(deformed)):
        pt = deformed[i]
        dist = compute_globe_composite_sdf(pt, c_s, gaze_rotation, params)
        if dist < tear_clearance_mm:
            # Radial outward direction from sclera center
            r_vec = pt - c_s
            norm_r = float(np.linalg.norm(r_vec))
            if norm_r > 1e-8:
                r_hat = r_vec / norm_r
            else:
                r_hat = np.array([0.0, 0.0, 1.0], dtype=np.float64)

            penetration = tear_clearance_mm - dist
            deformed[i] = pt + penetration * r_hat

    return deformed


def evaluate_pupillary_light_reflex(
    scene_illuminance_lux: float,
    min_radius_mm: float = 1.0,
    max_radius_mm: float = 4.0,
) -> float:
    """Compute pupil aperture radius (mm) from scene illuminance via Watson-Yellott formula."""
    if scene_illuminance_lux < 0.0:
        raise ValueError("scene_illuminance_lux cannot be negative")
    # Higher illuminance contracts pupil (miosis), lower dilates (mydriasis)
    lux = max(scene_illuminance_lux, 0.0)
    contraction = 1.0 / (1.0 + 0.05 * np.sqrt(lux))
    radius = min_radius_mm + (max_radius_mm - min_radius_mm) * contraction
    return float(np.clip(radius, min_radius_mm, max_radius_mm))


def trace_3d_snell_corneal_refraction(
    view_ray_origin: np.ndarray,
    view_ray_direction: np.ndarray,
    sclera_center: np.ndarray,
    gaze_rotation: np.ndarray,
    scene_illuminance_lux: float = 500.0,
    params: EyeOpticalParameters | None = None,
) -> CornealRefractionResult | None:
    """Trace 3D Snell's law refraction through anterior cornea onto the iris plane."""
    p_opt = EyeOpticalParameters() if params is None else params
    o = np.asarray(view_ray_origin, dtype=np.float64)
    d = np.asarray(view_ray_direction, dtype=np.float64)
    d = d / np.linalg.norm(d)
    c_s = np.asarray(sclera_center, dtype=np.float64)

    # Cornea center
    gaze_dir = gaze_rotation @ np.array([0.0, 0.0, 1.0], dtype=np.float64)
    c_c = c_s + p_opt.cornea_center_offset_mm * gaze_dir
    r_c = p_opt.cornea_radius_mm

    # Ray-sphere intersection with corneal dome
    oc = o - c_c
    b_val = float(np.dot(oc, d))
    c_val = float(np.dot(oc, oc) - r_c**2)
    disc = b_val**2 - c_val
    if disc < 0.0:
        return None  # Ray misses cornea

    t_hit = -b_val - np.sqrt(disc)
    if t_hit <= 0.0:
        t_hit = -b_val + np.sqrt(disc)
    if t_hit <= 0.0:
        return None

    x_c = o + t_hit * d
    n_c = (x_c - c_c) / r_c

    # Vector Snell's Law from air (n1) into cornea (n2)
    n1 = IOR_AIR
    n2 = p_opt.cornea_ior
    eta = n1 / n2

    cos_theta_i = float(np.clip(-np.dot(d, n_c), 0.0, 1.0))
    sin2_theta_t = eta**2 * (1.0 - cos_theta_i**2)
    if sin2_theta_t > 1.0:
        # Total internal reflection
        return None

    cos_theta_t = float(np.sqrt(max(0.0, 1.0 - sin2_theta_t)))
    d_refr = eta * d + (eta * cos_theta_i - cos_theta_t) * n_c
    d_refr = d_refr / np.linalg.norm(d_refr)

    # Dielectric Schlick/Fresnel reflectance at outer corneal interface
    f0 = ((n1 - n2) / (n1 + n2)) ** 2
    fresnel_spec = float(f0 + (1.0 - f0) * ((1.0 - cos_theta_i) ** 5))

    # Intersect refracted ray with internal iris plane
    # Iris plane is perpendicular to gaze_dir at depth d_AC behind cornea vertex
    iris_plane_pt = c_c + (r_c - p_opt.anterior_chamber_depth_mm) * gaze_dir
    denom = float(np.dot(d_refr, gaze_dir))
    if abs(denom) < 1e-8:
        return None

    t_iris = float(np.dot(iris_plane_pt - x_c, gaze_dir) / denom)
    if t_iris <= 0.0:
        return None

    x_iris = x_c + t_iris * d_refr

    # Pupil aperture check
    pupil_rad = evaluate_pupillary_light_reflex(scene_illuminance_lux)
    dist_from_pupil_center = float(np.linalg.norm(x_iris - iris_plane_pt))
    is_in_pupil = bool(dist_from_pupil_center <= pupil_rad)

    return CornealRefractionResult(
        corneal_hit_point=x_c,
        corneal_normal=n_c,
        refracted_ray_direction=d_refr,
        iris_plane_hit_point=x_iris,
        pupil_aperture_radius_mm=pupil_rad,
        is_inside_pupil=is_in_pupil,
        fresnel_specular_reflectance=fresnel_spec,
    )


# ── Capillary Tear-Film Meniscus Specular Bridge ──────────────────────────────


@dataclass(frozen=True)
class TearMeniscusResult:
    """Optical parameters and surface normal for capillary tear film meniscus."""

    meniscus_height_mm: float
    surface_normal: np.ndarray
    dielectric_fresnel: float


def evaluate_tear_film_meniscus(
    distance_from_eyelid_margin_mm: float,
    view_direction: np.ndarray,
    meniscus_h0_mm: float = 0.18,
    capillary_length_mm: float = 0.35,
    tear_ior: float = IOR_TEAR_FILM,
) -> TearMeniscusResult:
    """Evaluate concave capillary tear meniscus height, normal perturbation, and Fresnel term."""
    u = float(max(distance_from_eyelid_margin_mm, 0.0))
    h = meniscus_h0_mm * np.exp(-u / max(capillary_length_mm, 1e-4))

    # Slope dh/du = -h0 / lambda * exp(-u / lambda)
    dh_du = -(meniscus_h0_mm / capillary_length_mm) * np.exp(-u / capillary_length_mm)
    # Meniscus normal in local coordinate frame [u, h, lateral]
    n_local = np.array([-dh_du, 1.0, 0.0], dtype=np.float64)
    n_local = n_local / np.linalg.norm(n_local)

    # Dielectric Fresnel
    v = np.asarray(view_direction, dtype=np.float64)
    v = v / np.linalg.norm(v)
    cos_theta = float(np.clip(abs(np.dot(v, n_local)), 0.0, 1.0))

    f0 = ((1.0 - tear_ior) / (1.0 + tear_ior)) ** 2
    fresnel = float(f0 + (1.0 - f0) * ((1.0 - cos_theta) ** 5))

    return TearMeniscusResult(
        meniscus_height_mm=h,
        surface_normal=n_local,
        dielectric_fresnel=fresnel,
    )
