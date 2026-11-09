"""Phase-8 regression suite: bounded decoding (F08 and friends).

The finding: a 186,696-byte PNG expanded to a 8000x8000 RGB array (192 MB). A
28-byte header is not evidence of a small image, so every decoder here refuses
*before* allocating. The tests file the same class of bomb at each entry point.
"""

# ruff: noqa: E402 - _support.ensure_path() must run before these imports
from __future__ import annotations

import _support

_support.ensure_path()

import struct
import zlib

import pytest

from experiments.character_capsule.errors import MediaLimitExceeded, MediaUnsupported
from experiments.character_capsule.media import (
    DEFAULT_LIMITS,
    DecodeLimits,
    decode_png,
    decode_wav,
    encode_png,
    encode_wav,
    load_rgb,
)


def _png(width: int, height: int, *, bit_depth: int = 8, colour_type: int = 2) -> bytes:
    """A valid-enough PNG header: the decoder must refuse on the header, not the data."""

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, bit_depth, colour_type, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IEND", b"")


def _wav(sample_rate: int, channels: int, frames: int, bits: int = 16) -> bytes:
    data = b"\x00" * (frames * channels * bits // 8)
    byte_rate = sample_rate * channels * bits // 8
    block_align = channels * bits // 8
    fmt = struct.pack("<HHIIHH", 1, channels, sample_rate, byte_rate, block_align, bits)
    header = b"RIFF" + struct.pack("<I", 4 + 8 + len(fmt) + 8 + len(data)) + b"WAVE"
    body = b"fmt " + struct.pack("<I", len(fmt)) + fmt
    body += b"data" + struct.pack("<I", len(data)) + data
    return header + body


def test_f08_a_pixel_bomb_is_refused_before_allocating():
    bomb = _png(8000, 8000)
    assert len(bomb) < 200_000  # small file, 192 MB of pixels
    with pytest.raises(MediaLimitExceeded):
        decode_png(bomb)


def test_f08b_the_limit_is_a_header_check_not_a_post_hoc_size_check():
    just_over = _png(5000, 4000)  # 20 MP > the 16 MP default
    with pytest.raises(MediaLimitExceeded):
        decode_png(just_over)
    import numpy as np

    big = np.zeros((1200, 1600, 3), dtype=np.uint8)
    assert decode_png(encode_png(big)).shape == (1200, 1600, 3)


def test_f08c_the_tripwire_names_the_limit_it_tripped():
    strict = DecodeLimits(max_pixels=1000, max_width=1024, max_height=1024)
    with pytest.raises(MediaLimitExceeded) as caught:
        decode_png(_png(200, 200), limits=strict)
    assert caught.value.details


def test_f08c2_a_non_positive_limit_is_refused_at_construction():
    with pytest.raises(MediaLimitExceeded):
        DecodeLimits(max_pixels=0)


def test_f08d_a_truncated_png_is_a_typed_refusal():
    with pytest.raises(MediaUnsupported):
        decode_png(b"\x89PNG\r\n\x1a\n")


def test_f08e_a_non_png_is_refused_by_magic_not_by_extension(tmp_path):
    fake = tmp_path / "image.png"
    fake.write_bytes(b"<?php echo 1; ?>")
    with pytest.raises(MediaUnsupported):
        load_rgb(fake)


def test_f08f_a_long_audio_header_cannot_exceed_the_frame_budget():
    bomb = _wav(48_000, 8, 4_000_000)
    with pytest.raises(MediaLimitExceeded):
        decode_wav(bomb)


def test_f08g_round_trip_inside_the_limits_still_works():
    import numpy as np

    rgb = np.zeros((32, 48, 3), dtype=np.uint8)
    rgb[:, :, 0] = 200
    assert decode_png(encode_png(rgb)).shape == (32, 48, 3)
    tone = np.zeros(1600, dtype=np.float64)
    samples, rate = decode_wav(encode_wav(tone, sample_rate=16000))
    assert samples.size == tone.size
    assert rate == 16000


def test_default_limits_are_stated_as_numbers():
    assert DEFAULT_LIMITS.max_pixels >= 1_000_000
    assert DEFAULT_LIMITS.max_width >= 1024 and DEFAULT_LIMITS.max_height >= 1024
    assert DEFAULT_LIMITS.max_audio_frames >= 16000
    # the defaults are small enough to refuse a 200 MP header
    with pytest.raises(MediaLimitExceeded):
        decode_png(_png(20000, 20000))
