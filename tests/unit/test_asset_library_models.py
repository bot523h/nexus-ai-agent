"""Unit tests for the Asset Library domain contracts and content classification.

The classification tests prove that the accepted media type is derived from the
*bytes*, never from a caller-supplied filename, extension or MIME claim.
"""

from __future__ import annotations

import pytest

from nexus_ai_agent.creative.assets.mime import HEADER_BYTES, detect_media_type
from nexus_ai_agent.creative.assets.models import (
    SUPPORTED_MEDIA_TYPES,
    IngestRequest,
    MediaKind,
)

#: Minimal, header-only fixtures: the library classifies by magic bytes and does
#: not decode media, so a valid signature prefix is the whole contract here.
FIXTURES = {
    "image/png": b"\x89PNG\r\n\x1a\n" + b"\x00" * 32,
    "image/jpeg": b"\xff\xd8\xff\xe0" + b"\x00" * 32,
    "image/webp": b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 32,
    "audio/wav": b"RIFF\x00\x00\x00\x00WAVEfmt " + b"\x00" * 32,
    "audio/mpeg": b"ID3\x04\x00\x00" + b"\x00" * 32,
    "video/mp4": b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 32,
    "video/webm": b"\x1aE\xdf\xa3" + b"\x00" * 32,
}


@pytest.mark.parametrize("media_type,header", sorted(FIXTURES.items()))
def test_supported_media_is_classified_from_bytes(media_type: str, header: bytes) -> None:
    assert detect_media_type(header[:HEADER_BYTES]) == media_type
    assert media_type in SUPPORTED_MEDIA_TYPES


def test_unknown_and_empty_input_is_not_classified() -> None:
    assert detect_media_type(b"") is None
    assert detect_media_type(b"GIF89a" + b"\x00" * 16) is None  # gif is not allow-listed
    assert detect_media_type(b"PK\x03\x04" + b"\x00" * 16) is None  # a zip/archive
    assert detect_media_type(b"#!/bin/sh\n") is None


def test_extension_and_claimed_type_do_not_change_classification() -> None:
    # A shell script named "photo.png" with a claimed image type is still unknown.
    request = IngestRequest(
        idempotency_key="k1",
        original_filename="../../etc/photo.png",
        declared_media_type="image/png",
    )
    assert detect_media_type(b"#!/bin/sh\nrm -rf /\n") is None
    # The request keeps the filename as display metadata only.
    assert request.original_filename == "../../etc/photo.png"


def test_supported_media_types_are_closed_and_typed() -> None:
    assert set(SUPPORTED_MEDIA_TYPES) == set(FIXTURES)
    assert all(isinstance(kind, MediaKind) for kind in SUPPORTED_MEDIA_TYPES.values())
