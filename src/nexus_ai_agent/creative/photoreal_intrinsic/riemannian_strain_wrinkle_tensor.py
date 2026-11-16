"""Vectorized Green-Lagrange strain, wrinkle, and elastic image warp."""

from __future__ import annotations

from typing import cast

import numpy as np


class RiemannianStrainWrinkleTensor:
    def analyze(
        self, displacement: np.ndarray, roughness: np.ndarray | float = 0.5
    ) -> dict[str, np.ndarray]:
        u = np.asarray(displacement, dtype=np.float32)
        if u.ndim != 3 or u.shape[-1] != 2:
            raise ValueError("displacement must be HxWx2")
        ux_y, ux_x = np.gradient(u[..., 0])
        uy_y, uy_x = np.gradient(u[..., 1])
        f00 = 1 + ux_x
        f01 = ux_y
        f10 = uy_x
        f11 = 1 + uy_y
        e00 = 0.5 * (f00 * f00 + f10 * f10 - 1)
        e11 = 0.5 * (f01 * f01 + f11 * f11 - 1)
        e01 = 0.5 * (f00 * f01 + f10 * f11)
        mid = 0.5 * (e00 + e11)
        rad = np.sqrt((0.5 * (e00 - e11)) ** 2 + e01**2)
        e1 = mid - rad
        e2 = mid + rad
        angle = 0.5 * np.arctan2(2 * e01, e00 - e11)
        vec = np.stack((np.cos(angle), np.sin(angle)), -1)
        comp = np.maximum(0, -e1 - 0.035)
        wrinkles = (comp > 0).astype(np.float32)
        rough = (
            np.asarray(roughness, dtype=np.float32) * np.exp(-2.4 * np.maximum(e2, 0)) + 0.35 * comp
        )
        return {
            "epsilon_1": e1,
            "epsilon_2": e2,
            "eigenvector_1": vec,
            "roughness": rough,
            "wrinkle": wrinkles,
            "ao": 1 - 0.5 * wrinkles,
            "normal_deviation": comp,
        }

    @staticmethod
    def warp(image: np.ndarray, flow: np.ndarray) -> np.ndarray:
        im = np.asarray(image)
        h, w = im.shape[:2]
        y, x = np.mgrid[:h, :w]
        sx = np.clip(x + flow[..., 0], 0, w - 1)
        sy = np.clip(y + flow[..., 1], 0, h - 1)
        x0 = sx.astype(int)
        y0 = sy.astype(int)
        x1 = np.minimum(x0 + 1, w - 1)
        y1 = np.minimum(y0 + 1, h - 1)
        dx = sx - x0
        dy = sy - y0
        return cast(
            np.ndarray,
            (
                im[y0, x0] * (1 - dx)[..., None] * (1 - dy)[..., None]
                + im[y0, x1] * dx[..., None] * (1 - dy)[..., None]
                + im[y1, x0] * (1 - dx)[..., None] * dy[..., None]
                + im[y1, x1] * dx[..., None] * dy[..., None]
            ).astype(im.dtype),
        )
