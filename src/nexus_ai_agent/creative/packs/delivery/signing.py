"""Ed25519 manifest signing for the delivery pack (wave-4 step2).

The delivery pack (``nexus.color.delivery``) emits an ``export_otio``
manifest that is consumed by external tools.  The pack's manifest today
verifies as ``format_only_unverified`` — this module closes the loop
with a typed, fail-closed signing seam.

Design
------
* **Zero new hard dependency.**  When PyNaCl (``nacl.signing``) is
  installed the module uses Ed25519.  When it is absent it falls back to
  HMAC-SHA256 over the *same* key material and *same* canonicalisation —
  every test is green without PyNaCl, and an installed PyNaCl is
  exercised without any code change (the fallback is byte-for-byte
  compatible with the Ed25519 path's contract: ``sign`` → base64,
  ``verify`` → constant-time compare, tamper → :class:`SigningError`).
* **Fail-closed.**  No key in ``NEXUS_SIGNING_KEY`` → :class:`SigningError`
  with an actionable message (``generate`` CLI hint).  Empty or
  whitespace-only keys are treated as missing.
* **Canonicalisation before every sign/verify.**  ``dict`` manifests are
  serialised with ``sort_keys=True, separators=(",", ":"), ensure_ascii=False``
  and UTF-8 — JSON object key order never affects a signature and an
  attacker cannot smuggle a duplicate key.
* **Transport is base64.**  Signatures are ``base64.b64encode`` strings,
  safe for JSON and for ``X-NEXUS-Signature``-style headers.
* **Constant-time verification.**  ``hmac.compare_digest`` (HMAC fallback)
  or ``VerifyKey.verify`` (Ed25519) — no early exit.

Environment
-----------
``NEXUS_SIGNING_KEY`` — hex-encoded 32-byte seed (64 hex characters).
Generate with::

    python -c "import secrets; print(secrets.token_hex(32))"

The same env var is read for signing *and* verification so a single
deployment secret suffices; rotation is “new value in env”.

Security notes
--------------
* The private key never leaves this module except as a return value from
  :func:`get_signing_key_bytes` (for tests).  It is not logged, not
  included in exceptions, and not written to disk.
* Signatures are deterministic for a given manifest + key (Ed25519 and
  HMAC-SHA256 are deterministic) — no nonce to manage.

This module is the spike referenced in task-110/task-112; the real
delivery pipeline will call :func:`sign_manifest` after ``export_otio``
and the consumer will call :func:`verify_manifest` before import.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from typing import Any

# Optional Ed25519 backend — PyNaCl is the documented choice (BSD, wheel).
# We probe at import time so the module remains importable without it.
try:  # pragma: no cover — exercised when PyNaCl is installed
    from nacl.exceptions import BadSignatureError as _NaclBadSig
    from nacl.signing import SigningKey as _NaclSigningKey
    from nacl.signing import VerifyKey as _NaclVerifyKey

    _HAVE_NACL = True
except Exception:  # pragma: no cover
    _NaclBadSig = Exception
    _NaclSigningKey = None
    _NaclVerifyKey = None
    _HAVE_NACL = False


class SigningError(RuntimeError):
    """Typed failure for every signing/verification error (fail-closed)."""


def _load_key_bytes() -> bytes:
    raw = os.environ.get("NEXUS_SIGNING_KEY", "").strip()
    if not raw:
        raise SigningError(
            "NEXUS_SIGNING_KEY is not set — refusing to sign/verify. "
            'Generate one with: python -c "import secrets; print(secrets.token_hex(32))" '
            "and export NEXUS_SIGNING_KEY=<hex> before calling sign/verify."
        )
    # Allow hex (64 chars) or base64; hex is the documented form.
    try:
        # Hex is strictly 64 hex chars → 32 bytes
        if all(c in "0123456789abcdefABCDEF" for c in raw) and len(raw) == 64:
            return bytes.fromhex(raw)
        # Fallback: base64
        padded = raw + "=" * (-len(raw) % 4)
        return base64.b64decode(padded, validate=True)
    except Exception as exc:  # pragma: no cover — defensive
        raise SigningError(f"NEXUS_SIGNING_KEY is malformed: {exc}") from exc


def _canonical_bytes(manifest: dict[str, Any] | bytes | str) -> bytes:
    if isinstance(manifest, bytes):
        return manifest
    if isinstance(manifest, str):
        return manifest.encode("utf-8")
    # dict → canonical JSON
    return json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def _sign_with_nacl(canonical: bytes, seed: bytes) -> str:
    assert _NaclSigningKey is not None
    sk = _NaclSigningKey(seed)
    sig = sk.sign(canonical).signature
    return base64.b64encode(sig).decode("ascii")


def _verify_with_nacl(canonical: bytes, signature_b64: str, seed: bytes) -> None:
    assert _NaclSigningKey is not None and _NaclVerifyKey is not None
    try:
        sig = base64.b64decode(signature_b64, validate=True)
    except Exception as exc:
        raise SigningError(f"signature is not valid base64: {exc}") from exc
    sk = _NaclSigningKey(seed)
    vk: Any = sk.verify_key
    # nacl's VerifyKey.verify expects signature + message concatenated
    try:
        vk.verify(canonical, sig)
    except _NaclBadSig as exc:
        raise SigningError("signature verification failed — manifest was tampered") from exc


def _sign_with_hmac(canonical: bytes, key: bytes) -> str:
    digest = hmac.new(key, canonical, hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def _verify_with_hmac(canonical: bytes, signature_b64: str, key: bytes) -> None:
    try:
        expected = base64.b64decode(signature_b64, validate=True)
    except Exception as exc:
        raise SigningError(f"signature is not valid base64: {exc}") from exc
    digest = hmac.new(key, canonical, hashlib.sha256).digest()
    if not hmac.compare_digest(digest, expected):
        raise SigningError("signature verification failed — manifest was tampered")


def get_signing_key_bytes() -> bytes:
    """Return the raw key bytes (for tests)."""
    return _load_key_bytes()


def sign_manifest(manifest: dict[str, Any] | bytes | str) -> str:
    """Sign a manifest and return a base64 signature string.

    Raises :class:`SigningError` when the key is missing or malformed.
    """
    key = _load_key_bytes()
    canonical = _canonical_bytes(manifest)
    if _HAVE_NACL:
        return _sign_with_nacl(canonical, key)
    return _sign_with_hmac(canonical, key)


def verify_manifest(manifest: dict[str, Any] | bytes | str, signature: str) -> None:
    """Verify a manifest against a base64 signature.

    Returns ``None`` on success, raises :class:`SigningError` on failure
    (missing key, bad base64, or tamper).  The caller should treat *any*
    exception as “reject”.
    """
    key = _load_key_bytes()
    canonical = _canonical_bytes(manifest)
    if _HAVE_NACL:
        _verify_with_nacl(canonical, signature, key)
        return
    _verify_with_hmac(canonical, signature, key)


@dataclass(frozen=True)
class SignedManifest:
    manifest: dict[str, Any]
    signature: str

    def verify(self) -> None:
        verify_manifest(self.manifest, self.signature)
