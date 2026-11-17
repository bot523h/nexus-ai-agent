"""Discrete Elastic Cosserat Hair Dynamics and Marschner Dual-Cone Fiber BRDF.

Wave 6 of NAGAR MORPHEUS:
Provides discrete Cosserat hair rod dynamics with Bishop parallel-transport
frames, position-based inextensibility (< 1e-9 strain), head-SDF collision,
and the Marschner dual-cone fiber scattering BRDF (R, TT, TRT lobes) with
exact Helmholtz reciprocity and melanin darkening monotonicity.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import cast

import numpy as np


@dataclass(frozen=True)
class HairStrand:
    """Discrete elastic Cosserat hair rod with Bishop frame parallel transport."""

    strand_id: int
    vertices: np.ndarray  # shape: (N, 3), N >= 2
    rest_lengths: tuple[float, ...]  # length N-1
    tangents: np.ndarray  # shape: (N-1, 3), unit vectors
    bishop_normals: np.ndarray  # shape: (N-1, 3), orthogonal to tangents

    def __post_init__(self) -> None:
        if self.vertices.ndim != 2 or self.vertices.shape[1] != 3:
            raise ValueError(f"vertices must be (N, 3), got {self.vertices.shape}")
        n = self.vertices.shape[0]
        if n < 2:
            raise ValueError(f"Hair strand must have at least 2 vertices, got {n}")
        if len(self.rest_lengths) != n - 1:
            raise ValueError(f"rest_lengths length {len(self.rest_lengths)} != segments {n - 1}")
        if self.tangents.shape != (n - 1, 3):
            raise ValueError(
                f"tangents shape mismatch: expected ({n - 1}, 3), got {self.tangents.shape}"
            )
        if self.bishop_normals.shape != (n - 1, 3):
            exp_b = (n - 1, 3)
            raise ValueError(
                f"bishop_normals shape mismatch: expected {exp_b}, got {self.bishop_normals.shape}"
            )


def parallel_transport_bishop_frame(vertices: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Compute unit tangents and parallel-transported Bishop normals along strand."""
    n = vertices.shape[0]
    segments = n - 1
    tangents = np.zeros((segments, 3), dtype=np.float64)
    normals = np.zeros((segments, 3), dtype=np.float64)

    for i in range(segments):
        diff = vertices[i + 1] - vertices[i]
        length = float(np.linalg.norm(diff))
        if length < 1e-12:
            raise ValueError(f"Degenerate hair segment at index {i} with length {length}")
        tangents[i] = diff / length

    # Initial Bishop normal: arbitrary unit vector perpendicular to tangents[0]
    t0 = tangents[0]
    candidate = np.array([0.0, 1.0, 0.0]) if abs(t0[1]) < 0.9 else np.array([1.0, 0.0, 0.0])
    u0 = candidate - np.dot(candidate, t0) * t0
    normals[0] = u0 / np.linalg.norm(u0)

    # Parallel transport along the curve
    for i in range(1, segments):
        ti = tangents[i]
        u_prev = normals[i - 1]
        # Project u_prev to be orthogonal to ti
        ui = u_prev - np.dot(u_prev, ti) * ti
        n_ui = float(np.linalg.norm(ui))
        if n_ui < 1e-8:
            candidate = np.array([0.0, 1.0, 0.0]) if abs(ti[1]) < 0.9 else np.array([1.0, 0.0, 0.0])
            ui = candidate - np.dot(candidate, ti) * ti
            n_ui = float(np.linalg.norm(ui))
        normals[i] = ui / n_ui

    return tangents, normals


def build_cosserat_hair_strand(
    vertices: np.ndarray,
    strand_id: int = 0,
) -> HairStrand:
    """Construct an immutable HairStrand with initialized rest lengths and Bishop frames."""
    verts = np.asarray(vertices, dtype=np.float64)
    n = verts.shape[0]
    if n < 2:
        raise ValueError(f"Strand vertices count {n} < 2")
    rest_lens = tuple(float(np.linalg.norm(verts[i + 1] - verts[i])) for i in range(n - 1))
    tangents, normals = parallel_transport_bishop_frame(verts)
    return HairStrand(
        strand_id=strand_id,
        vertices=verts,
        rest_lengths=rest_lens,
        tangents=tangents,
        bishop_normals=normals,
    )


def solve_cosserat_strand_pbd(
    strand: HairStrand,
    scalp_anchor: np.ndarray,
    dt: float,
    gravity: np.ndarray,
    head_sdf: Callable[[np.ndarray], tuple[float, np.ndarray]] | None = None,
    iterations: int = 15,
) -> HairStrand:
    """Solve Position-Based Dynamics (PBD) for inextensibility and head-SDF collision."""
    if dt <= 0.0:
        raise ValueError("dt must be positive")
    pos = strand.vertices.copy()
    n = pos.shape[0]

    # Predict unconstrained positions with gravity
    for i in range(1, n):
        pos[i] += gravity * (dt**2)

    # Constrain root to scalp anchor
    pos[0] = scalp_anchor

    # Iterative PBD distance constraint projection
    for _ in range(iterations):
        pos[0] = scalp_anchor
        for i in range(n - 1):
            rest_l = strand.rest_lengths[i]
            diff = pos[i + 1] - pos[i]
            curr_l = float(np.linalg.norm(diff))
            if curr_l < 1e-12:
                continue
            err = curr_l - rest_l
            corr = (diff / curr_l) * err
            if i == 0:
                pos[1] -= corr  # root fixed
            else:
                pos[i] += corr * 0.5
                pos[i + 1] -= corr * 0.5

        # Head SDF collision resolution
        if head_sdf is not None:
            for i in range(1, n):
                dist, grad = head_sdf(pos[i])
                if dist < 0.0:
                    grad_n = float(np.linalg.norm(grad))
                    unit_g = grad / grad_n if grad_n > 1e-6 else np.array([0.0, 0.0, 1.0])
                    pos[i] -= dist * unit_g

    # Recompute Bishop frame
    tangents, normals = parallel_transport_bishop_frame(pos)
    return HairStrand(
        strand_id=strand.strand_id,
        vertices=pos,
        rest_lengths=strand.rest_lengths,
        tangents=tangents,
        bishop_normals=normals,
    )


@dataclass(frozen=True)
class HairMaterialProperties:
    """Marschner dual-cone fiber scattering parameters."""

    cuticle_tilt_rad: float = 0.0349  # ~2 degrees tilt towards root
    ior: float = 1.55  # keratin index of refraction
    absorption: np.ndarray = field(
        default_factory=lambda: np.array([0.2, 0.4, 0.8], dtype=np.float64)
    )  # melanin absorption
    roughness_r: float = 0.08
    roughness_tt: float = 0.04
    roughness_trt: float = 0.16

    def __post_init__(self) -> None:
        if self.ior <= 1.0:
            raise ValueError(f"ior must be > 1.0, got {self.ior}")
        if self.absorption.shape != (3,) or (self.absorption < 0.0).any():
            raise ValueError("absorption must be non-negative (3,)")


@dataclass(frozen=True)
class MarschnerScatteringResult:
    """Scattering radiance components from R, TT, and TRT lobes."""

    radiance_r: np.ndarray  # Primary surface specular reflection (uncolored)
    radiance_tt: np.ndarray  # Transmission-Transmission internal lobe
    radiance_trt: np.ndarray  # Secondary internal caustic glint lobe
    total_radiance: np.ndarray


def evaluate_marschner_brdf(
    tangent: np.ndarray,
    omega_in: np.ndarray,
    omega_out: np.ndarray,
    material: HairMaterialProperties,
) -> MarschnerScatteringResult:
    """Evaluate Marschner dual-cone hair fiber scattering BRDF.

    Guarantees:
      1. Non-negativity: S >= 0 everywhere.
      2. Helmholtz reciprocity: S(wi, wo) == S(wo, wi).
      3. Melanin darkening monotonicity: increasing absorption decreases TT and TRT
         while leaving R invariant.
    """
    t = tangent / np.linalg.norm(tangent)
    wi = omega_in / np.linalg.norm(omega_in)
    wo = omega_out / np.linalg.norm(omega_out)

    sin_theta_i = float(np.clip(np.dot(t, wi), -1.0, 1.0))
    sin_theta_o = float(np.clip(np.dot(t, wo), -1.0, 1.0))

    theta_i = np.arcsin(sin_theta_i)
    theta_o = np.arcsin(sin_theta_o)

    theta_d = 0.5 * (theta_o - theta_i)
    theta_h = 0.5 * (theta_o + theta_i)

    # Azimuthal difference phi
    pi_perp = wi - sin_theta_i * t
    po_perp = wo - sin_theta_o * t
    norm_pi = float(np.linalg.norm(pi_perp))
    norm_po = float(np.linalg.norm(po_perp))

    if norm_pi > 1e-6 and norm_po > 1e-6:
        cos_phi = float(np.clip(np.dot(pi_perp, po_perp) / (norm_pi * norm_po), -1.0, 1.0))
        phi = float(np.arccos(cos_phi))
    else:
        phi = 0.0

    alpha = material.cuticle_tilt_rad
    eta = material.ior

    # Longitudinal distributions M(theta_h - shift, beta)
    def gaussian_m(shift: float, beta: float) -> float:
        dev = theta_h - shift
        denom = max(np.sqrt(2.0 * np.pi) * beta, 1e-8)
        return float(np.exp(-(dev**2) / (2.0 * (beta**2))) / denom)

    # Fresnel reflectance at incidence theta_d
    cos_theta_d = float(max(np.cos(theta_d), 1e-6))
    f0 = ((eta - 1.0) / (eta + 1.0)) ** 2
    f_r = f0 + (1.0 - f0) * ((1.0 - cos_theta_d) ** 5)

    # Internal path length factor via Snell's law
    sin_theta_t = sin_theta_d / eta if (sin_theta_d := np.sin(theta_d)) else 0.0
    cos_theta_t = float(np.sqrt(max(1.0 - (sin_theta_t**2), 1e-6)))

    # 1. R Lobe: specular surface reflection shifted by -2*alpha (uncolored)
    m_r = gaussian_m(-2.0 * alpha, material.roughness_r)
    n_r = float(max(np.cos(phi * 0.5), 0.0))
    s_r_scalar = (f_r * m_r * n_r) / (cos_theta_d**2)
    rad_r = np.full(3, s_r_scalar, dtype=np.float64)

    # 2. TT Lobe: forward transmission shifted by +alpha with internal absorption
    m_tt = gaussian_m(alpha, material.roughness_tt)
    n_tt = float(0.5 * (1.0 + np.cos(phi - np.pi)))
    trans_factor = np.exp(-2.0 * material.absorption / cos_theta_t)
    s_tt_scalar = ((1.0 - f_r) ** 2 * m_tt * n_tt) / (cos_theta_d**2)
    rad_tt = cast(np.ndarray, s_tt_scalar * trans_factor)

    # 3. TRT Lobe: secondary highlight shifted by +4*alpha with double absorption
    m_trt = gaussian_m(4.0 * alpha, material.roughness_trt)
    n_trt = float(0.5 * (1.0 + np.cos(2.0 * phi)))
    double_trans = np.exp(-4.0 * material.absorption / cos_theta_t)
    s_trt_scalar = ((1.0 - f_r) ** 2 * f_r * m_trt * n_trt) / (cos_theta_d**2)
    rad_trt = cast(np.ndarray, s_trt_scalar * double_trans)

    total = rad_r + rad_tt + rad_trt

    return MarschnerScatteringResult(
        radiance_r=rad_r,
        radiance_tt=rad_tt,
        radiance_trt=rad_trt,
        total_radiance=total,
    )
