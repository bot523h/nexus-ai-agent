"""Versioned, domain-separated SHA-256 randomness and bounded immutable buffers.

Seeds are cryptographically *derived*, not encrypted secrets. No clock, global RNG,
network, filesystem, Python hash(), or platform-endian serialization is involved.
Float results are reproducible on the same IEEE-754/libm/NumPy stack; transcendental
functions are not promised bit-identical across different numerical libraries.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import TypeAlias

import numpy as np
from numpy.typing import NDArray

Seed: TypeAlias = str | bytes | int
FloatArray: TypeAlias = NDArray[np.float64]
PCMArray: TypeAlias = NDArray[np.int16]
Vec3: TypeAlias = tuple[float, float, float]
RGB: TypeAlias = tuple[float, float, float]

SCHEMA = "nagar-world-backlot-v1"
MAX_POINTS = 32_768
MAX_RANDOM_VALUES = 1_500_000
MAX_AUDIO_SECONDS = 5.0
SAMPLE_RATE = 48_000
LICENSE = "CC0-Procedural"


def finite(value: float, name: str, low: float, high: float) -> float:
    """Reject NaN, infinities, booleans and out-of-contract scalar values."""
    if isinstance(value, bool) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{name} must be finite and in [{low}, {high}]")
    return float(value)


def integer(value: int, name: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"{name} must be an integer in [{low}, {high}]")
    return value


def vector(value: Vec3, name: str, limit: float = 1e9) -> Vec3:
    if len(value) != 3:
        raise ValueError(f"{name} must have three components")
    return (
        finite(value[0], name, -limit, limit),
        finite(value[1], name, -limit, limit),
        finite(value[2], name, -limit, limit),
    )


def canonical_bytes(value: object) -> bytes:
    """Package-local canonical JSON, deliberately not an RFC 8785/JCS claim."""
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def digest(value: object) -> str:
    return hashlib.sha256(canonical_bytes((SCHEMA, value))).hexdigest()


def seed_bytes(seed: Seed, namespace: str = "root") -> bytes:
    if isinstance(seed, bool) or not isinstance(seed, (str, bytes, int)):
        raise ValueError("seed must be a string, bytes, or integer")
    if not namespace or len(namespace) > 1024:
        raise ValueError("a nonempty namespace of at most 1024 characters is required")
    if isinstance(seed, bytes):
        kind, payload = b"bytes", seed
    elif isinstance(seed, str):
        kind, payload = b"str", seed.encode("utf-8")
    else:
        kind, payload = b"int", str(seed).encode("ascii")
    if len(payload) > 65_536:
        raise ValueError("seed exceeds the 64 KiB limit")
    parts = (SCHEMA.encode("ascii"), kind, payload, namespace.encode("utf-8"))
    return hashlib.sha256(b"".join(len(p).to_bytes(8, "big") + p for p in parts)).digest()


def uniform(seed: Seed, namespace: str, count: int) -> FloatArray:
    """Counter-mode SHA-256, four open-interval 52-bit uniforms per block.

    Each named stream is prefix stable and independent of the requested count.
    SHA-256 is used as a deterministic PRF here, not a password KDF or encryption.
    """
    integer(count, "random value count", 0, MAX_RANDOM_VALUES)
    key = seed_bytes(seed, namespace)
    raw = b"".join(
        hashlib.sha256(key + counter.to_bytes(8, "big")).digest()
        for counter in range((count + 3) // 4)
    )
    words = np.frombuffer(raw, dtype=">u8", count=count)
    return ((words >> 12).astype(np.float64) + 0.5) * (2.0**-52)


def normal(seed: Seed, namespace: str, count: int) -> FloatArray:
    """Box-Muller normals without a process-global RNG."""
    integer(count, "normal value count", 0, MAX_RANDOM_VALUES // 2)
    pairs = (count + 1) // 2
    u = uniform(seed, namespace, 2 * pairs).reshape(pairs, 2)
    radius = np.sqrt(-2.0 * np.log(u[:, 0]))
    angle = 2.0 * math.pi * u[:, 1]
    result = np.empty(2 * pairs, dtype=np.float64)
    result[0::2] = radius * np.cos(angle)
    result[1::2] = radius * np.sin(angle)
    return result[:count]


def frozen_floats(values: FloatArray) -> FloatArray:
    """An immutable bytes owner prevents callers re-enabling WRITEABLE."""
    array = np.asarray(values, dtype="<f8", order="C")
    if not np.isfinite(array).all():
        raise ValueError("non-finite array")
    return np.frombuffer(array.tobytes(), dtype="<f8").reshape(array.shape)


def frozen_pcm(values: PCMArray) -> PCMArray:
    array = np.asarray(values, dtype="<i2", order="C")
    return np.frombuffer(array.tobytes(), dtype="<i2").reshape(array.shape)


def array_digest(values: FloatArray | PCMArray) -> str:
    array = np.asarray(values, order="C")
    # Explicit little endian makes the byte contract independent of host endianness.
    dtype = "<f8" if array.dtype.kind == "f" else "<i2"
    encoded = array.astype(dtype, copy=False)
    state = hashlib.sha256(canonical_bytes((SCHEMA, dtype, array.shape)))
    state.update(encoded.tobytes())
    return state.hexdigest()


def points_array(points: FloatArray) -> FloatArray:
    array = np.asarray(points, dtype=np.float64)
    if array.ndim != 2 or array.shape[1] != 3 or len(array) > MAX_POINTS:
        raise ValueError(f"expected an (N, 3) array with N <= {MAX_POINTS}")
    if not np.isfinite(array).all():
        raise ValueError("points must be finite")
    return array
