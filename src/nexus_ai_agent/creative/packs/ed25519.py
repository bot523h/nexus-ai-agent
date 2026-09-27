"""Pure-Python Ed25519 (RFC 8032) — minimal, stdlib-only, no nacl.

Ported from Daniel J. Bernstein's reference ``ed25519.py`` (public domain)
to Python 3. The implementation is deliberately small, deterministic and
auditable: it uses only ``hashlib`` and big integers. It is *not* constant-time
and must not be used with secret keys in a production signer that is exposed to
timing attacks — here it only *verifies* manifests whose keys are public.

Tested against RFC 8032 §7.1 vectors (see ``tests/unit/test_manifest_signature.py``).

Original: https://ed25519.cr.yp.to/python/ed25519.py
License: public domain.
"""
from __future__ import annotations

import hashlib

b: int = 256
q: int = 2**255 - 19
l: int = 2**252 + 27742317777372353535851937790883648493

def _H(m: bytes) -> bytes:
    return hashlib.sha512(m).digest()

def _expmod(b_: int, e: int, m: int) -> int:
    # pow with mod is built-in but keep for clarity and to mirror reference
    return pow(b_, e, m)

def _inv(x: int) -> int:
    return pow(x, q - 2, q)

d: int = -121665 * _inv(121666) % q
I: int = _expmod(2, (q - 1) // 4, q)

def _xrecover(y: int) -> int:
    xx = (y * y - 1) * _inv(d * y * y + 1) % q
    x = _expmod(xx, (q + 3) // 8, q)
    if (x * x - xx) % q != 0:
        x = (x * I) % q
    if x % 2 != 0:
        x = q - x
    return x

By: int = 4 * _inv(5) % q
Bx: int = _xrecover(By)
B = [Bx % q, By % q]  # base point

def _edwards(P: list[int], Q: list[int]) -> list[int]:
    x1, y1 = P
    x2, y2 = Q
    x3 = (x1 * y2 + x2 * y1) * _inv(1 + d * x1 * x2 * y1 * y2) % q
    y3 = (y1 * y2 + x1 * x2) * _inv(1 - d * x1 * x2 * y1 * y2) % q
    return [x3 % q, y3 % q]

def _scalarmult(P: list[int], e: int) -> list[int]:
    if e == 0:
        return [0, 1]
    Q = _scalarmult(P, e // 2)
    Q = _edwards(Q, Q)
    if e & 1:
        Q = _edwards(Q, P)
    return Q

def _encodeint(y: int) -> bytes:
    return y.to_bytes(b // 8, "little")

def _encodepoint(P: list[int]) -> bytes:
    x, y = P
    bits = y | ((x & 1) << 255)
    return bits.to_bytes(b // 8, "little")

def _bit(h: bytes, i: int) -> int:
    return (h[i // 8] >> (i % 8)) & 1

def publickey(sk: bytes) -> bytes:
    if len(sk) != b // 8:
        raise ValueError("bad secret key length")
    h = _H(sk)
    a = 2 ** (b - 2) + sum(2**i * _bit(h, i) for i in range(3, b - 2))
    A = _scalarmult(B, a)
    return _encodepoint(A)

def _Hint(m: bytes) -> int:
    h = _H(m)
    # interpret 64-byte hash as little-endian integer (512 bits)
    return int.from_bytes(h, "little")

def signature(m: bytes, sk: bytes, pk: bytes) -> bytes:
    h = _H(sk)
    a = 2 ** (b - 2) + sum(2**i * _bit(h, i) for i in range(3, b - 2))
    # r = Hint( second half of h || m )
    r = _Hint(h[b // 8 : b // 4] + m)
    r %= l
    R = _scalarmult(B, r)
    S = (r + _Hint(_encodepoint(R) + pk + m) * a) % l
    return _encodepoint(R) + _encodeint(S)

def _isoncurve(P: list[int]) -> bool:
    x, y = P
    return (-x * x + y * y - 1 - d * x * x * y * y) % q == 0

def _decodeint(s: bytes) -> int:
    return int.from_bytes(s, "little")

def _decodepoint(s: bytes) -> list[int]:
    if len(s) != b // 8:
        raise ValueError("bad point length")
    y = int.from_bytes(s, "little") & ((1 << 255) - 1)
    sign = (int.from_bytes(s, "little") >> 255) & 1
    x = _xrecover(y)
    if (x & 1) != sign:
        x = q - x
    P = [x, y]
    if not _isoncurve(P):
        raise ValueError("point not on curve")
    return P

def checkvalid(s: bytes, m: bytes, pk: bytes) -> bool:
    """Return True iff ``s`` is a valid signature for ``m`` under ``pk``."""
    try:
        if len(s) != b // 4:
            return False
        if len(pk) != b // 8:
            return False
        R = _decodepoint(s[: b // 8])
        A = _decodepoint(pk)
        S = _decodeint(s[b // 8 : b // 4])
        if S >= l:
            return False
        h = _Hint(_encodepoint(R) + pk + m)
        # Equation: S*B == R + h*A
        lhs = _scalarmult(B, S)
        rhs = _edwards(R, _scalarmult(A, h))
        return lhs == rhs
    except Exception:
        return False

def sign_message(sk: bytes, msg: bytes) -> bytes:
    """Convenience: derive pk and sign."""
    pk = publickey(sk)
    return signature(msg, sk, pk)

def verify_signature(pk: bytes, msg: bytes, sig: bytes) -> bool:
    return checkvalid(sig, msg, pk)

# --- helpers for manifest use ---

def keypair_from_seed(seed: bytes) -> tuple[bytes, bytes]:
    """Deterministic keypair from 32-byte seed (seed is the secret)."""
    if len(seed) != 32:
        raise ValueError("seed must be 32 bytes")
    pk = publickey(seed)
    return seed, pk

# Pre-generated test vectors: RFC 8032 example 1
# Secret: 9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60
# Public: d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a
# Message: "" (empty)
# Signature: e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e0652249015555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b

