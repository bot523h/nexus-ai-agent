"""Minimal, deterministic PNG I/O plus array helpers (stdlib + numpy only).

The prototype must be able to read and write real image files in an environment
with no imaging toolkit installed beyond ``numpy`` (a declared NEXUS runtime
dependency). Rather than add a dependency, this module implements the subset of
PNG the benchmark actually needs:

* **write** — 8-bit RGB, non-interlaced, filter 0, fixed zlib level, no
  timestamp or text chunks. Two runs of the same array therefore produce
  byte-identical files, which is what makes the benchmark digest reproducible.
* **read** — 8-bit, non-interlaced, colour types 0/2/4/6 (grey, RGB, grey+alpha,
  RGBA) with all five filter types. Interlaced files and any other colour type
  are refused with :class:`MediaUnsupported` instead of being guessed at.

PNG input is **untrusted**. A 190 KB file can declare 8000×8000 pixels and
decompress to 192 MB; a crafted header can declare far more. Every decode is
therefore bounded *before* allocation by an explicit :class:`DecodeLimits`:

============  ==================================================================
input bytes   refused when the file itself is larger than ``max_input_bytes``
dimensions    ``max_width`` / ``max_height`` checked straight from IHDR
pixels        ``max_pixels`` (width × height) checked before any row is decoded
IDAT bytes    total compressed payload capped by ``max_idat_bytes``
decompression streamed through ``max_decompressed_bytes`` and stopped exactly at
              the declared bitmap size, so a bomb fails instead of allocating
============  ==================================================================

Everything the continuity verifier measures starts from these arrays, so the
reader is strict: a truncated file, a bad CRC, an unknown chunk or an exceeded
cap raises rather than returning a partially decoded image.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .errors import MediaLimitExceeded, MediaUnsupported

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_IEND = b"IEND"
_IDAT = b"IDAT"
_IHDR = b"IHDR"
_PLTE = b"PLTE"


@dataclass(frozen=True)
class DecodeLimits:
    """Explicit resource caps for untrusted media.

    The defaults are generous for a production still (16 MP) and tiny compared
    with what a decompression bomb asks for.
    """

    max_width: int = 8192
    max_height: int = 8192
    max_pixels: int = 16_000_000
    max_input_bytes: int = 32 * 1024 * 1024
    max_idat_bytes: int = 48 * 1024 * 1024
    max_decompressed_bytes: int = 96 * 1024 * 1024
    max_audio_frames: int = 48_000 * 60 * 10  # 10 minutes at 48 kHz

    def __post_init__(self) -> None:
        for name in (
            "max_width",
            "max_height",
            "max_pixels",
            "max_input_bytes",
            "max_idat_bytes",
            "max_decompressed_bytes",
            "max_audio_frames",
        ):
            if int(getattr(self, name)) <= 0:
                raise MediaLimitExceeded("decode limits must be positive", limit=name)


#: Defaults used by every caller that does not pass its own.
DEFAULT_LIMITS = DecodeLimits()


#: Media types this island recognises by *content*, never by filename.
PNG_MIME = "image/png"
JPEG_MIME = "image/jpeg"
WAV_MIME = "audio/wav"


def sniff_media_type(data: bytes) -> str | None:
    """Classify bytes by signature. Returns ``None`` for anything unrecognised.

    Only formats the island can actually process are returned: PNG, JPEG (stored
    and integrity-checked, not decoded) and WAV. A caller-supplied filename or
    declared MIME type is never evidence.
    """
    if data.startswith(_PNG_MAGIC):
        return PNG_MIME
    if data.startswith(b"\xff\xd8\xff"):
        return JPEG_MIME
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return WAV_MIME
    return None


def _chunk(kind: bytes, payload: bytes) -> bytes:
    crc = zlib.crc32(kind + payload) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", crc)


def encode_png(rgb: np.ndarray, *, level: int = 6) -> bytes:
    """Encode an ``(H, W, 3)`` uint8 array as a deterministic PNG."""
    array = np.asarray(rgb)
    if array.ndim != 3 or array.shape[2] != 3 or array.dtype != np.uint8:
        raise MediaUnsupported(f"encode_png expects uint8 (H,W,3), got {array.shape}/{array.dtype}")
    height, width = array.shape[0], array.shape[1]
    if height == 0 or width == 0:
        raise MediaUnsupported("refusing to encode an empty image")
    scanlines = np.zeros((height, 1 + width * 3), dtype=np.uint8)
    scanlines[:, 1:] = array.reshape(height, width * 3)
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"".join(
        [
            _PNG_MAGIC,
            _chunk(_IHDR, ihdr),
            _chunk(_IDAT, zlib.compress(scanlines.tobytes(), level)),
            _chunk(_IEND, b""),
        ]
    )


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def _unfilter(raw: bytes, height: int, width: int, bpp: int) -> np.ndarray:
    stride = width * bpp
    if len(raw) < height * (stride + 1):
        raise MediaUnsupported("truncated PNG scanline data")
    out = np.zeros((height, stride), dtype=np.uint8)
    prev = np.zeros(stride, dtype=np.uint8)
    pos = 0
    for row in range(height):
        ftype = raw[pos]
        pos += 1
        line = np.frombuffer(raw, dtype=np.uint8, count=stride, offset=pos).astype(np.int16)
        pos += stride
        if ftype == 0:
            cur = line
        elif ftype == 1:  # Sub
            cur = line.copy()
            for i in range(bpp, stride):
                cur[i] = (cur[i] + cur[i - bpp]) & 0xFF
        elif ftype == 2:  # Up
            cur = (line + prev.astype(np.int16)) & 0xFF
        elif ftype == 3:  # Average
            cur = line.copy()
            for i in range(stride):
                left = int(cur[i - bpp]) if i >= bpp else 0
                cur[i] = (cur[i] + ((left + int(prev[i])) >> 1)) & 0xFF
        elif ftype == 4:  # Paeth
            cur = line.copy()
            for i in range(stride):
                left = int(cur[i - bpp]) if i >= bpp else 0
                up = int(prev[i])
                upleft = int(prev[i - bpp]) if i >= bpp else 0
                cur[i] = (cur[i] + _paeth(left, up, upleft)) & 0xFF
        else:
            raise MediaUnsupported(f"unsupported PNG filter type {ftype}")
        out[row] = cur.astype(np.uint8)
        prev = out[row]
    return out


def decode_png(data: bytes, *, limits: DecodeLimits | None = None) -> np.ndarray:
    """Decode an 8-bit non-interlaced PNG into an ``(H, W, 3)`` uint8 array."""
    limit = limits or DEFAULT_LIMITS
    if not data.startswith(_PNG_MAGIC):
        raise MediaUnsupported("not a PNG file")
    if len(data) > limit.max_input_bytes:
        raise MediaLimitExceeded(
            "PNG input exceeds the configured byte cap",
            size=len(data),
            cap=limit.max_input_bytes,
        )
    pos = len(_PNG_MAGIC)
    header: tuple[int, int, int, int, int] | None = None
    idat = bytearray()
    palette: bytes | None = None
    while pos + 8 <= len(data):
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        kind = data[pos + 4 : pos + 8]
        if length > limit.max_input_bytes:
            raise MediaLimitExceeded(
                "PNG chunk length exceeds the configured byte cap", length=length
            )
        payload = data[pos + 8 : pos + 8 + length]
        if len(payload) != length or pos + 12 + length > len(data):
            raise MediaUnsupported("truncated PNG chunk")
        (crc,) = struct.unpack(">I", data[pos + 8 + length : pos + 12 + length])
        if zlib.crc32(kind + payload) & 0xFFFFFFFF != crc:
            raise MediaUnsupported("PNG chunk CRC mismatch")
        pos += 12 + length
        if kind == _IHDR:
            if header is not None:
                raise MediaUnsupported("PNG declares more than one IHDR")
            width, height, depth, colour, comp, filt, interlace = struct.unpack(">IIBBBBB", payload)
            _check_dimensions(width, height, limit)
            if depth != 8 or interlace != 0 or comp != 0 or filt != 0:
                raise MediaUnsupported(
                    f"unsupported PNG header depth={depth} interlace={interlace}"
                )
            if colour not in (0, 2, 4, 6):
                raise MediaUnsupported(f"unsupported PNG colour type {colour}")
            header = (width, height, colour, depth, interlace)
        elif kind == _PLTE:
            palette = payload
        elif kind == _IDAT:
            if header is None:
                raise MediaUnsupported("PNG data before IHDR")
            idat += payload
            if len(idat) > limit.max_idat_bytes:
                raise MediaLimitExceeded(
                    "PNG compressed payload exceeds the configured cap",
                    size=len(idat),
                    cap=limit.max_idat_bytes,
                )
        elif kind == _IEND:
            break
    if header is None:
        raise MediaUnsupported("PNG has no IHDR")
    if palette is not None and header[2] != 2:
        raise MediaUnsupported("palette chunk in an unsupported colour type")
    width, height, colour = header[0], header[1], header[2]
    channels = {0: 1, 2: 3, 4: 2, 6: 4}[colour]
    expected = height * (width * channels + 1)
    if expected > limit.max_decompressed_bytes:
        raise MediaLimitExceeded(
            "PNG would decompress beyond the configured cap",
            expected_bytes=expected,
            cap=limit.max_decompressed_bytes,
        )
    raw = _decompress_bounded(bytes(idat), expected, limit)
    planes = _unfilter(raw, height, width, channels).reshape(height, width, channels)
    if channels == 1:
        return np.repeat(planes, 3, axis=2)
    if channels == 2:
        return np.repeat(planes[:, :, :1], 3, axis=2)
    return planes[:, :, :3].copy()


def _check_dimensions(width: int, height: int, limits: DecodeLimits) -> None:
    if width <= 0 or height <= 0:
        raise MediaUnsupported("PNG declares an empty image", width=width, height=height)
    if width > limits.max_width or height > limits.max_height:
        raise MediaLimitExceeded(
            "PNG dimensions exceed the configured cap",
            width=width,
            height=height,
            cap=(limits.max_width, limits.max_height),
        )
    if width * height > limits.max_pixels:
        raise MediaLimitExceeded(
            "PNG pixel count exceeds the configured cap",
            pixels=width * height,
            cap=limits.max_pixels,
        )


def _decompress_bounded(payload: bytes, expected: int, limits: DecodeLimits) -> bytes:
    """Inflate ``payload`` but never past ``expected`` bytes (no bomb allocation)."""
    if expected > limits.max_decompressed_bytes:
        raise MediaLimitExceeded("declared bitmap exceeds the decompression cap", size=expected)
    engine = zlib.decompressobj()
    try:
        raw = engine.decompress(payload, expected)
    except zlib.error as exc:
        raise MediaUnsupported("PNG image data is not a valid zlib stream") from exc
    if engine.unconsumed_tail:
        raise MediaLimitExceeded(
            "PNG decompresses to more bytes than its header declares", declared=expected
        )
    if len(raw) != expected:
        raise MediaUnsupported("truncated PNG scanline data", expected=expected, got=len(raw))
    return raw


def load_rgb(path: str | Path, *, limits: DecodeLimits | None = None) -> np.ndarray:
    """Load a PNG from disk as an ``(H, W, 3)`` uint8 array, within the caps."""
    limit = limits or DEFAULT_LIMITS
    target = Path(path)
    size = target.stat().st_size
    if size > limit.max_input_bytes:
        raise MediaLimitExceeded(
            "image file exceeds the configured byte cap", size=size, cap=limit.max_input_bytes
        )
    return decode_png(target.read_bytes(), limits=limit)


def save_rgb(path: str | Path, rgb: np.ndarray) -> None:
    """Write an ``(H, W, 3)`` uint8 array as a deterministic PNG."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(encode_png(rgb))


def to_gray(rgb: np.ndarray) -> np.ndarray:
    """ITU-R BT.601 luma in ``0..255`` float space."""
    array = np.asarray(rgb, dtype=np.float64)
    return 0.299 * array[..., 0] + 0.587 * array[..., 1] + 0.114 * array[..., 2]


def resize_bilinear(image: np.ndarray, height: int, width: int) -> np.ndarray:
    """Bilinear resample an ``(H, W, C)`` or ``(H, W)`` array to a fixed size."""
    array = np.asarray(image, dtype=np.float64)
    squeeze = array.ndim == 2
    if squeeze:
        array = array[:, :, None]
    src_h, src_w, channels = array.shape
    if height <= 0 or width <= 0:
        raise MediaUnsupported("resize target must be positive")
    ys = (np.arange(height) + 0.5) * (src_h / height) - 0.5
    xs = (np.arange(width) + 0.5) * (src_w / width) - 0.5
    y0 = np.clip(np.floor(ys).astype(int), 0, src_h - 1)
    y1 = np.clip(y0 + 1, 0, src_h - 1)
    x0 = np.clip(np.floor(xs).astype(int), 0, src_w - 1)
    x1 = np.clip(x0 + 1, 0, src_w - 1)
    wy = np.clip(ys - y0, 0.0, 1.0)[:, None, None]
    wx = np.clip(xs - x0, 0.0, 1.0)[None, :, None]
    top = array[y0][:, x0] * (1 - wx) + array[y0][:, x1] * wx
    bottom = array[y1][:, x0] * (1 - wx) + array[y1][:, x1] * wx
    out = top * (1 - wy) + bottom * wy
    return out[:, :, 0] if squeeze else out


def encode_wav(samples: np.ndarray, *, sample_rate: int) -> bytes:
    """Encode mono float samples in ``-1..1`` as a 16-bit PCM WAV (deterministic)."""
    array = np.asarray(samples, dtype=np.float64).reshape(-1)
    clipped = np.clip(array, -1.0, 1.0)
    pcm = np.round(clipped * 32767.0).astype("<i2").tobytes()
    byte_rate = sample_rate * 2
    header = b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVE"
    fmt = b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, sample_rate, byte_rate, 2, 16)
    return header + fmt + b"data" + struct.pack("<I", len(pcm)) + pcm


def decode_wav(data: bytes, *, limits: DecodeLimits | None = None) -> tuple[np.ndarray, int]:
    """Decode a 16-bit PCM mono/stereo WAV written by :func:`encode_wav`."""
    limit = limits or DEFAULT_LIMITS
    if len(data) > limit.max_input_bytes:
        raise MediaLimitExceeded(
            "WAV input exceeds the configured byte cap", size=len(data), cap=limit.max_input_bytes
        )
    if len(data) < 44 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise MediaUnsupported("not a RIFF/WAVE file")
    pos = 12
    sample_rate = 0
    channels = 1
    bits = 16
    samples: bytes | None = None
    while pos + 8 <= len(data):
        kind = data[pos : pos + 4]
        (length,) = struct.unpack("<I", data[pos + 4 : pos + 8])
        body = data[pos + 8 : pos + 8 + length]
        pos += 8 + length + (length & 1)
        if kind == b"fmt ":
            fmt, channels, sample_rate = struct.unpack("<HHI", body[:8])
            bits = struct.unpack("<H", body[14:16])[0]
            if fmt != 1 or bits != 16:
                raise MediaUnsupported(f"unsupported WAV encoding fmt={fmt} bits={bits}")
        elif kind == b"data":
            samples = body
    if samples is None or sample_rate <= 0:
        raise MediaUnsupported("WAV has no data/fmt chunk")
    frames = len(samples) // max(2 * max(channels, 1), 1)
    if frames > limit.max_audio_frames:
        raise MediaLimitExceeded(
            "WAV frame count exceeds the configured cap", frames=frames, cap=limit.max_audio_frames
        )
    pcm = np.frombuffer(samples, dtype="<i2").astype(np.float64) / 32768.0
    if channels > 1:
        pcm = pcm.reshape(-1, channels).mean(axis=1)
    return pcm, sample_rate
