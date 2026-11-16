"""Intrinsic decomposition and NumPy-only orthonormal DCT-II solvers."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import secrets
from dataclasses import dataclass
from typing import Any, Literal

import numpy as np

_BASIS = np.array(
    [[0.242, 0.118, 0.57735], [0.543, 0.768, 0.57735], [0.804, 0.629, 0.57735]], dtype=np.float64
)
_MODULE_KEY = secrets.token_bytes(32)
_RADIANCE_EPSILON = 1e-6
_POISSON_REGULARIZATION = 1e-4
_MAX_BASIS_CONDITION = 100.0
_FIELD_NAMES = (
    ("albedo", "albedo"),
    ("melanin", "melanin"),
    ("hemoglobin", "hemoglobin"),
    ("shade", "shading"),
    ("depth", "depth"),
    ("normals", "normals"),
)


KeyPolicy = Literal["auto", "persistent", "ephemeral"]


def validate_physiology(
    heart_rate_bpm: float = 72.0,
    time_sec: float = 0.0,
    sympathetic_flush: float = 0.0,
    blood_oxygenation: float = 0.97,
    focus_depth: float = 0.5,
) -> None:
    """Validate normalized physiology; time is seconds, depth/focus are unitless [0,1]."""
    values = (heart_rate_bpm, time_sec, sympathetic_flush, blood_oxygenation)
    if not all(math.isfinite(float(value)) for value in values):
        raise ValueError("physiological and temporal values must be finite")
    if not math.isfinite(float(focus_depth)):
        raise ValueError("focus_depth must be finite")
    if not 0 < heart_rate_bpm <= 240:
        raise ValueError("heart_rate_bpm must be in (0, 240]")
    if not 0.0 <= time_sec <= 86400.0:
        raise ValueError("time_sec must be in [0, 86400] seconds")
    if not 0.0 <= focus_depth <= 1.0:
        raise ValueError("focus_depth must be in normalized range [0, 1]")
    if not -1 <= sympathetic_flush <= 1:
        raise ValueError("sympathetic_flush must be in [-1, 1]")
    if not 0.70 <= blood_oxygenation <= 0.99:
        raise ValueError("blood_oxygenation must be in [0.70, 0.99]")


def _resolve_key(key: bytes | None, policy: KeyPolicy) -> tuple[bytes, bool]:
    if policy not in ("auto", "persistent", "ephemeral"):
        raise ValueError("key_policy must be 'auto', 'persistent', or 'ephemeral'")
    environment_value = os.environ.get("NEXUS_PHOTOREAL_HMAC_KEY")
    if environment_value is not None:
        environment_key = environment_value.encode()
        if len(environment_key) < 16:
            raise ValueError("NEXUS_PHOTOREAL_HMAC_KEY must contain at least 16 bytes")
    else:
        environment_key = None
    if key is not None and len(key) < 16:
        raise ValueError("hmac_key must contain at least 16 bytes")
    external_key = key if key is not None else environment_key
    if policy == "persistent" and external_key is None:
        raise ValueError(
            "Persistent vault requires an explicit hmac_key or NEXUS_PHOTOREAL_HMAC_KEY"
        )
    if policy == "ephemeral":
        return (external_key if external_key is not None else _MODULE_KEY), True
    if external_key is not None:
        return external_key, False
    return _MODULE_KEY, True


def _dct_axis(values: np.ndarray, axis: int) -> np.ndarray:
    a = np.moveaxis(np.asarray(values, dtype=np.float64), axis, -1)
    n = a.shape[-1]
    middle = (n + 1) // 2
    reordered = np.empty_like(a)
    reordered[..., :middle] = a[..., ::2]
    reordered[..., middle:] = a[..., 1::2][..., ::-1]
    k = np.arange(n, dtype=np.float64)
    spectrum = np.fft.fft(reordered, axis=-1)
    result = np.real(spectrum * np.exp(-1j * np.pi * k / (2 * n)))
    result *= np.sqrt(2.0 / n)
    result[..., 0] /= np.sqrt(2.0)
    return np.moveaxis(result, -1, axis)


def _idct_axis(coefficients: np.ndarray, axis: int) -> np.ndarray:
    c = np.moveaxis(np.asarray(coefficients, dtype=np.float64), axis, -1).copy()
    n = c.shape[-1]
    c[..., 0] *= np.sqrt(n)
    if n > 1:
        c[..., 1:] *= np.sqrt(n / 2.0)
    reverse = np.zeros_like(c)
    if n > 1:
        reverse[..., 1:] = c[..., :0:-1]
    k = np.arange(n, dtype=np.float64)
    spectrum = (c - 1j * reverse) * np.exp(1j * np.pi * k / (2 * n))
    reordered = np.real(np.fft.ifft(spectrum, axis=-1))
    middle = (n + 1) // 2
    result = np.empty_like(reordered)
    result[..., ::2] = reordered[..., :middle]
    result[..., 1::2] = reordered[..., middle:][..., ::-1]
    return np.moveaxis(result, -1, axis)


def dctn(values: np.ndarray) -> np.ndarray:
    """Orthonormal separable DCT-II, O(HW(log H + log W))."""
    return _dct_axis(_dct_axis(values, -1), -2)


def idctn(coefficients: np.ndarray) -> np.ndarray:
    """Exact orthonormal inverse DCT-II, implemented with FFTs only."""
    return _idct_axis(_idct_axis(coefficients, -1), -2)


@dataclass(frozen=True)
class PhotorealDNASeal:
    algorithm: str
    digest: str
    signature: str
    key_fingerprint: str
    ephemeral: bool


@dataclass(frozen=True)
class IntrinsicBiophysicalVault:
    melanin: np.ndarray
    hemoglobin: np.ndarray
    shading: np.ndarray
    albedo: np.ndarray
    normals: np.ndarray
    depth: np.ndarray
    seal: PhotorealDNASeal
    metadata: dict[str, Any]

    def __post_init__(self) -> None:
        for name in ("albedo", "melanin", "hemoglobin", "shading", "depth", "normals"):
            dtype = np.float64 if name == "normals" else np.float32
            array = np.array(getattr(self, name), dtype=dtype, copy=True)
            array.setflags(write=False)
            object.__setattr__(self, name, array)
        object.__setattr__(self, "metadata", dict(self.metadata))

    def _canonical_bytes(self) -> bytes:
        parts: list[bytes] = []
        for canonical_name, attr_name in _FIELD_NAMES:
            array = np.ascontiguousarray(getattr(self, attr_name))
            header = json.dumps(
                {
                    "name": canonical_name,
                    "ndim": array.ndim,
                    "shape": array.shape,
                    "dtype": array.dtype.str,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            parts.extend(
                (
                    len(header).to_bytes(8, "big"),
                    header,
                    array.nbytes.to_bytes(8, "big"),
                    array.tobytes(),
                )
            )
        meta = json.dumps(
            self.metadata, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
        parts.extend((len(meta).to_bytes(8, "big"), meta))
        parts.append(b"ephemeral=1" if self.seal.ephemeral else b"ephemeral=0")
        return b"".join(parts)

    def verify(self, hmac_key: bytes | None = None, key_policy: KeyPolicy = "auto") -> bool:
        key, _ = _resolve_key(hmac_key, key_policy)
        payload = self._canonical_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        signature = hmac.new(key, payload, hashlib.sha256).hexdigest()
        fingerprint = hashlib.sha256(key).hexdigest()[:8]
        return (
            self.seal.algorithm == "HMAC-SHA256"
            and hmac.compare_digest(self.seal.digest, digest)
            and hmac.compare_digest(self.seal.signature, signature)
            and hmac.compare_digest(self.seal.key_fingerprint, fingerprint)
        )


class LogChromophoreTomographyBaker:
    """Separate log-density chromophores and positive multiplicative illumination."""

    basis = _BASIS.copy()

    @staticmethod
    def _geometry(shade: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        lightness = np.log(np.maximum(shade.astype(np.float32), _RADIANCE_EPSILON))
        gy, gx = np.gradient(lightness)
        div = np.gradient(gx, axis=1) + np.gradient(gy, axis=0)
        div -= div.mean()
        height, width = shade.shape
        ky = np.arange(height, dtype=np.float64)[:, None]
        kx = np.arange(width, dtype=np.float64)[None, :]
        eig = (
            4 * np.sin(np.pi * ky / (2 * height)) ** 2
            + 4 * np.sin(np.pi * kx / (2 * width)) ** 2
            + _POISSON_REGULARIZATION
        )
        depth = idctn(dctn(-div) / eig).astype(np.float32)
        dy, dx = np.gradient(depth)
        normals = np.stack(
            (-dx.astype(np.float64), -dy.astype(np.float64), np.ones_like(depth, dtype=np.float64)),
            axis=-1,
        )
        normals /= np.maximum(np.linalg.norm(normals, axis=-1, keepdims=True), 1e-15)
        return normals, depth

    @staticmethod
    def _create_seal(vault: IntrinsicBiophysicalVault, key: bytes) -> PhotorealDNASeal:
        payload = vault._canonical_bytes()
        return PhotorealDNASeal(
            "HMAC-SHA256",
            hashlib.sha256(payload).hexdigest(),
            hmac.new(key, payload, hashlib.sha256).hexdigest(),
            hashlib.sha256(key).hexdigest()[:8],
            vault.seal.ephemeral,
        )

    def bake(
        self,
        rgb: np.ndarray,
        hmac_key: bytes | None = None,
        key_policy: KeyPolicy = "auto",
        input_colorspace: Literal["auto", "srgb", "linear"] = "auto",
    ) -> IntrinsicBiophysicalVault:
        raw = np.asarray(rgb)
        if raw.ndim != 3 or raw.shape[-1] != 3:
            raise ValueError("rgb must have shape (height,width,3)")
        if raw.shape[0] < 2 or raw.shape[1] < 2:
            raise ValueError("image spatial dimensions must be at least 2x2")
        if input_colorspace not in ("auto", "srgb", "linear"):
            raise ValueError("input_colorspace must be 'auto', 'srgb', or 'linear'")
        basis = np.asarray(self.basis, dtype=np.float64)
        if (
            basis.shape != (3, 3)
            or not np.isfinite(basis).all()
            or np.linalg.matrix_rank(basis) != 3
        ):
            raise ValueError("chromophore basis must be finite, full-rank, and well-conditioned")
        basis_cond = float(np.linalg.cond(basis))
        if not math.isfinite(basis_cond) or basis_cond > _MAX_BASIS_CONDITION:
            raise ValueError("chromophore basis must be finite, full-rank, and well-conditioned")
        if (
            not math.isfinite(_RADIANCE_EPSILON)
            or not math.isfinite(_POISSON_REGULARIZATION)
            or min(_RADIANCE_EPSILON, _POISSON_REGULARIZATION) <= 0
        ):
            raise ValueError("regularization parameters must be finite and positive")
        if np.issubdtype(raw.dtype, np.signedinteger):
            raise ValueError(
                "Signed integer dtypes are not supported; use uint8, uint16, or floating-point"
            )
        if np.issubdtype(raw.dtype, np.integer):
            normalized = raw.astype(np.float32) / float(np.iinfo(raw.dtype).max)
            default_space: Literal["srgb", "linear"] = "srgb"
        elif np.issubdtype(raw.dtype, np.floating):
            if (
                not np.isfinite(raw).all()
                or np.any(raw < 0)
                or np.any(raw > np.finfo(np.float32).max)
            ):
                raise ValueError("floating RGB radiance must be finite and nonnegative")
            normalized = raw.astype(np.float32)
            default_space = "linear"
        else:
            raise ValueError("rgb dtype must be integer or floating point")
        colorspace = default_space if input_colorspace == "auto" else input_colorspace
        if colorspace == "srgb":
            if np.any(normalized > 1):
                raise ValueError("sRGB input values must be normalized to [0,1]")
            linear = np.where(
                normalized <= 0.04045,
                normalized / 12.92,
                ((normalized + 0.055) / 1.055) ** 2.4,
            )
        else:
            linear = normalized
        density = -np.log(np.maximum(linear, _RADIANCE_EPSILON))
        chroma = np.einsum("...c,ic->...i", density, np.linalg.inv(basis)).astype(np.float32)
        mel, hem, _ = np.moveaxis(np.maximum(chroma, 0), -1, 0)
        shading = np.maximum(np.max(linear, axis=-1), _RADIANCE_EPSILON).astype(np.float32)
        albedo = np.clip(linear / shading[..., None], 0, 1).astype(np.float32)
        normals, depth = self._geometry(shading)
        key, ephemeral = _resolve_key(hmac_key, key_policy)
        temporary = IntrinsicBiophysicalVault(
            mel,
            hem,
            shading,
            albedo,
            normals,
            depth,
            PhotorealDNASeal("HMAC-SHA256", "", "", "", ephemeral),
            {
                "height": int(raw.shape[0]),
                "width": int(raw.shape[1]),
                "input_colorspace": colorspace,
            },
        )
        return IntrinsicBiophysicalVault(
            mel,
            hem,
            shading,
            albedo,
            normals,
            depth,
            self._create_seal(temporary, key),
            temporary.metadata,
        )

    @staticmethod
    def canonical_archetype(height: int = 64, width: int = 64) -> np.ndarray:
        y, x = np.mgrid[-1 : 1 : complex(height), -1 : 1 : complex(width)]
        face = np.exp(-2 * (x * x + y * y))
        return np.stack(
            (0.52 * face + 0.18, 0.34 * face + 0.12, 0.27 * face + 0.1), axis=-1
        ).astype(np.float32)
