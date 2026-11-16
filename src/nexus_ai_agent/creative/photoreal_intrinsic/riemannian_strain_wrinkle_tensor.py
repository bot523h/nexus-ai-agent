"""Green-Lagrange surface strain, wrinkle normals, and dense bilinear warp."""

from __future__ import annotations

from typing import cast

import numpy as np


class RiemannianStrainWrinkleTensor:
    def analyze(
        self,
        displacement: np.ndarray,
        roughness: np.ndarray | float = 0.5,
        surface_depth: np.ndarray | None = None,
    ) -> dict[str, np.ndarray]:
        input_array = np.asarray(displacement)
        numeric_dtype = np.float64 if input_array.dtype == np.float64 else np.float32
        u = np.asarray(input_array, dtype=numeric_dtype)
        if u.ndim != 3 or u.shape[-1] != 2 or min(u.shape[:2], default=0) < 2:
            raise ValueError("displacement must have shape HxWx2 with H and W >= 2")
        ux_y, ux_x = np.gradient(u[..., 0])
        uy_y, uy_x = np.gradient(u[..., 1])
        f00, f01, f10, f11 = 1 + ux_x, ux_y, uy_x, 1 + uy_y
        if surface_depth is None:
            z_x: np.ndarray = np.zeros(u.shape[:2], dtype=np.float64)
            z_y: np.ndarray = np.zeros_like(z_x)
        else:
            depth = np.asarray(surface_depth, dtype=numeric_dtype)
            if depth.shape != u.shape[:2] or not np.isfinite(depth).all():
                raise ValueError(
                    "surface_depth must be finite and match displacement spatial dimensions"
                )
            depth_gradients = np.gradient(depth)
            z_y, z_x = np.asarray(depth_gradients[0]), np.asarray(depth_gradients[1])
        if not np.any(u):
            zero = np.zeros(u.shape[:2], dtype=numeric_dtype)
            e1_vector = np.zeros((*u.shape[:2], 2), dtype=numeric_dtype)
            e1_vector[..., 1] = 1
            e2_vector = np.zeros((*u.shape[:2], 2), dtype=numeric_dtype)
            e2_vector[..., 0] = 1
            rough_map = np.broadcast_to(
                np.asarray(roughness, dtype=numeric_dtype), u.shape[:2]
            ).copy()
            return {
                "E_xx": zero,
                "E_yy": zero,
                "E_xy": zero,
                "epsilon_1": zero,
                "epsilon_2": zero,
                "eigenvector_1": e1_vector,
                "eigenvector_2": e2_vector,
                "roughness": rough_map,
                "wrinkle": zero,
                "ao": np.ones_like(zero),
                "normal_deviation": zero,
            }
        g00 = 1 + z_x * z_x
        g01 = z_x * z_y
        g11 = 1 + z_y * z_y
        c00 = g00 * f00**2 + 2 * g01 * f00 * f10 + g11 * f10**2
        c11 = g00 * f01**2 + 2 * g01 * f01 * f11 + g11 * f11**2
        c01 = g00 * f00 * f01 + g01 * (f00 * f11 + f10 * f01) + g11 * f10 * f11
        e00, e11, e01 = 0.5 * (c00 - g00), 0.5 * (c11 - g11), 0.5 * (c01 - g01)
        mid = 0.5 * (e00 + e11)
        rad = np.sqrt((0.5 * (e00 - e11)) ** 2 + e01**2)
        e1, e2 = mid - rad, mid + rad
        angle = 0.5 * np.arctan2(2 * e01, e00 - e11)
        e2_vec = np.stack((np.cos(angle), np.sin(angle)), -1)
        e1_vec = np.stack((-np.sin(angle), np.cos(angle)), -1)
        comp = np.maximum(0, -e1 - 0.035)
        wrinkles = (comp > 0).astype(np.float32)
        rough = (
            np.asarray(roughness, dtype=numeric_dtype) * np.exp(-2.4 * np.maximum(e2, 0))
            + 0.35 * comp
        )
        return {
            "E_xx": e00,
            "E_yy": e11,
            "E_xy": e01,
            "epsilon_1": e1,
            "epsilon_2": e2,
            "eigenvector_1": e1_vec,
            "eigenvector_2": e2_vec,
            "roughness": rough,
            "wrinkle": wrinkles,
            "ao": 1 - 0.5 * wrinkles,
            "normal_deviation": comp,
        }

    @staticmethod
    def perturb_normals(normals: np.ndarray, wrinkle_field: np.ndarray) -> np.ndarray:
        n = np.asarray(normals, dtype=np.float32)
        wrinkle = np.asarray(wrinkle_field, dtype=np.float32)
        if n.ndim != 3 or n.shape[-1] != 3 or wrinkle.shape != n.shape[:2]:
            raise ValueError("normals must be HxWx3 and wrinkle_field must match HxW")
        wy, wx = np.gradient(wrinkle)
        perturbed = n.copy()
        perturbed[..., 0] -= 0.12 * wx
        perturbed[..., 1] -= 0.12 * wy
        perturbed /= np.maximum(np.linalg.norm(perturbed, axis=-1, keepdims=True), 1e-12)
        return perturbed.astype(np.float32)

    @staticmethod
    def warp(image: np.ndarray, flow: np.ndarray) -> np.ndarray:
        im = np.asarray(image)
        flow_input = np.asarray(flow)
        coordinate_dtype = np.float64 if flow_input.dtype == np.float64 else np.float32
        displacement = np.asarray(flow_input, dtype=coordinate_dtype)
        if im.ndim not in (2, 3) or min(im.shape[:2], default=0) < 1:
            raise ValueError(
                "image must have shape (H,W) or (H,W,C) with nonempty spatial dimensions"
            )
        h, w = im.shape[:2]
        if displacement.shape != (h, w, 2) or not np.isfinite(displacement).all():
            raise ValueError("flow must be finite with shape (H,W,2) matching image")
        if not np.any(displacement):
            return im
        y, x = np.mgrid[:h, :w]
        y, x = y.astype(coordinate_dtype), x.astype(coordinate_dtype)
        sx = np.clip(x + displacement[..., 0], 0, w - 1)
        sy = np.clip(y + displacement[..., 1], 0, h - 1)
        x0, y0 = sx.astype(np.intp), sy.astype(np.intp)
        x1, y1 = np.minimum(x0 + 1, w - 1), np.minimum(y0 + 1, h - 1)
        dx, dy = sx - x0, sy - y0
        weights = ((1 - dx) * (1 - dy), dx * (1 - dy), (1 - dx) * dy, dx * dy)
        if im.ndim == 3:
            weights = tuple(weight[..., None] for weight in weights)
        warped = (
            im[y0, x0] * weights[0]
            + im[y0, x1] * weights[1]
            + im[y1, x0] * weights[2]
            + im[y1, x1] * weights[3]
        )
        if np.issubdtype(im.dtype, np.integer):
            info = np.iinfo(im.dtype)
            warped = np.floor(warped + 0.5).clip(info.min, info.max).astype(im.dtype)
        else:
            warped = warped.astype(im.dtype)
        return cast(np.ndarray, warped)
