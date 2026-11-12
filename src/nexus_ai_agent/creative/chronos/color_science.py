"""Transfer functions, Bradford adaptation, OT relighting, and CIEDE2000."""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

import numpy as np
from numpy.typing import NDArray

from .contracts import KernelError

FloatArray = NDArray[np.float64]


def srgb_decode(value: FloatArray) -> FloatArray:
    x = np.asarray(value, dtype=np.float64)
    if np.any((x < 0) | (x > 1)):
        raise KernelError("display RGB must be in [0,1]")
    return np.where(x <= 0.04045, x / 12.92, ((x + 0.055) / 1.055) ** 2.4)


def srgb_encode(value: FloatArray) -> FloatArray:
    x = np.clip(np.asarray(value, dtype=np.float64), 0, 1)
    return np.where(x <= 0.0031308, 12.92 * x, 1.055 * x ** (1 / 2.4) - 0.055)


def bt709_decode(value: FloatArray) -> FloatArray:
    x = np.asarray(value, dtype=np.float64)
    return np.where(x < 0.081, x / 4.5, ((x + 0.099) / 1.099) ** (1 / 0.45))


def bt709_encode(value: FloatArray) -> FloatArray:
    x = np.clip(np.asarray(value, dtype=np.float64), 0, 1)
    return np.where(x < 0.018, 4.5 * x, 1.099 * x**0.45 - 0.099)


def bradford_adaptation(source_xyz: FloatArray, target_xyz: FloatArray) -> FloatArray:
    matrix = np.array(
        [[0.8951, 0.2664, -0.1614], [-0.7502, 1.7135, 0.0367], [0.0389, -0.0685, 1.0296]]
    )
    src, dst = matrix @ source_xyz, matrix @ target_xyz
    if np.any(np.abs(src) < 1e-12):
        raise KernelError("singular source white")
    return np.linalg.solve(matrix, np.diag(dst / src) @ matrix)


def preserve_structure(
    log_luma: FloatArray, illumination: FloatArray, regularization: float = 1e-3
) -> FloatArray:
    """Regularized scalar Gaussian OT on detail; stable for flat/constant arrays."""
    image, target = (
        np.asarray(log_luma, dtype=np.float64),
        np.asarray(illumination, dtype=np.float64),
    )
    if (
        image.shape != target.shape
        or regularization <= 0
        or not np.all(np.isfinite(image + target))
    ):
        raise KernelError("invalid illumination inputs")
    detail = image - image.mean()
    scale = np.sqrt(
        (float(np.var(target)) + regularization) / (float(np.var(detail)) + regularization)
    )
    output: FloatArray = np.asarray(target.mean() + scale * detail, dtype=np.float64)
    return output


def rgb_to_lab(rgb: FloatArray) -> FloatArray:
    """sRGB D65 to CIE Lab (2 degree observer)."""
    linear = srgb_decode(np.asarray(rgb, dtype=np.float64))
    xyz = linear @ np.array(
        [
            [0.4124564, 0.2126729, 0.0193339],
            [0.3575761, 0.7151522, 0.1191920],
            [0.1804375, 0.0721750, 0.9503041],
        ]
    )
    ratio = xyz / np.array([0.95047, 1.0, 1.08883])
    delta = 6 / 29
    f = np.where(ratio > delta**3, np.cbrt(ratio), ratio / (3 * delta**2) + 4 / 29)
    return np.stack(
        (116 * f[..., 1] - 16, 500 * (f[..., 0] - f[..., 1]), 200 * (f[..., 1] - f[..., 2])),
        axis=-1,
    )


def delta_e_2000(lab1: FloatArray, lab2: FloatArray) -> FloatArray:
    """Sharma-Wu-Dalal CIEDE2000; inputs end in (L*,a*,b*)."""
    a1v, b1v = np.asarray(lab1, dtype=float)[..., 1], np.asarray(lab1, dtype=float)[..., 2]
    a2v, b2v = np.asarray(lab2, dtype=float)[..., 1], np.asarray(lab2, dtype=float)[..., 2]
    l1, l2 = np.asarray(lab1, dtype=float)[..., 0], np.asarray(lab2, dtype=float)[..., 0]
    c1, c2 = np.hypot(a1v, b1v), np.hypot(a2v, b2v)
    cm = (c1 + c2) / 2
    g = 0.5 * (1 - np.sqrt(cm**7 / (cm**7 + 25**7)))
    ap1, ap2 = (1 + g) * a1v, (1 + g) * a2v
    cp1, cp2 = np.hypot(ap1, b1v), np.hypot(ap2, b2v)
    hp1, hp2 = (
        np.mod(np.degrees(np.arctan2(b1v, ap1)), 360),
        np.mod(np.degrees(np.arctan2(b2v, ap2)), 360),
    )
    dl, dc = l2 - l1, cp2 - cp1
    dh = hp2 - hp1
    dh = np.where(
        cp1 * cp2 == 0, 0, np.where(dh > 180, dh - 360, np.where(dh < -180, dh + 360, dh))
    )
    dH = 2 * np.sqrt(cp1 * cp2) * np.sin(np.radians(dh / 2))
    lm, cpm = (l1 + l2) / 2, (cp1 + cp2) / 2
    hsum = hp1 + hp2
    hm = np.where(
        cp1 * cp2 == 0,
        hsum,
        np.where(
            np.abs(hp1 - hp2) <= 180,
            hsum / 2,
            np.where(hsum < 360, (hsum + 360) / 2, (hsum - 360) / 2),
        ),
    )
    t = (
        1
        - 0.17 * np.cos(np.radians(hm - 30))
        + 0.24 * np.cos(np.radians(2 * hm))
        + 0.32 * np.cos(np.radians(3 * hm + 6))
        - 0.20 * np.cos(np.radians(4 * hm - 63))
    )
    theta = 30 * np.exp(-(((hm - 275) / 25) ** 2))
    rc = 2 * np.sqrt(cpm**7 / (cpm**7 + 25**7))
    sl = 1 + 0.015 * (lm - 50) ** 2 / np.sqrt(20 + (lm - 50) ** 2)
    sc, sh = 1 + 0.045 * cpm, 1 + 0.015 * cpm * t
    rt = -np.sin(np.radians(2 * theta)) * rc
    x, y, z = dl / sl, dc / sc, dH / sh
    output: FloatArray = np.asarray(
        np.sqrt(np.maximum(0, x * x + y * y + z * z + rt * y * z)),
        dtype=np.float64,
    )
    return output


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    body = kind + data
    return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)


def write_rgb8_png(path: str | Path, rgb_u8: NDArray[np.uint8]) -> None:
    """Write a standards-compliant filter-zero RGB PNG using only stdlib."""
    image = np.asarray(rgb_u8)
    if image.ndim != 3 or image.shape[2] != 3 or image.dtype != np.uint8:
        raise KernelError("PNG input must be uint8 with shape (height, width, 3)")
    height, width, _ = image.shape
    if min(height, width) <= 0:
        raise KernelError("PNG dimensions must be non-zero")
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + row.tobytes() for row in image)
    content = b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", header)
    content += _png_chunk(b"IDAT", zlib.compress(raw, level=6)) + _png_chunk(b"IEND", b"")
    Path(path).write_bytes(content)


def read_rgb8_png(path: str | Path) -> NDArray[np.uint8]:
    """Read only filter-zero, 8-bit RGB PNGs and validate all chunk CRCs."""
    content = Path(path).read_bytes()
    if not content.startswith(b"\x89PNG\r\n\x1a\n"):
        raise KernelError("invalid PNG signature")
    offset, width, height = 8, 0, 0
    compressed: list[bytes] = []
    seen_header = False
    while offset < len(content):
        if offset + 12 > len(content):
            raise KernelError("truncated PNG chunk")
        length = struct.unpack_from(">I", content, offset)[0]
        kind = content[offset + 4 : offset + 8]
        start, end = offset + 8, offset + 8 + length
        if end + 4 > len(content):
            raise KernelError("truncated PNG data")
        data = content[start:end]
        expected = struct.unpack_from(">I", content, end)[0]
        if zlib.crc32(kind + data) & 0xFFFFFFFF != expected:
            raise KernelError("PNG CRC mismatch")
        if kind == b"IHDR":
            width, height, depth, color, comp, filt, interlace = struct.unpack(">IIBBBBB", data)
            if depth != 8 or color != 2 or (comp, filt, interlace) != (0, 0, 0):
                raise KernelError("unsupported PNG encoding")
            seen_header = True
        elif kind == b"IDAT":
            compressed.append(data)
        elif kind == b"IEND":
            break
        offset = end + 4
    if not seen_header or width <= 0 or height <= 0:
        raise KernelError("missing or invalid PNG header")
    raw = zlib.decompress(b"".join(compressed))
    stride = width * 3
    if len(raw) != height * (stride + 1):
        raise KernelError("PNG scanline length mismatch")
    rows = []
    for row in range(height):
        begin = row * (stride + 1)
        if raw[begin] != 0:
            raise KernelError("unsupported PNG filter")
        rows.append(np.frombuffer(raw[begin + 1 : begin + 1 + stride], dtype=np.uint8))
    output: NDArray[np.uint8] = np.stack(rows).reshape(height, width, 3)
    return output


def cct_kelvin_to_xyz(kelvin: float) -> tuple[float, float, float]:
    """Approximate Planckian-locus chromaticity (CIE 1931 xy) for 2000–15000 K."""
    if not 2000.0 <= kelvin <= 15000.0:
        raise KernelError("CCT is outside supported [2000, 15000] K domain")
    temperature = kelvin
    if temperature <= 4000:
        x = (
            -0.2661239e9 / temperature**3
            - 0.2343580e6 / temperature**2
            + 0.8776956e3 / temperature
            + 0.179910
        )
    else:
        x = (
            -3.0258469e9 / temperature**3
            + 2.1070379e6 / temperature**2
            + 0.2226347e3 / temperature
            + 0.240390
        )
    if temperature <= 2222:
        y = -1.1063814 * x**3 - 1.34811020 * x**2 + 2.18555832 * x - 0.20219683
    elif temperature <= 4000:
        y = -0.9549476 * x**3 - 1.37418593 * x**2 + 2.09137015 * x - 0.16748867
    else:
        y = 3.0817580 * x**3 - 5.87338670 * x**2 + 3.75112997 * x - 0.37001483
    if y <= 0:
        raise KernelError("CCT approximation produced invalid chromaticity")
    return float(x / y), 1.0, float((1 - x - y) / y)


def _symmetric_power(matrix: FloatArray, exponent: float) -> FloatArray:
    values, vectors = np.linalg.eigh((matrix + matrix.T) / 2)
    if np.any(values <= 0):
        raise KernelError("regularized covariance is not positive definite")
    result: FloatArray = np.asarray((vectors * values**exponent) @ vectors.T, dtype=np.float64)
    return result


def match_linear_rgb_gaussian_ot(
    source_rgb: FloatArray, target_rgb: FloatArray, regularization: float = 1e-6
) -> FloatArray:
    """3-channel Gaussian OT mapping in scene-linear RGB using symmetric eigensystems."""
    source = np.asarray(source_rgb, dtype=np.float64)
    target = np.asarray(target_rgb, dtype=np.float64)
    if source.shape[-1:] != (3,) or target.shape[-1:] != (3,) or source.size < 6 or target.size < 6:
        raise KernelError("source and target must each contain at least two RGB samples")
    if regularization <= 0 or not np.all(np.isfinite(source)) or not np.all(np.isfinite(target)):
        raise KernelError("invalid Gaussian OT inputs")
    s = source.reshape(-1, 3)
    t = target.reshape(-1, 3)
    mean_s, mean_t = s.mean(axis=0), t.mean(axis=0)
    cov_s = np.cov(s, rowvar=False) + regularization * np.eye(3)
    cov_t = np.cov(t, rowvar=False) + regularization * np.eye(3)
    root_s = _symmetric_power(cov_s, 0.5)
    inv_root_s = _symmetric_power(cov_s, -0.5)
    transport = inv_root_s @ _symmetric_power(root_s @ cov_t @ root_s, 0.5) @ inv_root_s
    transformed = (s - mean_s) @ transport.T + mean_t
    output: FloatArray = np.asarray(transformed.reshape(source.shape), dtype=np.float64)
    return output
