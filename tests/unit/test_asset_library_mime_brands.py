"""ISO-BMFF ``ftyp`` brand allowlist for the Asset Library MIME classifier.

An ``ftyp`` box only says "this is an ISO base media file"; it does not say the
file is MP4 video.  HEIC/AVIF stills, QuickTime movies and 3GPP files share the
same container signature.  The closed allowlist must therefore be decided by the
*major brand* (bytes 8..12), checked against the MP4 Registration Authority
registry (https://mp4ra.org/registered-types/brands), not by ``ftyp`` alone.

Each case builds a structurally valid ``ftyp`` header: 4-byte big-endian box
size, ``ftyp``, 4-byte major brand, 4-byte minor version, then compatible brands.
"""

from __future__ import annotations

import struct

import pytest

from nexus_ai_agent.creative.assets.mime import HEADER_BYTES, detect_media_type
from nexus_ai_agent.creative.assets.models import SUPPORTED_MEDIA_TYPES


def _ftyp(major: bytes, *compatible: bytes, minor: int = 0) -> bytes:
    assert len(major) == 4 and all(len(c) == 4 for c in compatible)
    body = major + struct.pack(">I", minor) + b"".join(compatible)
    return (struct.pack(">I", 8 + len(body)) + b"ftyp" + body).ljust(HEADER_BYTES, b"\x00")


# Major brands that name an MP4 / ISO-base-media *video* file (MP4RA registry).
MP4_MAJOR_BRANDS = [b"isom", b"iso2", b"iso4", b"iso5", b"iso6", b"mp41", b"mp42", b"avc1", b"M4V "]

# Registered ISO-BMFF brands that are NOT MP4 video; V1 does not list them.
NON_MP4_MAJOR_BRANDS = [
    (b"heic", "HEIF still image (HEVC)"),
    (b"heix", "HEIF still image (HEVC, extended)"),
    (b"mif1", "HEIF still image (generic)"),
    (b"msf1", "HEIF image sequence"),
    (b"avif", "AVIF still image"),
    (b"avis", "AVIF image sequence"),
    (b"qt  ", "QuickTime movie"),
    (b"3gp4", "3GPP release 4"),
    (b"3gp5", "3GPP release 5"),
    (b"3g2a", "3GPP2"),
    (b"M4A ", "iTunes AAC audio, not video"),
    (b"crx ", "Canon raw still"),
]


@pytest.mark.parametrize("brand", MP4_MAJOR_BRANDS, ids=lambda b: b.decode("latin-1").strip())
def test_mp4_major_brands_classify_as_mp4(brand: bytes) -> None:
    assert detect_media_type(_ftyp(brand, b"isom", b"mp42")) == "video/mp4"


@pytest.mark.parametrize(
    ("brand", "why"),
    NON_MP4_MAJOR_BRANDS,
    ids=lambda v: v.decode("latin-1").strip() if isinstance(v, bytes) else "",
)
def test_non_mp4_iso_bmff_brands_are_rejected(brand: bytes, why: str) -> None:
    assert detect_media_type(_ftyp(brand)) is None, why


def test_an_mp4_compatible_brand_does_not_rescue_a_non_mp4_major_brand() -> None:
    """A HEIC file may list ``isom``/``mp42`` as *compatible*; the major brand decides."""
    assert detect_media_type(_ftyp(b"heic", b"mif1", b"isom", b"mp42")) is None
    assert detect_media_type(_ftyp(b"qt  ", b"isom", b"mp42")) is None


def test_a_header_truncated_inside_the_major_brand_is_rejected() -> None:
    full = _ftyp(b"mp42")
    assert detect_media_type(full[:11]) is None  # ftyp present, brand cut short
    assert detect_media_type(full[:8]) is None
    assert detect_media_type(full[:12]) == "video/mp4"  # exactly through the brand


def test_the_ftyp_marker_must_sit_at_offset_four() -> None:
    assert detect_media_type(b"ftypmp42" + b"\x00" * 24) is None
    assert detect_media_type(b"\x00\x00\x00\x18XXXXmp42" + b"\x00" * 24) is None


def test_every_mp4_brand_maps_to_a_type_the_library_actually_allows() -> None:
    for brand in MP4_MAJOR_BRANDS:
        detected = detect_media_type(_ftyp(brand))
        assert detected in SUPPORTED_MEDIA_TYPES


def test_non_mp4_brands_never_reach_the_allowlist_through_any_signature() -> None:
    """Whatever the classifier returns for a rejected brand, it is never allow-listed."""
    for brand, _ in NON_MP4_MAJOR_BRANDS:
        assert detect_media_type(_ftyp(brand)) not in SUPPORTED_MEDIA_TYPES


@pytest.mark.parametrize("cut", range(0, 12))
def test_no_prefix_shorter_than_the_full_brand_is_ever_accepted(cut: int) -> None:
    """Every strict prefix of a valid MP4 header is rejected, so a missing or
    partial brand can never be read as an allowed one."""
    assert detect_media_type(_ftyp(b"mp42")[:cut]) is None


def test_an_ftyp_header_with_a_zero_filled_brand_is_rejected() -> None:
    assert detect_media_type(b"\x00\x00\x00\x10ftyp\x00\x00\x00\x00" + b"\x00" * 8) is None
