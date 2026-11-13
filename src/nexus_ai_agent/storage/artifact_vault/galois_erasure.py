"""Pure-Python/NumPy Reed-Solomon erasure coding over GF(256), polynomial 0x11d."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

PRIMITIVE_POLYNOMIAL = 0x11D
_EXP = np.zeros(512, dtype=np.uint16)
_LOG = np.full(256, -1, dtype=np.int16)
x = 1
for i in range(255):
    _EXP[i] = x
    _LOG[x] = i
    x <<= 1
    if x & 0x100:
        x ^= PRIMITIVE_POLYNOMIAL
_EXP[255:] = _EXP[:257]
_LOG[0] = -1
EXP_TABLE = _EXP.astype(np.uint8)
LOG_TABLE = _LOG


def gf_multiply(a: int, b: int) -> int:
    a, b = int(a), int(b)
    if not 0 <= a < 256 or not 0 <= b < 256:
        raise ValueError("GF(256) elements must be in [0, 255]")
    return 0 if not a or not b else int(_EXP[int(_LOG[a]) + int(_LOG[b])])


mul = gf_multiply
gf_mul = gf_multiply


def gf_inverse(a: int) -> int:
    a = int(a)
    if not 1 <= a < 256:
        raise ValueError("only non-zero GF(256) elements have an inverse")
    return int(_EXP[255 - int(_LOG[a])])


inverse = gf_inverse
gf_inv = gf_inverse


def _gf_scale(vector: np.ndarray, coefficient: int) -> np.ndarray:
    """Vectorised GF(256) multiply of a uint8 vector by a scalar via log/exp gathers."""
    if coefficient == 0:
        return np.zeros(vector.shape, dtype=np.uint8)
    idx = np.asarray(vector, dtype=np.uint8)
    logs = _LOG[idx] + _LOG[int(coefficient)]
    out: NDArray[np.uint8] = np.asarray(_EXP[logs], dtype=np.uint8)
    out[idx == 0] = 0
    return out


def _matrix_multiply(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    result = np.zeros((a.shape[0], b.shape[1]), dtype=np.uint8)
    for i in range(a.shape[0]):
        for j in range(b.shape[1]):
            value = 0
            for k in range(a.shape[1]):
                value ^= gf_multiply(int(a[i, k]), int(b[k, j]))
            result[i, j] = value
    return result


def matrix_invert(matrix: np.ndarray) -> np.ndarray:
    matrix = np.asarray(matrix, dtype=np.uint8)
    if matrix.ndim != 2 or matrix.shape[0] != matrix.shape[1]:
        raise ValueError("matrix must be square")
    n = matrix.shape[0]
    aug = np.concatenate((matrix.copy(), np.eye(n, dtype=np.uint8)), axis=1)
    for col in range(n):
        pivot = next((row for row in range(col, n) if aug[row, col]), None)
        if pivot is None:
            raise ValueError("matrix is singular over GF(256)")
        if pivot != col:
            aug[[col, pivot]] = aug[[pivot, col]]
        scale = gf_inverse(int(aug[col, col]))
        aug[col] = np.array([gf_multiply(int(v), scale) for v in aug[col]], dtype=np.uint8)
        for row in range(n):
            if row != col and aug[row, col]:
                factor = int(aug[row, col])
                aug[row] ^= np.array(
                    [gf_multiply(int(v), factor) for v in aug[col]], dtype=np.uint8
                )
    return aug[:, n:]


invert_matrix = matrix_invert


def _generator(k: int, m: int) -> np.ndarray:
    rows = k + m
    matrix = np.zeros((rows, k), dtype=np.uint8)
    matrix[:k] = np.eye(k, dtype=np.uint8)
    for row in range(k, rows):
        base = row - k + 1
        for col in range(k):
            matrix[row, col] = 1 if col == 0 else gf_multiply(matrix[row, col - 1], base)
    return matrix


@dataclass(frozen=True, slots=True)
class ErasureShard:
    index: int
    data: bytes
    total: int
    data_shards: int
    parity_shards: int

    @property
    def is_parity(self) -> bool:
        return self.index >= self.data_shards

    @property
    def shard_index(self) -> int:
        return self.index


def encode(data: bytes | bytearray | memoryview, k: int, m: int) -> list[ErasureShard]:
    if k < 1 or m < 1 or k + m > 255:
        raise ValueError("require 1 <= k and 1 <= m with k+m <= 255")
    raw = bytes(data)
    if not raw:
        raise ValueError("payload must be non-empty")
    size = max(1, (len(raw) + k - 1) // k)
    padded = raw.ljust(size * k, b"\0")
    data_arrays = [
        np.frombuffer(padded[i * size : (i + 1) * size], dtype=np.uint8) for i in range(k)
    ]
    generator = _generator(k, m)
    shards = [bytes(a) for a in data_arrays]
    for row in generator[k:]:
        parity = np.zeros(size, dtype=np.uint8)
        for col, coefficient in enumerate(row):
            if coefficient:
                parity ^= _gf_scale(data_arrays[col], int(coefficient))
        shards.append(bytes(parity))
    return [ErasureShard(i, shard, k + m, k, m) for i, shard in enumerate(shards)]


def reconstruct(
    shards: list[ErasureShard] | tuple[ErasureShard, ...], original_size: int | None = None
) -> bytes:
    if not shards:
        raise ValueError("at least k shards are required")
    first = shards[0]
    k, m = first.data_shards, first.parity_shards
    if len(shards) < k or any(s.total != k + m for s in shards):
        raise ValueError("need at least k compatible shards")
    selected = {s.index: s for s in shards}
    if len(selected) < k or any(not 0 <= i < k + m for i in selected):
        raise ValueError("invalid or duplicate shard indexes")
    selected_items = list(selected.values())[:k]
    size = len(selected_items[0].data)
    if any(len(s.data) != size for s in selected_items):
        raise ValueError("shards have inconsistent sizes")
    generator = _generator(k, m)
    submatrix = generator[[s.index for s in selected_items]]
    inverse_matrix = matrix_invert(submatrix)
    selected_arrays = [np.frombuffer(s.data, dtype=np.uint8) for s in selected_items]
    recovered = []
    for row in inverse_matrix:
        output = np.zeros(size, dtype=np.uint8)
        for col, coefficient in enumerate(row):
            if coefficient:
                output ^= _gf_scale(selected_arrays[col], int(coefficient))
        recovered.append(bytes(output))
    result = b"".join(recovered)
    return result if original_size is None else result[:original_size]


encode_shards = encode
reconstruct_data = reconstruct


def encode_erasure_shards(
    data_bytes: bytes | bytearray | memoryview, k_data: int, m_parity: int
) -> list[ErasureShard]:
    """Named wrapper for systematic ``(k_data, m_parity)`` encoding."""
    return encode(data_bytes, k_data, m_parity)


def reconstruct_from_shards(
    available_shards: list[ErasureShard] | tuple[ErasureShard, ...],
    original_byte_length: int | None = None,
) -> bytes:
    """Recover the original payload from any compatible surviving shards."""
    return reconstruct(available_shards, original_byte_length)
