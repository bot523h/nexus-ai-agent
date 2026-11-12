"""Ed25519 **signing**, built on the repository's pinned verification module.

Why this module exists (and why it is not a second crypto implementation)
-------------------------------------------------------------------------
:mod:`nexus_ai_agent.creative.packs.ed25519` ships verification only, on
purpose: the pack trust root must be able to *check* authority in an
environment that installs no crypto wheel, and it must never be able to
*mint* it.  Artifact passports need the other half — an artifact passport that
nobody can sign is not evidence of anything, only a JSON file.

So this module adds exactly the two functions verification cannot express:

* :func:`derive_public_key` — ``A = [s]B`` from a 32-byte seed (RFC 8032 §5.1.5);
* :func:`sign_ed25519` — ``R = [r]B``, ``S = (r + H(R,A,M)·s) mod L`` (§5.1.6).

The curve arithmetic, encodings and nonce/expansion rules are **reused** from
the verification module (single source of truth for the group law), and every
signature this module produces is checked by that module's verifier before it is
returned — a signer that emits signatures its own verifier rejects is worse than
no signer at all.

Key handling rules:

* a key is passed in explicitly (32-byte seed); nothing is read from the
  environment here, and nothing is ever logged;
* :func:`key_id_for` derives a *stable, public* key id from the public key, so a
  keyring entry can be looked up without trusting a caller-supplied label.
"""

from __future__ import annotations

import hashlib

from nexus_ai_agent.creative.packs.ed25519 import (
    _B,
    _L,
    _P,
    Ed25519Error,
    _decode_point,
    _point_add,
    _scalar_mult,
    verify_ed25519,
)

__all__ = [
    "Ed25519SigningError",
    "derive_public_key",
    "sign_ed25519",
    "key_id_for",
]

SEED_BYTES = 32


class Ed25519SigningError(ValueError):
    """A key or message could not be signed (fail-closed)."""


def _encode_point(point: tuple[int, int, int, int]) -> bytes:
    """Affine encode ``(y, sign(x))`` per RFC 8032 §5.1.2 (canonical)."""
    x, y, z, _ = point
    zinv = pow(z, _P - 2, _P)
    x = (x * zinv) % _P
    y = (y * zinv) % _P
    encoded = bytearray(y.to_bytes(32, "little"))
    encoded[31] |= (x & 1) << 7
    return bytes(encoded)


def _expand_seed(seed: bytes) -> tuple[int, bytes]:
    """RFC 8032 §5.1.5: ``h = SHA512(seed)``; ``s`` clamped, ``prefix`` = h[32:]."""
    digest = hashlib.sha512(seed).digest()
    scalar_bytes = bytearray(digest[:32])
    scalar_bytes[0] &= 248
    scalar_bytes[31] &= 127
    scalar_bytes[31] |= 64
    return int.from_bytes(scalar_bytes, "little"), digest[32:]


def derive_public_key(seed: bytes) -> bytes:
    """``A = [s]B`` encoded canonically (32 bytes)."""
    if not isinstance(seed, (bytes, bytearray)) or len(seed) != SEED_BYTES:
        raise Ed25519SigningError(f"an Ed25519 seed must be exactly {SEED_BYTES} bytes")
    scalar, _ = _expand_seed(bytes(seed))
    if scalar % _L == 0:  # pragma: no cover - astronomically unlikely
        raise Ed25519SigningError("seed yields a zero scalar; refusing to sign")
    public = _encode_point(_scalar_mult(_B, scalar))
    if _decode_point(public) is None:  # pragma: no cover - encode() is canonical
        raise Ed25519SigningError("derived public key is not a canonical point")
    return public


def sign_ed25519(seed: bytes, message: bytes) -> tuple[str, str]:
    """Sign ``message``; return ``(signature_hex, public_key_hex)``.

    The returned signature is verified with the repository's verifier before it
    is handed back, so the signer can never emit a signature the trust plane
    would reject.
    """
    if not isinstance(message, (bytes, bytearray)):
        raise Ed25519SigningError("message must be bytes")
    public = derive_public_key(seed)
    scalar, prefix = _expand_seed(bytes(seed))
    nonce = int.from_bytes(hashlib.sha512(prefix + bytes(message)).digest(), "little") % _L
    encoded_r = _encode_point(_scalar_mult(_B, nonce))
    challenge = (
        int.from_bytes(
            hashlib.sha512(encoded_r + public + bytes(message)).digest(), "little"
        )
        % _L
    )
    s = (nonce + challenge * scalar) % _L
    signature = encoded_r + s.to_bytes(32, "little")
    try:
        verify_ed25519(public_key=public, message=bytes(message), signature=signature)
    except Ed25519Error as error:  # pragma: no cover - would be a crypto bug
        raise Ed25519SigningError(
            f"produced a signature its own verifier rejects: {error}"
        ) from error
    return signature.hex(), public.hex()


def key_id_for(public_key: bytes, *, prefix: str = "ed25519") -> str:
    """A stable public key id: ``ed25519:<first 16 hex of sha256(pubkey)>``."""
    if len(public_key) != 32:
        raise Ed25519SigningError("public key must be 32 bytes")
    digest = hashlib.sha256(public_key).hexdigest()[:16]
    return f"{prefix}:{digest}"


# ``_point_add`` is imported to document that the group law is shared, not
# re-implemented; the signer only needs scalar multiplication.
_ = _point_add
