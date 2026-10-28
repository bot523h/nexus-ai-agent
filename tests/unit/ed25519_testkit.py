"""Ephemeral Ed25519 **signing** for tests only — never imported by production.

Deliberate properties (the hostile-audit rule "private trust material must not
be derivable from repository source"):

* keys are generated from :func:`os.urandom`; there is no fixed seed, no
  derivation recipe and no checked-in private key anywhere in this repository;
* this module lives under ``tests/`` and is not importable from the
  ``nexus_ai_agent`` package — ``tests/architecture/test_pack_trust_boundary.py``
  pins that the shipped package contains no signing primitive at all;
* a key produced here can only ever become trusted if a test explicitly writes
  its *public* half into a temporary trust root.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass

from nexus_ai_agent.creative.packs.ed25519 import _B, _L, _P, _point_add, _scalar_mult


def _encode_point(p: tuple[int, int, int, int]) -> bytes:
    x, y, z, _ = p
    zinv = pow(z, _P - 2, _P)
    x = (x * zinv) % _P
    y = (y * zinv) % _P
    return int.to_bytes(y | ((x & 1) << 255), 32, "little")


@dataclass(frozen=True)
class TestKeyPair:
    seed: bytes
    public_key: bytes

    @property
    def public_hex(self) -> str:
        return self.public_key.hex()


def generate_keypair() -> TestKeyPair:
    """A fresh random Ed25519 key pair (test scope only)."""
    seed = os.urandom(32)
    h = hashlib.sha512(seed).digest()
    a = _clamp(h[:32])
    return TestKeyPair(seed=seed, public_key=_encode_point(_scalar_mult(_B, a)))


def _clamp(raw: bytes) -> int:
    a = int.from_bytes(raw, "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return a


def sign(keypair: TestKeyPair, message: bytes) -> bytes:
    h = hashlib.sha512(keypair.seed).digest()
    a = _clamp(h[:32])
    r = int.from_bytes(hashlib.sha512(h[32:] + message).digest(), "little") % _L
    big_r = _encode_point(_scalar_mult(_B, r))
    k = int.from_bytes(hashlib.sha512(big_r + keypair.public_key + message).digest(), "little") % _L
    s = (r + k * a) % _L
    return big_r + int.to_bytes(s, 32, "little")


def malleate(signature: bytes) -> bytes:
    """Return the ``S + L`` variant — a second signature RFC 8032 must reject."""
    s = int.from_bytes(signature[32:], "little") + _L
    return signature[:32] + int.to_bytes(s, 32, "little")


#: Encoding of the curve's identity point — a small-order public key.
IDENTITY_PUBLIC_KEY = bytes.fromhex(
    "0100000000000000000000000000000000000000000000000000000000000000"
)


def forge_for_identity_key(scalar: int = 12345) -> bytes:
    """A signature that satisfies ``[S]B = R + [k]A`` for the identity key A.

    With ``A`` the identity, ``[k]A`` is the identity for every message, so
    ``R = [S]B`` makes the verification equation hold for **any** message.  A
    verifier that does not reject small-order public keys therefore accepts
    attacker-chosen signatures over attacker-chosen messages.
    """
    s = scalar % _L
    return _encode_point(_scalar_mult(_B, s)) + int.to_bytes(s, 32, "little")


__all__ = [
    "TestKeyPair",
    "generate_keypair",
    "sign",
    "malleate",
    "forge_for_identity_key",
    "IDENTITY_PUBLIC_KEY",
    "_point_add",
]
