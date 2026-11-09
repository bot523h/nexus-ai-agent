"""Canonical hashing for the Character Capsule island.

Why this module exists
----------------------
A character capsule is only as trustworthy as the digests that bind it. Two
requirements follow, and both are structural rather than stylistic:

1. **One spelling.** The digest of a capsule must not depend on how the payload
   happened to be serialised, so all hashing goes through one canonical JSON
   spelling. When the NEXUS package is importable the repository's own
   implementation (``nexus_ai_agent.provenance.models.canonical_json``) is used
   directly — the prototype must not invent a second dialect (see
   ``tests/character_capsule/test_canonical.py``, which asserts equality against
   the repository function when it is present).
2. **Domain separation.** A face-module digest, a capsule digest and a blob
   digest must never be interchangeable. Every digest here is computed over
   ``<domain>/<scope>\\n<canonical-json>`` so that confusing one for another is a
   structural impossibility rather than a convention.

The module is deliberately dependency-free (stdlib only) so it can run in the
pre-install fast rail.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

#: Root domain for every digest produced by this island.
DOMAIN: str = "nagar.character-capsule.v1"

#: Scope tags — one per digest family. Each becomes part of the hashed bytes.
SCOPE_CAPSULE: str = "capsule"
SCOPE_IDENTITY: str = "identity"
SCOPE_MODULE: str = "module"
SCOPE_REVISION: str = "revision"
SCOPE_SHOT: str = "shot"
SCOPE_SCENE: str = "scene"
SCOPE_LINEAGE: str = "lineage"
SCOPE_CALIBRATION: str = "calibration"
SCOPE_BENCHMARK: str = "benchmark"
SCOPE_EVIDENCE: str = "evidence"
SCOPE_CONSENT_REVOCATION: str = "consent-revocation"


def local_canonical_json(obj: Any) -> str:
    """Canonical JSON: sorted keys, compact separators, non-ASCII preserved.

    Byte-identical to ``nexus_ai_agent.provenance.models.canonical_json``; kept
    here so the island still works when the NEXUS package is not importable
    (bare ``PYTHONPATH=experiments`` runs, early CI rails).
    """
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def canonical_json(obj: Any) -> str:
    """Canonical JSON, preferring the repository's implementation when present."""
    try:  # pragma: no branch - the import result is stable per interpreter
        from nexus_ai_agent.provenance.models import canonical_json as nexus_canonical_json
    except Exception:  # noqa: BLE001 - optional reuse, never a hard dependency
        return local_canonical_json(obj)
    return str(nexus_canonical_json(obj))


def sha256_hex(data: bytes) -> str:
    """``sha256:<hex>`` of raw bytes — the repository's digest spelling."""
    return "sha256:" + hashlib.sha256(data).hexdigest()


def digest_of(obj: Any) -> str:
    """Content digest of a JSON-able value under the canonical spelling."""
    return sha256_hex(canonical_json(obj).encode("utf-8"))


def scoped_digest(scope: str, payload: Any) -> str:
    """Domain-separated digest: ``sha256(DOMAIN "/" scope "\\n" canonical_json)``.

    ``scope`` is one of the ``SCOPE_*`` constants. Passing an unknown scope is a
    programming error and raises ``ValueError`` rather than silently widening
    the digest namespace.
    """
    allowed = {
        SCOPE_CAPSULE,
        SCOPE_IDENTITY,
        SCOPE_MODULE,
        SCOPE_REVISION,
        SCOPE_SHOT,
        SCOPE_SCENE,
        SCOPE_LINEAGE,
        SCOPE_CALIBRATION,
        SCOPE_BENCHMARK,
        SCOPE_EVIDENCE,
        SCOPE_CONSENT_REVOCATION,
    }
    if scope not in allowed:
        raise ValueError(f"unknown digest scope: {scope!r}")
    material = f"{DOMAIN}/{scope}\n{canonical_json(payload)}".encode()
    return sha256_hex(material)


def short(digest: str, length: int = 12) -> str:
    """Human-facing abbreviation of a digest (never used for comparisons)."""
    body = digest.split(":", 1)[-1]
    return body[:length]
