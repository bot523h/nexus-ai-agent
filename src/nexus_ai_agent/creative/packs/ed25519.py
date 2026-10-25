"""Ed25519 **verification only** — RFC 8032, with pinned edge-case rules.

Why this module exists
----------------------
The pack trust root must be able to check a signature in every environment
this project ships to, including the minimal runtime image that installs no
cryptographic wheel.  The alternatives were worse:

* a mandatory binary dependency (``cryptography``/PyNaCl) in a local-first
  runtime that otherwise needs none, and that the pack substrate's import
  allow-list deliberately forbids;
* an optional backend — which means two implementations that can disagree on
  exactly the edge cases listed below, and a "library missing" path that tends
  to decay into "not verified, but allowed".

So there is **one** implementation, always active, with pinned semantics.  It
processes only *public* data (public key, message, signature): there is no
secret here to leak through timing, and **no signing function ships** — this
module can check authority, never create it.  Verification costs a few
milliseconds and happens at pack activation, not per operation.

Pinned semantics (RFC 8032 leaves choices open; ambiguity is a bug)
-------------------------------------------------------------------
1. ``S`` MUST be canonical: ``0 <= S < L``.  Without it, ``S + L`` is a second
   valid signature for the same message (malleability).
2. ``A`` and ``R`` MUST be canonically encoded points; non-canonical
   encodings are rejected.
3. Small-order ``A`` is rejected: such a key verifies "anything" for an
   attacker who knows the discrete log.
4. The cofactorless equation ``[S]B = R + [k]A`` is used, matching RFC 8032
   section 5.1.7 and what ``cryptography``/OpenSSL do — so both backends of
   this module accept exactly the same set of signatures.

References: RFC 8032; "Taming the many EdDSAs" (eprint 2020/1244).
"""

from __future__ import annotations

import hashlib

__all__ = ["Ed25519Error", "verify_ed25519", "BACKEND"]


class Ed25519Error(ValueError):
    """Any verification failure — malformed input included (fail-closed)."""


# --- curve constants -------------------------------------------------------
_P = 2**255 - 19
_L = 2**252 + 27742317777372353535851937790883648493
_D = (-121665 * pow(121666, _P - 2, _P)) % _P
_I = pow(2, (_P - 1) // 4, _P)
_BY = (4 * pow(5, _P - 2, _P)) % _P
_BX = 15112221349535400772501151409588531511454012693041857206046113283949847762202
_B = (_BX % _P, _BY % _P, 1, (_BX * _BY) % _P)


Point = tuple[int, int, int, int]


def _point_add(p: Point, q: Point) -> Point:
    x1, y1, z1, t1 = p
    x2, y2, z2, t2 = q
    a = ((y1 - x1) * (y2 - x2)) % _P
    b = ((y1 + x1) * (y2 + x2)) % _P
    c = (2 * t1 * t2 * _D) % _P
    d = (2 * z1 * z2) % _P
    e, f, g, h = b - a, d - c, d + c, b + a
    return ((e * f) % _P, (g * h) % _P, (f * g) % _P, (e * h) % _P)


def _scalar_mult(p: tuple[int, int, int, int], e: int) -> tuple[int, int, int, int]:
    q = (0, 1, 1, 0)  # neutral element
    while e > 0:
        if e & 1:
            q = _point_add(q, p)
        p = _point_add(p, p)
        e >>= 1
    return q


def _recover_x(y: int, sign: int) -> int | None:
    if y >= _P:
        return None  # non-canonical y encoding
    xx = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P)
    x = pow(xx, (_P + 3) // 8, _P)
    if (x * x - xx) % _P != 0:
        x = (x * _I) % _P
    if (x * x - xx) % _P != 0:
        return None
    if x % 2 != sign:
        x = _P - x
    if x == 0 and sign == 1:
        return None  # non-canonical encoding of the identity
    return x


def _decode_point(data: bytes) -> tuple[int, int, int, int] | None:
    if len(data) != 32:
        return None
    y = int.from_bytes(data, "little") & ((1 << 255) - 1)
    sign = data[31] >> 7
    x = _recover_x(y, sign)
    if x is None:
        return None
    return (x, y, 1, (x * y) % _P)


def _is_identity(p: tuple[int, int, int, int]) -> bool:
    x, y, z, _ = p
    zinv = pow(z, _P - 2, _P)
    return (x * zinv) % _P == 0 and (y * zinv) % _P == 1


def _equal(p: tuple[int, int, int, int], q: tuple[int, int, int, int]) -> bool:
    x1, y1, z1, _ = p
    x2, y2, z2, _ = q
    return (x1 * z2 - x2 * z1) % _P == 0 and (y1 * z2 - y2 * z1) % _P == 0


def _has_small_order(p: tuple[int, int, int, int]) -> bool:
    return _is_identity(_scalar_mult(p, 8))


def _verify_pure(public_key: bytes, message: bytes, signature: bytes) -> None:
    a_point = _decode_point(public_key)
    if a_point is None:
        raise Ed25519Error("public key is not a canonical Ed25519 point")
    if _has_small_order(a_point):
        raise Ed25519Error("public key has small order and is rejected")
    r_point = _decode_point(signature[:32])
    if r_point is None:
        raise Ed25519Error("signature R is not a canonical Ed25519 point")
    s = int.from_bytes(signature[32:], "little")
    if s >= _L:
        raise Ed25519Error("signature S is not canonical (S >= L): malleable signature rejected")
    k = (
        int.from_bytes(hashlib.sha512(signature[:32] + public_key + message).digest(), "little")
        % _L
    )
    # cofactorless: [S]B == R + [k]A
    if not _equal(_scalar_mult(_B, s), _point_add(r_point, _scalar_mult(a_point, k))):
        raise Ed25519Error("signature does not verify")


BACKEND = "pure-python"


def verify_ed25519(public_key: bytes, message: bytes, signature: bytes) -> None:
    """Raise :class:`Ed25519Error` unless *signature* is valid.  Never returns a bool."""
    if len(public_key) != 32:
        raise Ed25519Error(f"public key must be 32 bytes, got {len(public_key)}")
    if len(signature) != 64:
        raise Ed25519Error(f"signature must be 64 bytes, got {len(signature)}")
    _verify_pure(public_key, message, signature)
