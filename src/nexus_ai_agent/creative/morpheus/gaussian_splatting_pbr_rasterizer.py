"""Deterministic 3D Anisotropic Gaussian Splatting and Cook-Torrance GGX PBR.

Wave 3 of NAGAR MORPHEUS:
Implements 3D Anisotropic Gaussian primitives, EWA perspective splatting with
regularized 2D screen covariance, Cook-Torrance GGX microfacet relighting with
Order-2 Spherical Harmonics, and depth-sorted front-to-back alpha compositing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

import numpy as np

from nexus_ai_agent.creative.morpheus.dual_quaternion_skeletal_rig import UnitQuaternion


@dataclass(frozen=True)
class Gaussian3D:
    """3D Anisotropic Gaussian primitive with physical PBR surface attributes."""

    mean: np.ndarray  # shape: (3,)
    rotation: UnitQuaternion
    scale: np.ndarray  # shape: (3,), strictly positive
    opacity: float  # [0.0, 1.0]
    normal: np.ndarray  # shape: (3,), unit vector
    albedo: np.ndarray  # shape: (3,), in [0.0, 1.0]
    roughness: float  # in [0.04, 1.0]
    metallic: float  # in [0.0, 1.0]
    sh_coefficients: np.ndarray | None = None  # shape: (9, 3)

    def __post_init__(self) -> None:
        if self.mean.shape != (3,):
            raise ValueError(f"mean must be (3,), got {self.mean.shape}")
        if self.scale.shape != (3,) or (self.scale <= 0.0).any():
            raise ValueError(f"scale must be (3,) with positive entries, got {self.scale}")
        if not (0.0 <= self.opacity <= 1.0):
            raise ValueError(f"opacity must be in [0, 1], got {self.opacity}")
        if self.normal.shape != (3,):
            raise ValueError(f"normal must be (3,), got {self.normal.shape}")
        norm_len = float(np.linalg.norm(self.normal))
        if abs(norm_len - 1.0) > 1e-4:
            raise ValueError(f"normal must be unit length, got {norm_len}")
        if self.albedo.shape != (3,) or (self.albedo < 0.0).any() or (self.albedo > 1.0).any():
            raise ValueError(f"albedo entries must be in [0, 1], got {self.albedo}")
        if not (0.04 <= self.roughness <= 1.0):
            raise ValueError(f"roughness must be in [0.04, 1.0], got {self.roughness}")
        if not (0.0 <= self.metallic <= 1.0):
            raise ValueError(f"metallic must be in [0, 1], got {self.metallic}")
        if self.sh_coefficients is not None and self.sh_coefficients.shape != (9, 3):
            raise ValueError(f"sh_coefficients must be (9, 3), got {self.sh_coefficients.shape}")


@dataclass(frozen=True)
class CameraParameters:
    """Pinhole perspective camera model with near/far depth planes."""

    view_matrix: np.ndarray  # shape: (4, 4), world-to-camera
    fx: float
    fy: float
    cx: float
    cy: float
    width: int
    height: int
    near_plane: float = 0.1
    far_plane: float = 100.0

    def __post_init__(self) -> None:
        if self.view_matrix.shape != (4, 4):
            raise ValueError(f"view_matrix must be (4, 4), got {self.view_matrix.shape}")
        if self.fx <= 0.0 or self.fy <= 0.0:
            raise ValueError("Focal lengths fx and fy must be positive")
        if self.width <= 0 or self.height <= 0:
            raise ValueError("Image dimensions width and height must be positive")
        if self.near_plane <= 0.0 or self.far_plane <= self.near_plane:
            raise ValueError("Invalid depth planes: near > 0 and far > near")


@dataclass(frozen=True)
class DirectionalLight:
    """Infinite directional light source."""

    direction: np.ndarray  # shape: (3,), points towards light, unit vector
    radiance: np.ndarray  # shape: (3,), non-negative RGB

    def __post_init__(self) -> None:
        if self.direction.shape != (3,):
            raise ValueError("direction must be (3,)")
        n = float(np.linalg.norm(self.direction))
        if abs(n - 1.0) > 1e-4:
            raise ValueError(f"direction must be unit length, got {n}")
        if self.radiance.shape != (3,) or (self.radiance < 0.0).any():
            raise ValueError("radiance must be non-negative (3,)")


@dataclass(frozen=True)
class PointLight:
    """Finite point light source."""

    position: np.ndarray  # shape: (3,)
    intensity: np.ndarray  # shape: (3,), non-negative

    def __post_init__(self) -> None:
        if self.position.shape != (3,):
            raise ValueError("position must be (3,)")
        if self.intensity.shape != (3,) or (self.intensity < 0.0).any():
            raise ValueError("intensity must be non-negative (3,)")


@dataclass(frozen=True)
class RasterizationResult:
    """Outputs from Gaussian Splatting PBR rasterizer."""

    rgb_float: np.ndarray  # shape: (H, W, 3), in [0.0, 1.0]
    rgb_uint8: np.ndarray  # shape: (H, W, 3), in [0, 255]
    depth_map: np.ndarray  # shape: (H, W)
    alpha_silhouette: np.ndarray  # shape: (H, W), in [0.0, 1.0]
    visible_gaussian_count: int


def compute_3d_covariance(gaussian: Gaussian3D) -> np.ndarray:
    """Compute 3D world covariance: Sigma = R @ diag(s^2) @ R.T."""
    r = gaussian.rotation.to_matrix()
    s_diag = np.diag(gaussian.scale**2)
    cov = r @ s_diag @ r.T
    return cast(np.ndarray, (cov + cov.T) * 0.5)


def project_gaussian_ewa(
    gaussian: Gaussian3D,
    camera: CameraParameters,
) -> tuple[np.ndarray, np.ndarray, float] | None:
    """EWA perspective projection yielding 2D center and regularized 2D conic inverse.

    Returns (mean_2d, conic_2d_inv, camera_z) or None if culled behind near plane.
    """
    w_mat = camera.view_matrix[:3, :3]
    t_cam = camera.view_matrix[:3, 3]

    # Transform mean to camera space: t = W @ mu + t_cam
    p_cam = w_mat @ gaussian.mean + t_cam
    t_z = float(p_cam[2])

    if t_z <= camera.near_plane or t_z >= camera.far_plane:
        return None

    t_x, t_y = float(p_cam[0]), float(p_cam[1])

    # Affine Jacobian of perspective projection J at p_cam
    fx, fy = camera.fx, camera.fy
    j = np.array(
        [
            [fx / t_z, 0.0, -fx * t_x / (t_z**2)],
            [0.0, fy / t_z, -fy * t_y / (t_z**2)],
        ],
        dtype=np.float64,
    )

    sigma_3d = compute_3d_covariance(gaussian)
    # Sigma_2D = J @ W @ Sigma_3D @ W.T @ J.T + 0.3 * I_2
    sigma_2d = j @ w_mat @ sigma_3d @ w_mat.T @ j.T + 0.3 * np.eye(2, dtype=np.float64)

    # Invert 2x2 matrix
    det = sigma_2d[0, 0] * sigma_2d[1, 1] - sigma_2d[0, 1] ** 2
    if det < 1e-12:
        return None

    conic_inv = (
        np.array(
            [
                [sigma_2d[1, 1], -sigma_2d[0, 1]],
                [-sigma_2d[0, 1], sigma_2d[0, 0]],
            ],
            dtype=np.float64,
        )
        / det
    )

    u_pix = fx * (t_x / t_z) + camera.cx
    v_pix = fy * (t_y / t_z) + camera.cy
    mean_2d = np.array([u_pix, v_pix], dtype=np.float64)

    return mean_2d, conic_inv, t_z


def evaluate_ggx_pbr(
    normal: np.ndarray,
    view_dir: np.ndarray,
    light_dir: np.ndarray,
    albedo: np.ndarray,
    roughness: float,
    metallic: float,
    light_radiance: np.ndarray,
) -> np.ndarray:
    """Cook-Torrance GGX microfacet direct relighting model.

    Enforces energy conservation and Helmholtz reciprocity.
    """
    n = normal / np.linalg.norm(normal)
    v = view_dir / np.linalg.norm(view_dir)
    l_u = light_dir / np.linalg.norm(light_dir)

    n_dot_l = float(np.clip(np.dot(n, l_u), 0.0, 1.0))
    n_dot_v = float(np.clip(np.dot(n, v), 0.0, 1.0))

    if n_dot_l <= 1e-6 or n_dot_v <= 1e-6:
        return np.zeros(3, dtype=np.float64)

    h = (l_u + v) / np.linalg.norm(l_u + v)
    n_dot_h = float(np.clip(np.dot(n, h), 0.0, 1.0))
    v_dot_h = float(np.clip(np.dot(v, h), 0.0, 1.0))

    alpha_g = max(roughness**2, 0.001)

    # 1. Trowbridge-Reitz GGX NDF
    d_denom = np.pi * ((n_dot_h**2) * (alpha_g**2 - 1.0) + 1.0) ** 2
    d_ggx = (alpha_g**2) / max(d_denom, 1e-12)

    # 2. Smith height-correlated / separable geometry factor G2
    def g1(n_dot_x: float) -> float:
        denom = n_dot_x + np.sqrt(alpha_g**2 + (1.0 - alpha_g**2) * (n_dot_x**2))
        return float(2.0 * n_dot_x / max(denom, 1e-12))

    g2 = g1(n_dot_v) * g1(n_dot_l)

    # 3. Schlick Fresnel
    f0 = 0.04 * (1.0 - metallic) + albedo * metallic
    f_schlick = f0 + (1.0 - f0) * ((1.0 - v_dot_h) ** 5)

    # Specular BRDF
    f_spec = (d_ggx * g2 * f_schlick) / max(4.0 * n_dot_v * n_dot_l, 1e-7)

    # Diffuse Lambertian BRDF (energy conserving with specular)
    kd = (1.0 - f_schlick) * (1.0 - metallic)
    f_diff = (kd * albedo) / np.pi

    f_brdf = f_diff + f_spec
    return cast(np.ndarray, f_brdf * n_dot_l * light_radiance)


def evaluate_order2_sh_diffuse(normal: np.ndarray, sh_coeffs: np.ndarray) -> np.ndarray:
    """Evaluate 9-coefficient Order-2 Spherical Harmonics diffuse irradiance."""
    n = normal / np.linalg.norm(normal)
    x, y, z = float(n[0]), float(n[1]), float(n[2])

    # Real spherical harmonics basis up to l=2
    c0 = 0.5 * np.sqrt(1.0 / np.pi)
    c1 = 0.5 * np.sqrt(3.0 / np.pi)
    c2 = 0.5 * np.sqrt(15.0 / np.pi)
    c3 = 0.25 * np.sqrt(5.0 / np.pi)
    c4 = 0.25 * np.sqrt(15.0 / np.pi)

    basis = np.array(
        [
            c0,
            c1 * y,
            c1 * z,
            c1 * x,
            c2 * x * y,
            c2 * y * z,
            c3 * (2.0 * z**2 - x**2 - y**2),
            c2 * x * z,
            c4 * (x**2 - y**2),
        ],
        dtype=np.float64,
    )

    irr = np.zeros(3, dtype=np.float64)
    for i in range(9):
        irr += basis[i] * sh_coeffs[i]
    return np.clip(irr, 0.0, 1.0)


def rasterize_gaussian_splats(
    gaussians: tuple[Gaussian3D, ...],
    camera: CameraParameters,
    lights: tuple[DirectionalLight | PointLight, ...],
    ambient_radiance: np.ndarray | None = None,
) -> RasterizationResult:
    """Depth-sorted front-to-back alpha compositing rasterizer."""
    if ambient_radiance is not None:
        ambient = ambient_radiance
    else:
        ambient = np.full(3, 0.05, dtype=np.float64)
    h, w = camera.height, camera.width

    # 1. Project and cull visible Gaussians
    projected_list: list[tuple[float, Gaussian3D, np.ndarray, np.ndarray, np.ndarray]] = []
    w_cam_pos = -camera.view_matrix[:3, :3].T @ camera.view_matrix[:3, 3]

    for g in gaussians:
        proj = project_gaussian_ewa(g, camera)
        if proj is None:
            continue
        mean_2d, conic_inv, t_z = proj

        # Calculate shaded radiance
        view_dir = w_cam_pos - g.mean
        n_v = float(np.linalg.norm(view_dir))
        if n_v > 1e-12:
            view_dir = view_dir / n_v
        else:
            view_dir = np.array([0.0, 0.0, 1.0])

        shaded_rgb = np.copy(ambient * g.albedo)
        if g.sh_coefficients is not None:
            shaded_rgb += evaluate_order2_sh_diffuse(g.normal, g.sh_coefficients) * g.albedo

        for light in lights:
            if isinstance(light, DirectionalLight):
                l_dir = light.direction
                l_rad = light.radiance
            else:
                l_vec = light.position - g.mean
                dist = float(np.linalg.norm(l_vec))
                if dist < 1e-12:
                    continue
                l_dir = l_vec / dist
                l_rad = light.intensity / max(dist**2, 0.01)

            shaded_rgb += evaluate_ggx_pbr(
                g.normal,
                view_dir,
                l_dir,
                g.albedo,
                g.roughness,
                g.metallic,
                l_rad,
            )

        projected_list.append((t_z, g, mean_2d, conic_inv, np.clip(shaded_rgb, 0.0, 1.0)))

    # Sort strictly front-to-back by camera depth t_z
    projected_list.sort(key=lambda item: item[0])
    visible_count = len(projected_list)

    accum_rgb = np.zeros((h, w, 3), dtype=np.float64)
    accum_depth = np.zeros((h, w), dtype=np.float64)
    accum_alpha = np.zeros((h, w), dtype=np.float64)
    transmittance = np.ones((h, w), dtype=np.float64)

    for t_z, g, mean_2d, conic_inv, shaded_color in projected_list:
        cx, cy = float(mean_2d[0]), float(mean_2d[1])
        # Conic bounding box at 3.0 standard deviations
        var_x = 1.0 / max(conic_inv[0, 0], 1e-6)
        var_y = 1.0 / max(conic_inv[1, 1], 1e-6)
        radius_x = int(np.ceil(3.0 * np.sqrt(var_x)))
        radius_y = int(np.ceil(3.0 * np.sqrt(var_y)))

        min_u = max(0, int(cx - radius_x))
        max_u = min(w, int(cx + radius_x + 1))
        min_v = max(0, int(cy - radius_y))
        max_v = min(h, int(cy + radius_y + 1))

        if min_u >= max_u or min_v >= max_v:
            continue

        u_grid, v_grid = np.meshgrid(np.arange(min_u, max_u), np.arange(min_v, max_v))
        du = u_grid - cx
        dv = v_grid - cy

        power = -0.5 * (
            conic_inv[0, 0] * (du**2) + 2.0 * conic_inv[0, 1] * du * dv + conic_inv[1, 1] * (dv**2)
        )
        valid_mask = power <= 0.0
        alpha_splat = np.zeros_like(power)
        alpha_splat[valid_mask] = g.opacity * np.exp(power[valid_mask])

        t_sub = transmittance[min_v:max_v, min_u:max_u]
        weight = t_sub * alpha_splat

        for c in range(3):
            accum_rgb[min_v:max_v, min_u:max_u, c] += weight * shaded_color[c]

        accum_depth[min_v:max_v, min_u:max_u] += weight * t_z
        accum_alpha[min_v:max_v, min_u:max_u] += weight
        transmittance[min_v:max_v, min_u:max_u] *= 1.0 - alpha_splat

    rgb_float = np.clip(accum_rgb, 0.0, 1.0)
    rgb_uint8 = np.clip(rgb_float * 255.0 + 0.5, 0, 255).astype(np.uint8)

    return RasterizationResult(
        rgb_float=rgb_float,
        rgb_uint8=rgb_uint8,
        depth_map=accum_depth,
        alpha_silhouette=np.clip(accum_alpha, 0.0, 1.0),
        visible_gaussian_count=visible_count,
    )
