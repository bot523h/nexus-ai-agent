"""Versioned provenance digest verification with explicit legacy migration mode."""

from __future__ import annotations

import hashlib
import hmac
import json
from enum import Enum
from typing import Any

from nexus_ai_agent.provenance.jcs import (
    JcsError,
    jcs_digest,
)
from nexus_ai_agent.provenance.jcs import (
    canonical_bytes as _jcs_canonical_bytes,
)


class CanonicalizationScheme(str, Enum):
    """Digest formats carried by persisted provenance records."""

    LEGACY_JSON_V1 = "legacy_json_v1"
    JCS_RFC8785_V2 = "jcs_rfc8785_v2"


def canonical_bytes(payload: Any, scheme: CanonicalizationScheme) -> bytes:
    """Return the canonical bytes for a versioned scheme (RFC 8785 for JCS)."""
    if scheme is CanonicalizationScheme.JCS_RFC8785_V2:
        return _jcs_canonical_bytes(payload)
    if scheme is CanonicalizationScheme.LEGACY_JSON_V1:
        return json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    raise ValueError(f"unsupported canonicalization scheme: {scheme!r}")


def canonical_digest(payload: Any, scheme: CanonicalizationScheme) -> str:
    """Return the sha256-prefixed digest for exactly the selected scheme."""
    if scheme is CanonicalizationScheme.LEGACY_JSON_V1:
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return "sha256:" + hashlib.sha256(encoded).hexdigest()
    if scheme is CanonicalizationScheme.JCS_RFC8785_V2:
        return jcs_digest(payload)
    raise ValueError(f"unsupported canonicalization scheme: {scheme!r}")


def verify_canonical_digest(
    payload: Any,
    expected_digest: str,
    scheme: CanonicalizationScheme | None = None,
    *,
    migration_mode: bool = False,
) -> bool:
    """Verify strictly by scheme, or explicitly try both during migration.

    An absent scheme is not permission to guess unless ``migration_mode=True``.
    """
    candidates: tuple[CanonicalizationScheme, ...]
    if scheme is not None:
        candidates = (scheme,)
    elif migration_mode:
        candidates = (
            CanonicalizationScheme.JCS_RFC8785_V2,
            CanonicalizationScheme.LEGACY_JSON_V1,
        )
    else:
        candidates = (CanonicalizationScheme.LEGACY_JSON_V1,)

    for candidate in candidates:
        try:
            actual = canonical_digest(payload, candidate)
        except (JcsError, TypeError, ValueError):
            if scheme is not None:
                raise
            continue
        if hmac.compare_digest(actual, expected_digest):
            return True
    return False


__all__ = ["CanonicalizationScheme", "canonical_digest", "verify_canonical_digest"]
