"""Canonical serialization + digests for the IBPC layer.

There is exactly **one** canonical JSON spelling in this repository —
``nexus_ai_agent.provenance.models.canonical_json`` (sort_keys, compact
separators, ``ensure_ascii=False``, NaN rejected). The IBPC layer reuses it
instead of defining a second one, so an IBPC digest and a provenance record
digest are computed by the same function and can never drift apart.
"""

from __future__ import annotations

from nexus_ai_agent.provenance.models import canonical_json, digest_of, sha256_hex

__all__ = ["canonical_json", "digest_of", "sha256_hex", "sha256_bytes"]


def sha256_bytes(data: bytes) -> str:
    """``sha256:<hex>`` of raw bytes (the repo's digest spelling)."""
    return sha256_hex(data)
