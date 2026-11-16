"""Deterministic RGB intrinsic decomposition using a fixed biological basis."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

import numpy as np
from scipy.fft import dctn, idctn

_BASIS = np.array(
    [[0.242, 0.118, 0.57735], [0.543, 0.768, 0.57735], [0.804, 0.629, 0.57735]], dtype=np.float64
)


@dataclass(frozen=True)
class IntrinsicBiophysicalVault:
    melanin: np.ndarray
    hemoglobin: np.ndarray
    shading: np.ndarray
    albedo: np.ndarray
    normals: np.ndarray
    depth: np.ndarray
    seal: str

    def __post_init__(self) -> None:
        for name in ("melanin", "hemoglobin", "shading", "albedo", "normals", "depth"):
            arr = np.asarray(getattr(self, name), dtype=np.float32).copy()
            arr.setflags(write=False)
            object.__setattr__(self, name, arr)
        expected = self.compute_seal()
        if self.seal != expected:
            raise ValueError("PhotorealDNASeal mismatch")

    def compute_seal(self) -> str:
        h = hashlib.sha256()
        for name in ("melanin", "hemoglobin", "shading", "albedo", "normals", "depth"):
            h.update(np.ascontiguousarray(getattr(self, name), dtype=np.float32).tobytes())
        return h.hexdigest()


class LogChromophoreTomographyBaker:
    """Invert log optical density into melanin, hemoglobin, and achromatic shade."""

    basis = _BASIS.copy()

    @staticmethod
    def _geometry(shade: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        shade = shade.astype(np.float32)
        gy, gx = np.gradient(shade)
        div = np.gradient(gx, axis=1) + np.gradient(gy, axis=0)
        height, width = shade.shape
        ky = np.arange(height, dtype=np.float64)[:, None]
        kx = np.arange(width, dtype=np.float64)[None, :]
        eigenvalues = (
            (2 - 2 * np.cos(np.pi * kx / width)) + (2 - 2 * np.cos(np.pi * ky / height)) + 1e-4
        )
        depth = np.asarray(
            idctn(dctn(-div, type=2, norm="ortho") / eigenvalues, type=2, norm="ortho"),
            dtype=np.float32,
        )
        n = np.stack((-gx, -gy, np.ones_like(shade)), axis=-1)
        n /= np.maximum(np.linalg.norm(n, axis=-1, keepdims=True), 1e-8)
        # Sub-pixel deterministic pore-scale perturbation; no random state.
        yy, xx = np.indices(shade.shape)
        pore = (np.sin(xx * 6.283 / 7) * np.sin(yy * 6.283 / 9) * 0.002).astype(np.float32)
        normals = n.copy()
        normals[..., 0] += pore
        normals /= np.maximum(np.linalg.norm(normals, axis=-1, keepdims=True), 1e-8)
        return normals.astype(np.float32), depth

    def bake(self, rgb: np.ndarray) -> IntrinsicBiophysicalVault:
        a = np.asarray(rgb, dtype=np.float32)
        if a.ndim != 3 or a.shape[-1] != 3:
            raise ValueError("rgb must have shape (height,width,3)")
        if a.max(initial=0) > 1:
            a = np.asarray(a / 255, dtype=np.float32)
        lin = np.where(a <= 0.04045, a / 12.92, ((a + 0.055) / 1.055) ** 2.4)
        density = -np.log(np.maximum(lin, 1e-6))
        coeff = np.einsum("...c,ic->...i", density, np.linalg.inv(self.basis)).astype(np.float32)
        mel, hem, shade = np.moveaxis(coeff, -1, 0)
        normals, depth = self._geometry(shade)
        albedo = np.exp(-np.maximum(density - shade[..., None] * self.basis[:, 2], 0)).astype(
            np.float32
        )
        fields = [mel, hem, shade, albedo, normals, depth]
        h = hashlib.sha256()
        for v in fields:
            h.update(np.ascontiguousarray(v, dtype=np.float32).tobytes())
        return IntrinsicBiophysicalVault(mel, hem, shade, albedo, normals, depth, h.hexdigest())

    @staticmethod
    def canonical_archetype(height: int = 64, width: int = 64) -> np.ndarray:
        y, x = np.mgrid[-1 : 1 : complex(height), -1 : 1 : complex(width)]
        face = np.exp(-2 * (x * x + y * y))
        return np.stack(
            (0.52 * face + 0.18, 0.34 * face + 0.12, 0.27 * face + 0.1), axis=-1
        ).astype(np.float32)
