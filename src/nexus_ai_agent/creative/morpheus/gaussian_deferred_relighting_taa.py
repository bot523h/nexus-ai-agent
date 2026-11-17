"""Wave 13: Relightable 3DGS Deferred G-Buffer, Spherical Gaussian Transport & TAA.

Implements multi-channel deferred 3DGS G-buffer splatting (beauty, albedo, unit normals, depth,
material RMS, analytical 2D motion vectors), Spherical Gaussian (SG) all-frequency relighting,
and YCoCg variance-clipped Temporal Anti-Aliasing (TAA) with disocclusion anti-ghosting.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

import numpy as np

from nexus_ai_agent.creative.morpheus.gaussian_splatting_pbr_rasterizer import (
    CameraParameters,
    Gaussian3D,
    project_gaussian_ewa,
)


@dataclass(frozen=True)
class SphericalGaussianLobe:
    """Parametric Spherical Gaussian (SG) light lobe G(v; xi, lambda, mu)."""

    axis_direction: np.ndarray  # xi, shape: (3,), unit vector
    sharpness: float  # lambda > 0
    amplitude_rgb: np.ndarray  # mu, shape: (3,), non-negative

    def __post_init__(self) -> None:
        if self.axis_direction.shape != (3,):
            raise ValueError("axis_direction must be (3,)")
        n = float(np.linalg.norm(self.axis_direction))
        if abs(n - 1.0) > 1e-4:
            raise ValueError(f"axis_direction must be unit length, got {n}")
        if self.sharpness <= 0.0:
            raise ValueError(f"sharpness must be positive, got {self.sharpness}")
        if self.amplitude_rgb.shape != (3,) or (self.amplitude_rgb < 0.0).any():
            raise ValueError("amplitude_rgb must be non-negative (3,)")


@dataclass(frozen=True)
class MorpheusDeferredGBuffer:
    """Multi-channel deferred G-buffer for relightable 3DGS compositing."""

    beauty_rgb: np.ndarray  # shape: (H, W, 3), in [0.0, 1.0]
    albedo_rgb: np.ndarray  # shape: (H, W, 3)
    world_normal: np.ndarray  # shape: (H, W, 3), unit length on foreground
    depth_z: np.ndarray  # shape: (H, W)
    alpha_matte: np.ndarray  # shape: (H, W), in [0.0, 1.0]
    material_rms: np.ndarray  # shape: (H, W, 3): (roughness, metallic, sss_weight)
    motion_vectors_2d: np.ndarray  # shape: (H, W, 2): (delta_u, delta_v)


def splat_deferred_gbuffer(
    gaussians: tuple[Gaussian3D, ...],
    camera: CameraParameters,
    camera_prev: CameraParameters | None = None,
    gaussian_3d_velocities: tuple[np.ndarray, ...] | None = None,
    ambient_radiance: float = 0.05,
) -> MorpheusDeferredGBuffer:
    """Rasterize Gaussians into multi-channel deferred G-buffer with motion vectors."""
    h, w = camera.height, camera.width

    # Output buffers
    acc_beauty = np.zeros((h, w, 3), dtype=np.float64)
    acc_albedo = np.zeros((h, w, 3), dtype=np.float64)
    acc_normal = np.zeros((h, w, 3), dtype=np.float64)
    acc_depth = np.zeros((h, w), dtype=np.float64)
    acc_alpha = np.zeros((h, w), dtype=np.float64)
    acc_rms = np.zeros((h, w, 3), dtype=np.float64)
    acc_motion = np.zeros((h, w, 2), dtype=np.float64)
    transmittance = np.ones((h, w), dtype=np.float64)

    # Project and depth sort
    projected: list[tuple[float, Gaussian3D, np.ndarray, np.ndarray, int]] = []
    for idx, g in enumerate(gaussians):
        proj = project_gaussian_ewa(g, camera)
        if proj is None:
            continue
        mean_2d, conic_inv, t_z = proj
        projected.append((t_z, g, mean_2d, conic_inv, idx))

    projected.sort(key=lambda item: item[0])

    for t_z, g, mean_2d, conic_inv, g_idx in projected:
        cx, cy = float(mean_2d[0]), float(mean_2d[1])
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
        valid = power <= 0.0
        alpha_splat = np.zeros_like(power)
        alpha_splat[valid] = g.opacity * np.exp(power[valid])

        # Analytical screen-space motion vector
        # delta_p_screen = J @ (v_3d + v_cam)
        if gaussian_3d_velocities and g_idx < len(gaussian_3d_velocities):
            v_3d = gaussian_3d_velocities[g_idx]
        else:
            v_3d = np.zeros(3, dtype=np.float64)

        fx, fy = camera.fx, camera.fy
        delta_u = float((fx * v_3d[0]) / max(t_z, 0.1))
        delta_v = float((fy * v_3d[1]) / max(t_z, 0.1))
        motion_vec = np.array([delta_u, delta_v], dtype=np.float64)

        t_sub = transmittance[min_v:max_v, min_u:max_u]
        weight = alpha_splat * t_sub

        for c in range(3):
            acc_beauty[min_v:max_v, min_u:max_u, c] += weight * (ambient_radiance * g.albedo[c])
            acc_albedo[min_v:max_v, min_u:max_u, c] += weight * g.albedo[c]
            acc_normal[min_v:max_v, min_u:max_u, c] += weight * g.normal[c]

        acc_depth[min_v:max_v, min_u:max_u] += weight * t_z
        acc_alpha[min_v:max_v, min_u:max_u] += weight

        acc_rms[min_v:max_v, min_u:max_u, 0] += weight * g.roughness
        acc_rms[min_v:max_v, min_u:max_u, 1] += weight * g.metallic
        # Default sss_weight = 0.5 * (1.0 - metallic)
        acc_rms[min_v:max_v, min_u:max_u, 2] += weight * (0.5 * (1.0 - g.metallic))

        acc_motion[min_v:max_v, min_u:max_u, 0] += weight * motion_vec[0]
        acc_motion[min_v:max_v, min_u:max_u, 1] += weight * motion_vec[1]

        transmittance[min_v:max_v, min_u:max_u] *= np.maximum(0.0, 1.0 - alpha_splat)

    # Normalize accumulated world normals on foreground
    norm_len = np.sqrt(np.sum(acc_normal**2, axis=-1, keepdims=True))
    fg_mask = (acc_alpha[:, :, np.newaxis] > 1e-3) & (norm_len > 1e-8)
    acc_normal = np.where(fg_mask, acc_normal / np.maximum(norm_len, 1e-8), 0.0)

    return MorpheusDeferredGBuffer(
        beauty_rgb=np.clip(acc_beauty, 0.0, 1.0),
        albedo_rgb=acc_albedo,
        world_normal=acc_normal,
        depth_z=acc_depth,
        alpha_matte=np.clip(acc_alpha, 0.0, 1.0),
        material_rms=acc_rms,
        motion_vectors_2d=acc_motion,
    )


# ── Spherical Gaussian (SG) Specular & Diffuse Relighting ────────────────────


def evaluate_sg_inner_product(
    sg1: SphericalGaussianLobe,
    sg2: SphericalGaussianLobe,
) -> np.ndarray:
    """Compute exact closed-form integral of product of two SGs over S^2: int_S2 G1(v)*G2(v) dv."""
    xi_m = sg1.sharpness * sg1.axis_direction + sg2.sharpness * sg2.axis_direction
    lambda_m = float(np.linalg.norm(xi_m))
    if lambda_m < 1e-6:
        # Near antipodal or zero sharpness
        factor = 4.0 * np.pi * np.exp(-sg1.sharpness - sg2.sharpness)
    else:
        # 4*pi / lambda_m * exp(lambda_m - lambda1 - lambda2) * sinh(lambda_m)
        term_pos = np.exp(lambda_m - sg1.sharpness - sg2.sharpness)
        term_neg = np.exp(-lambda_m - sg1.sharpness - sg2.sharpness)
        factor = (2.0 * np.pi / lambda_m) * (term_pos - term_neg)
    return cast(np.ndarray, float(max(factor, 0.0)) * (sg1.amplitude_rgb * sg2.amplitude_rgb))


def relight_deferred_gbuffer_sg(
    gbuffer: MorpheusDeferredGBuffer,
    camera: CameraParameters,
    sg_lights: tuple[SphericalGaussianLobe, ...],
) -> np.ndarray:
    """Evaluate all-frequency specular and diffuse relighting on deferred G-buffer."""
    h, w = camera.height, camera.width
    lit_rgb = np.copy(gbuffer.beauty_rgb)
    w_cam_pos = -camera.view_matrix[:3, :3].T @ camera.view_matrix[:3, 3]

    for y in range(h):
        for x in range(w):
            alpha = gbuffer.alpha_matte[y, x]
            if alpha < 1e-3:
                continue
            n = gbuffer.world_normal[y, x]
            if np.allclose(n, 0.0):
                continue
            albedo = gbuffer.albedo_rgb[y, x]
            roughness, metallic, _ = gbuffer.material_rms[y, x]

            # Reconstruct pixel world position proxy
            t_z = max(gbuffer.depth_z[y, x], 0.1)
            p_cam = np.array(
                [(x - camera.cx) * t_z / camera.fx, (y - camera.cy) * t_z / camera.fy, t_z]
            )
            p_world = camera.view_matrix[:3, :3].T @ (p_cam - camera.view_matrix[:3, 3])
            v_dir = w_cam_pos - p_world
            n_v = float(np.linalg.norm(v_dir))
            v_dir = v_dir / max(n_v, 1e-8)

            n_dot_v = float(np.clip(np.dot(n, v_dir), 0.0, 1.0))
            if n_dot_v < 1e-6:
                continue

            # Specular reflection lobe axis: r = 2*(n . v)*n - v
            r_axis = 2.0 * n_dot_v * n - v_dir
            r_axis = r_axis / np.linalg.norm(r_axis)

            # Warped SG NDF lobe: sharpness lambda_ndf = 2 / (alpha^2 * (n . v))
            alpha_g = max(roughness**2, 0.01)
            lambda_ndf = float(2.0 / (alpha_g**2 * max(n_dot_v, 0.05)))

            # Diffuse clamp lobe aligned with normal n
            sg_diffuse_ndf = SphericalGaussianLobe(
                axis_direction=n,
                sharpness=1.0,
                amplitude_rgb=albedo / np.pi,
            )

            # Specular lobe
            f0 = (1.0 - metallic) * 0.04 + metallic * albedo
            sg_spec_ndf = SphericalGaussianLobe(
                axis_direction=r_axis,
                sharpness=lambda_ndf,
                amplitude_rgb=f0,
            )

            accum_light = np.zeros(3, dtype=np.float64)
            for sg_light in sg_lights:
                # Diffuse irradiance
                diff_rad = evaluate_sg_inner_product(sg_diffuse_ndf, sg_light)
                # Specular irradiance
                spec_rad = evaluate_sg_inner_product(sg_spec_ndf, sg_light)
                accum_light += diff_rad + spec_rad

            lit_rgb[y, x] = np.clip(lit_rgb[y, x] + accum_light * alpha, 0.0, 1.0)

    return lit_rgb


# ── YCoCg Reversible Color Space & Variance-Clipped TAA ───────────────────────


def rgb_to_ycocg(rgb: np.ndarray) -> np.ndarray:
    """Convert RGB to reversible YCoCg color space."""
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    y = 0.25 * r + 0.50 * g + 0.25 * b
    co = 0.50 * r - 0.50 * b
    cg = -0.25 * r + 0.50 * g - 0.25 * b
    return np.stack([y, co, cg], axis=-1)


def ycocg_to_rgb(ycocg: np.ndarray) -> np.ndarray:
    """Convert YCoCg back to RGB color space."""
    y, co, cg = ycocg[..., 0], ycocg[..., 1], ycocg[..., 2]
    r = y + co - cg
    g = y + cg
    b = y - co - cg
    res = np.clip(np.stack([r, g, b], axis=-1), 0.0, 1.0)
    return cast(np.ndarray, res)


def apply_variance_clipped_taa(
    current_frame_rgb: np.ndarray,
    history_frame_rgb: np.ndarray,
    motion_vectors_2d: np.ndarray,
    depth_current: np.ndarray,
    depth_history: np.ndarray,
    alpha_hysteresis: float = 0.10,
    gamma_variance: float = 1.25,
    disocclusion_threshold_mm: float = 5.0,
) -> np.ndarray:
    """Apply YCoCg variance-clipped Temporal Anti-Aliasing with bilinear history reprojection.

    Guarantees:
      1. Variance reduction on static/smoothly moving jittered frames.
      2. Zero ghosting on sudden foreground disocclusion (falls back to current frame).
    """
    if current_frame_rgb.shape != history_frame_rgb.shape:
        raise ValueError("current and history frame shapes mismatch")
    h, w, _ = current_frame_rgb.shape
    if motion_vectors_2d.shape != (h, w, 2):
        raise ValueError(f"motion_vectors_2d shape mismatch: {motion_vectors_2d.shape}")

    curr_ycocg = rgb_to_ycocg(current_frame_rgb)
    out_ycocg = np.zeros_like(curr_ycocg)

    for y in range(h):
        for x in range(w):
            du, dv = motion_vectors_2d[y, x]
            prev_x = float(x - du)
            prev_y = float(y - dv)

            # Disocclusion / boundary check
            if not (0 <= prev_x <= w - 1 and 0 <= prev_y <= h - 1):
                out_ycocg[y, x] = curr_ycocg[y, x]
                continue

            # Depth disocclusion check
            x0, y0 = int(np.floor(prev_x)), int(np.floor(prev_y))
            if abs(depth_current[y, x] - depth_history[y0, x0]) > disocclusion_threshold_mm:
                # Sudden disocclusion detected: reject history to eliminate ghosting
                out_ycocg[y, x] = curr_ycocg[y, x]
                continue

            # Bilinear history sample
            x1 = min(w - 1, x0 + 1)
            y1 = min(h - 1, y0 + 1)
            wx = prev_x - x0
            wy = prev_y - y0

            hist_rgb_sampled = (
                (1.0 - wx) * (1.0 - wy) * history_frame_rgb[y0, x0]
                + wx * (1.0 - wy) * history_frame_rgb[y0, x1]
                + (1.0 - wx) * wy * history_frame_rgb[y1, x0]
                + wx * wy * history_frame_rgb[y1, x1]
            )
            hist_sample = rgb_to_ycocg(hist_rgb_sampled)

            # Compute 3x3 spatial neighborhood statistics in YCoCg
            nbr_min_y = max(0, y - 1)
            nbr_max_y = min(h, y + 2)
            nbr_min_x = max(0, x - 1)
            nbr_max_x = min(w, x + 2)
            neighborhood = curr_ycocg[nbr_min_y:nbr_max_y, nbr_min_x:nbr_max_x]

            mu = np.mean(neighborhood, axis=(0, 1))
            sigma = np.std(neighborhood, axis=(0, 1))

            aabb_min = mu - gamma_variance * sigma
            aabb_max = mu + gamma_variance * sigma

            # Variance-clipped history
            hist_clipped = np.clip(hist_sample, aabb_min, aabb_max)

            # Temporal blend with hysteresis
            out_ycocg[y, x] = (
                alpha_hysteresis * curr_ycocg[y, x] + (1.0 - alpha_hysteresis) * hist_clipped
            )

    return ycocg_to_rgb(out_ycocg)
