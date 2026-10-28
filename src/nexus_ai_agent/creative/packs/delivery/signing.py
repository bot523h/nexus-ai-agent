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
# We probe at import time so the module remains importable without it.  The
# public names are deliberately ``Any``-typed: tests may inject a faithful
# backend double, and the fallback assigns ``None``/``Exception``.
_NaclBadSig: Any = Exception
_NaclSigningKey: Any = None
_NaclVerifyKey: Any = None
_HAVE_NACL = False
try:  # pragma: no cover — exercised when PyNaCl is installed
    from nacl.exceptions import BadSignatureError as _bad_sig
    from nacl.signing import SigningKey as _signing_key
    from nacl.signing import VerifyKey as _verify_key
except Exception:  # pragma: no cover
    pass
else:
    _NaclBadSig = _bad_sig
    _NaclSigningKey = _signing_key
    _NaclVerifyKey = _verify_key
    _HAVE_NACL = True


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
    # dict → canonical JSON.  json.dumps can raise TypeError/ValueError on an
    # unsupported value; the public contract is SigningError on every failure.
    try:
        return json.dumps(
            manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
    except Exception as exc:
        raise SigningError(f"manifest cannot be canonicalised: {exc}") from exc


def _seed_key(seed: bytes) -> Any:
    """Build the nacl signing key, mapping every failure to a typed error.

    PyNaCl rejects a seed that is not exactly 32 bytes with a bare
    ``ValueError``; the module contract is a :class:`SigningError` on *every*
    failure, so the raw exception must not escape to the caller.
    """
    assert _NaclSigningKey is not None
    try:
        return _NaclSigningKey(seed)
    except SigningError:
        raise
    except Exception as exc:
        raise SigningError(
            f"NEXUS_SIGNING_KEY is not usable by the Ed25519 backend: {exc}"
        ) from exc


def _sign_with_nacl(canonical: bytes, seed: bytes) -> str:
    sk = _seed_key(seed)
    try:
        sig = sk.sign(canonical).signature
        return base64.b64encode(sig).decode("ascii")
    except SigningError:
        raise
    except Exception as exc:
        raise SigningError(f"Ed25519 signing failed: {exc}") from exc


def _verify_with_nacl(canonical: bytes, signature_b64: str, seed: bytes) -> None:
    assert _NaclVerifyKey is not None
    try:
        sig = base64.b64decode(signature_b64, validate=True)
    except Exception as exc:
        raise SigningError(f"signature is not valid base64: {exc}") from exc
    # nacl's VerifyKey.verify expects signature + message concatenated.  Real
    # PyNaCl raises a bare ValueError for malformed signature lengths and
    # BadSignatureError for a 64-byte forgery; both are typed rejections.
    try:
        vk: Any = _seed_key(seed).verify_key
        vk.verify(canonical, sig)
    except SigningError:
        raise
    except _NaclBadSig as exc:
        raise SigningError("signature verification failed — manifest was tampered") from exc
    except Exception as exc:
        raise SigningError(f"not a valid Ed25519 signature: {exc}") from exc


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
    try:
        if _HAVE_NACL:
            return _sign_with_nacl(canonical, key)
        return _sign_with_hmac(canonical, key)
    except SigningError:
        raise
    except Exception as exc:  # pragma: no cover — last-resort contract fence
        raise SigningError(f"signing backend failed: {exc}") from exc


def verify_manifest(manifest: dict[str, Any] | bytes | str, signature: str) -> None:
    """Verify a manifest against a base64 signature.

    Returns ``None`` on success, raises :class:`SigningError` on failure
    (missing key, bad base64, or tamper).  The caller should treat *any*
    exception as “reject”.
    """
    key = _load_key_bytes()
    canonical = _canonical_bytes(manifest)
    try:
        if _HAVE_NACL:
            _verify_with_nacl(canonical, signature, key)
        else:
            _verify_with_hmac(canonical, signature, key)
    except SigningError:
        raise
    except Exception as exc:  # pragma: no cover — last-resort contract fence
        raise SigningError(f"verification backend failed: {exc}") from exc


@dataclass(frozen=True)
class SignedManifest:
    manifest: dict[str, Any]
    signature: str

    def verify(self) -> None:
        verify_manifest(self.manifest, self.signature)
