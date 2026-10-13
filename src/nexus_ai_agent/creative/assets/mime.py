"""Content-based media classification (magic bytes), stdlib only.

The caller's filename, extension and claimed MIME type are untrusted and never
reach this module.  Classification is derived solely from the leading bytes of
the stream, and only the types in :data:`SUPPORTED_MEDIA_TYPES` are accepted.

This is a *classification* check, not an antivirus: the library never executes,
extracts or renders the uploaded bytes, so a passing magic-byte check is not a
claim of safety for arbitrary content.
"""

from __future__ import annotations

#: How many leading bytes are needed to classify every supported container.
HEADER_BYTES = 64

#: ISO-BMFF ``ftyp`` major brands accepted as ``video/mp4``.  Source: the MP4
#: Registration Authority brand registry (https://mp4ra.org/registered-types/brands):
#: ``isom``/``iso2``..``iso9`` (ISO base media, MP4 family), ``mp41``/``mp42``
#: (MP4 v1/v2), ``avc1`` (AVC extensions), ``M4V `` (iTunes video) and ``dash``.
#: Deliberately absent: ``heic``/``heix``/``mif1`` (HEIF), ``avif``/``avis`` (AVIF),
#: ``qt  `` (QuickTime), ``3gp*``/``3g2*`` (3GPP) and ``M4A `` (audio) - none is
#: listed in V1's closed allowlist.  Adding one is an explicit, tested decision.
MP4_MAJOR_BRANDS: frozenset[bytes] = frozenset(
    {
        b"isom",
        b"iso2",
        b"iso3",
        b"iso4",
        b"iso5",
        b"iso6",
        b"iso7",
        b"iso8",
        b"iso9",
        b"mp41",
        b"mp42",
        b"avc1",
        b"M4V ",
        b"dash",
    }
)


def detect_media_type(header: bytes) -> str | None:
    """Return the canonical media type for ``header``, or ``None`` if unknown.

    ``header`` must be the first :data:`HEADER_BYTES` (or fewer) bytes of the
    stream.  Unknown or truncated input returns ``None``; the caller maps that
    to :class:`~nexus_ai_agent.creative.assets.errors.UnsupportedMediaType`.
    """
    if not header:
        return None

    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(header) >= 12 and header.startswith(b"RIFF"):
        container = header[8:12]
        if container == b"WEBP":
            return "image/webp"
        if container == b"WAVE":
            return "audio/wav"
        return None
    if header.startswith(b"ID3"):
        return "audio/mpeg"
    if len(header) >= 2 and header[0] == 0xFF and (header[1] & 0xE0) == 0xE0:
        # MPEG-1/2 audio frame sync (0xFFE0 mask); the third byte distinguishes
        # MP3 from other MPEG streams and is not required for the allowlist.
        return "audio/mpeg"
    if len(header) >= 12 and header[4:8] == b"ftyp":
        # ``ftyp`` only identifies an ISO base media file.  HEIC/AVIF stills,
        # QuickTime and 3GPP share it, so the *major brand* decides, never the
        # compatible-brands list (a HEIC file may list ``mp42`` as compatible).
        return "video/mp4" if header[8:12] in MP4_MAJOR_BRANDS else None
    if header.startswith(b"\x1aE\xdf\xa3"):
        # EBML container: Matroska/WebM.  V1 accepts the web profile.
        return "video/webm"
    return None
