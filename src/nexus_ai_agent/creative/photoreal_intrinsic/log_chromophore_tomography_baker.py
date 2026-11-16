"""Intrinsic image decomposition and NumPy-only orthonormal DCT-II solvers."""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass, field

import numpy as np

_BASIS = np.array(
    [[0.242, 0.118, 0.57735], [0.543, 0.768, 0.57735], [0.804, 0.629, 0.57735]], dtype=np.float64
)
_DEFAULT_KEY = b"nexus-ai-agent:photoreal-intrinsic:internal-seal-v1"


def _dct_axis(values: np.ndarray, axis: int) -> np.ndarray:
    a = np.moveaxis(np.asarray(values, dtype=np.float64), axis, -1)
    n = a.shape[-1]
    extended = np.concatenate((a, np.flip(a, axis=-1)), axis=-1)
    spectrum = np.fft.fft(extended, axis=-1)[..., :n]
    k = np.arange(n, dtype=np.float64)
    raw = 0.5 * np.real(spectrum * np.exp(-1j * np.pi * k / (2 * n)))
    raw[..., 0] *= np.sqrt(1 / n)
    if n > 1:
        raw[..., 1:] *= np.sqrt(2 / n)
    return np.moveaxis(raw, -1, axis)


def _idct_axis(coefficients: np.ndarray, axis: int) -> np.ndarray:
    c = np.moveaxis(np.asarray(coefficients, dtype=np.float64), axis, -1)
    n = c.shape[-1]
    k = np.arange(n, dtype=np.float64)
    x = np.arange(n, dtype=np.float64)
    basis = np.cos(np.pi * (x[:, None] + 0.5) * k[None, :] / n)
    basis[:, 0] *= np.sqrt(1 / n)
    if n > 1:
        basis[:, 1:] *= np.sqrt(2 / n)
    out = np.einsum("...k,nk->...n", c, basis)
    return np.moveaxis(out, -1, axis)


def dctn(values: np.ndarray) -> np.ndarray:
    return _dct_axis(_dct_axis(values, -1), -2)


def idctn(coefficients: np.ndarray) -> np.ndarray:
    return _idct_axis(_idct_axis(coefficients, -1), -2)


@dataclass(frozen=True)
class IntrinsicBiophysicalVault:
    melanin: np.ndarray
    hemoglobin: np.ndarray
    shading: np.ndarray
    albedo: np.ndarray
    normals: np.ndarray
    depth: np.ndarray
    seal: str
    hmac_key: bytes = field(default=_DEFAULT_KEY, repr=False, compare=False)

    def __post_init__(self) -> None:
        for name in ("melanin", "hemoglobin", "shading", "albedo", "normals", "depth"):
            arr = np.asarray(getattr(self, name), dtype=np.float32).copy()
            arr.setflags(write=False)
            object.__setattr__(self, name, arr)
        if not self.verify():
            raise ValueError("PhotorealDNASeal mismatch")

    def compute_seal(self, hmac_key: bytes | None = None) -> str:
        mac = hmac.new(self.hmac_key if hmac_key is None else hmac_key, digestmod=hashlib.sha256)
        for name in ("melanin", "hemoglobin", "shading", "albedo", "normals", "depth"):
            mac.update(np.ascontiguousarray(getattr(self, name), dtype=np.float32).tobytes())
        return mac.hexdigest()

    def verify(self, hmac_key: bytes | None = None) -> bool:
        return hmac.compare_digest(self.seal, self.compute_seal(hmac_key))


class LogChromophoreTomographyBaker:
    """Separate log-density chromophores and positive multiplicative illumination."""

    basis = _BASIS.copy()

    @staticmethod
    def _geometry(shade: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        lightness = np.log(np.maximum(shade.astype(np.float32), 1e-6))
        gy, gx = np.gradient(lightness)
        div = np.gradient(gx, axis=1) + np.gradient(gy, axis=0)
        height, width = shade.shape
        ky = np.arange(height, dtype=np.float64)[:, None]
        kx = np.arange(width, dtype=np.float64)[None, :]
        eig = 2 - 2 * np.cos(np.pi * kx / width) + 2 - 2 * np.cos(np.pi * ky / height) + 1e-4
        depth = idctn(dctn(-div) / eig).astype(np.float32)
        dy, dx = np.gradient(depth)
        normals = np.stack((-dx, -dy, np.ones_like(depth)), axis=-1)
        normals /= np.maximum(np.linalg.norm(normals, axis=-1, keepdims=True), 1e-8)
        return normals.astype(np.float32), depth

    def bake(self, rgb: np.ndarray, hmac_key: bytes | None = None) -> IntrinsicBiophysicalVault:
        raw = np.asarray(rgb)
        if raw.ndim != 3 or raw.shape[-1] != 3:
            raise ValueError("rgb must have shape (height,width,3)")
        if raw.shape[0] < 2 or raw.shape[1] < 2:
            raise ValueError("image spatial dimensions must be at least 2x2")
        if np.issubdtype(raw.dtype, np.integer):
            a = raw.astype(np.float32) / float(np.iinfo(raw.dtype).max)
        elif np.issubdtype(raw.dtype, np.floating):
            a = raw.astype(np.float32)
            if not np.isfinite(a).all() or np.any(a < 0):
                raise ValueError("floating RGB radiance must be finite and nonnegative")
        else:
            raise ValueError("rgb dtype must be integer or floating point")
        linear = (
            a
            if np.issubdtype(raw.dtype, np.floating)
            else np.where(a <= 0.04045, a / 12.92, ((a + 0.055) / 1.055) ** 2.4)
        )
        density = -np.log(np.maximum(linear, 1e-6))
        chroma = np.einsum("...c,ic->...i", density, np.linalg.inv(self.basis)).astype(np.float32)
        mel, hem, _ = np.moveaxis(chroma, -1, 0)
        # Positive scalar illumination, with an exact multiplicative reconstruction.
        shading = np.maximum(np.max(linear, axis=-1), 1e-6).astype(np.float32)
        albedo = np.clip(linear / shading[..., None], 0, 1).astype(np.float32)
        normals, depth = self._geometry(shading)
        key = _DEFAULT_KEY if hmac_key is None else hmac_key
        mac = hmac.new(key, digestmod=hashlib.sha256)
        for value in (mel, hem, shading, albedo, normals, depth):
            mac.update(np.ascontiguousarray(value, dtype=np.float32).tobytes())
        return IntrinsicBiophysicalVault(
            mel, hem, shading, albedo, normals, depth, mac.hexdigest(), key
        )

    @staticmethod
    def canonical_archetype(height: int = 64, width: int = 64) -> np.ndarray:
        y, x = np.mgrid[-1 : 1 : complex(height), -1 : 1 : complex(width)]
        face = np.exp(-2 * (x * x + y * y))
        return np.stack(
            (0.52 * face + 0.18, 0.34 * face + 0.12, 0.27 * face + 0.1), axis=-1
        ).astype(np.float32)
