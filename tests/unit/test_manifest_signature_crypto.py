"""Ed25519 manifest trust + crypto shim coverage (Vision Phase 1 hardening).

Covers:
* vendored ``creative.packs.ed25519`` against RFC 8032 vectors
* ``verify.canonical_manifest_bytes`` determinism
* ``verify`` placeholder vs verified vs invalid_signature vs unknown_publisher
* portrait/scene re-export shim importability (so pack_coverage does not report 0%)
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from nexus_ai_agent.creative.packs import ed25519 as ed
from nexus_ai_agent.creative.packs.manifest import CapabilityPackManifest, load_manifest
from nexus_ai_agent.creative.packs.verify import (
    TRUSTED_PUBLISHER_KEYS,
    canonical_manifest_bytes,
    verify_manifest,
)

PACKS_DIR = Path(__file__).parents[2] / "src" / "nexus_ai_agent" / "creative" / "packs"
SLIDESHOW_MANIFEST = PACKS_DIR / "slideshow" / "pack.manifest.json"


def _manifest_dict() -> dict:
    return json.loads(SLIDESHOW_MANIFEST.read_text(encoding="utf-8"))


def _nagar_core_seed() -> bytes:
    return hashlib.sha256(b"nagar-core:ed25519:01").digest()[:32]


def _sign_canonical(manifest: CapabilityPackManifest, seed: bytes) -> str:
    """Sign canonical bytes of *manifest* (which may still carry a placeholder)
    with ``seed`` and return ``base64:<b64>``."""
    # derive pk
    pk = ed.publickey(seed)
    msg = canonical_manifest_bytes(manifest)
    sig = ed.signature(msg, seed, pk)
    return "base64:" + base64.b64encode(sig).decode("ascii")


# ---------------------------------------------------------------------------
# ed25519 primitive
# ---------------------------------------------------------------------------


def test_ed25519_rfc_vector_empty_message():
    # RFC 8032 §7.1 Example 1 (secret 9d61..., public d75a..., sig e556... on b"")
    sk_hex = "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60"
    pk_hex = "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"
    sig_hex = "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"
    sk = bytes.fromhex(sk_hex)
    pk_exp = bytes.fromhex(pk_hex)
    sig_exp = bytes.fromhex(sig_hex)
    pk = ed.publickey(sk)
    assert pk.hex() == pk_hex
    sig = ed.signature(b"", sk, pk)
    assert sig.hex() == sig_hex
    assert ed.checkvalid(sig, b"", pk) is True
    assert ed.verify_signature(pk, b"", sig) is True
    # tamper fails
    bad = bytearray(sig)
    bad[0] ^= 1
    assert ed.checkvalid(bytes(bad), b"", pk) is False
    # wrong key fails
    seed2, pk2 = ed.keypair_from_seed(hashlib.sha256(b"other").digest()[:32])
    assert ed.checkvalid(sig, b"", pk2) is False


def test_ed25519_roundtrip_and_helpers():
    seed = hashlib.sha256(b"roundtrip").digest()[:32]
    seed2 = hashlib.sha256(b"roundtrip2").digest()[:32]
    pk = ed.publickey(seed)
    # keypair_from_seed
    s, p = ed.keypair_from_seed(seed)
    assert s == seed
    assert p == pk
    # sign_message helper
    msg = b"hello nagar"
    sig = ed.sign_message(seed, msg)
    assert ed.verify_signature(pk, msg, sig) is True
    assert ed.verify_signature(pk, b"other", sig) is False
    # _H, _Hint, _decodepoint, _encodepoint exercised via above
    # _inv, _xrecover, _scalarmult via publickey
    # _isoncurve via _decodepoint
    # encode/decode roundtrip
    # test bad lengths
    assert ed.checkvalid(b"short", msg, pk) is False
    assert ed.checkvalid(sig, msg, b"short") is False
    # S >= l fails
    bad_sig = ed.signature(msg, seed, pk)
    # manually make S >= l by setting high value: just flip to all ff after R
    bad = bytearray(bad_sig)
    bad[32:] = b"\xff" * 32
    assert ed.checkvalid(bytes(bad), msg, pk) is False


def test_nagar_core_key_is_pinned():
    seed = _nagar_core_seed()
    pk = ed.publickey(seed)
    b64 = base64.b64encode(pk).decode("ascii")
    assert b64 == "d0InlC2hkLabPz6GiKhbw248GZ15bssUcXJuVEncBNs="
    assert TRUSTED_PUBLISHER_KEYS["nagar-core"] == pk


def test_canonical_manifest_bytes_is_deterministic_and_excludes_signature():
    m1 = load_manifest(SLIDESHOW_MANIFEST)
    b1 = canonical_manifest_bytes(m1)
    # dump canonical json must be sort_keys, no spaces
    data = json.loads(b1.decode("utf-8"))
    assert "security" in data
    assert "signature" not in data["security"]
    # second call same
    assert canonical_manifest_bytes(m1) == b1
    # changing a capability changes bytes
    payload = _manifest_dict()
    payload["capabilities"] = ["slideshow.compose", "slideshow.render"]
    m2 = CapabilityPackManifest.model_validate(payload)
    assert canonical_manifest_bytes(m2) != b1
    # allow_nan=False — NaN would error
    # parameters with float nan must not be in manifest anyway
    assert b1.startswith(b"{")


# ---------------------------------------------------------------------------
# verify trust states
# ---------------------------------------------------------------------------


def test_verify_placeholder_is_warning_not_error():
    m = load_manifest(SLIDESHOW_MANIFEST)
    # slideshow is placeholder via base64:replace
    assert m.signature_is_placeholder is True
    report = verify_manifest(m, anchor="builtin")
    assert report.signature_state == "placeholder"
    assert report.ok is True
    assert any(i.code == "unsigned_manifest" for i in report.warnings)


def test_verify_placeholder_variants_both_treated_as_unsigned():
    # audio uses base64:placeholder-... historically; should also be placeholder
    p = PACKS_DIR / "audio" / "pack.manifest.json"
    m = load_manifest(p)
    assert m.signature_is_placeholder is True
    report = verify_manifest(m, anchor="builtin")
    assert report.signature_state == "placeholder"
    assert report.ok is True
    # portal still verifies external placeholder as warning (not invalid_signature)
    payload = _manifest_dict()
    payload["security"]["signature"] = "base64:placeholder-test"
    payload["security"]["trusted_publisher"] = "nagar-core"
    m2 = CapabilityPackManifest.model_validate(payload)
    assert m2.signature_is_placeholder is True
    report2 = verify_manifest(m2, anchor="external")
    assert report2.signature_state == "placeholder"


def test_verify_valid_ed25519_signature_is_verified():
    seed = _nagar_core_seed()
    pk = ed.publickey(seed)
    base = _manifest_dict()
    # ensure clean placeholder first for canonical bytes
    base["security"]["signature"] = "base64:replace-with-signed-manifest"
    base["security"]["trusted_publisher"] = "nagar-core"
    base["security"]["signature_algorithm"] = "ed25519"
    m0 = CapabilityPackManifest.model_validate(base)
    sig_b64 = _sign_canonical(m0, seed)
    # now build verified manifest
    verified_dict = dict(base)
    verified_dict["security"] = dict(base["security"])
    verified_dict["security"]["signature"] = sig_b64
    m_verified = CapabilityPackManifest.model_validate(verified_dict)
    report = verify_manifest(m_verified, anchor="external")
    assert report.signature_state == "verified"
    assert report.ok is True
    assert not any(i.code == "unsigned_manifest" for i in report.warnings)
    # builtin anchor with same signature still verifies (pin is same)
    report2 = verify_manifest(m_verified, anchor="builtin")
    assert report2.signature_state == "verified"


def test_verify_invalid_signature_is_error():
    seed = _nagar_core_seed()
    base = _manifest_dict()
    base["security"]["signature"] = "base64:replace-with-signed-manifest"
    base["security"]["trusted_publisher"] = "nagar-core"
    m0 = CapabilityPackManifest.model_validate(base)
    sig_b64 = _sign_canonical(m0, seed)
    # tamper: flip last char
    tampered = sig_b64[:-1] + ("A" if sig_b64[-1] != "A" else "B")
    bad_dict = dict(base)
    bad_dict["security"] = dict(base["security"])
    bad_dict["security"]["signature"] = tampered
    m_bad = CapabilityPackManifest.model_validate(bad_dict)
    report = verify_manifest(m_bad, anchor="external")
    assert report.signature_state == "invalid_signature"
    assert not report.ok
    assert any(i.code == "invalid_signature" for i in report.errors)


def test_verify_tampered_manifest_fails():
    seed = _nagar_core_seed()
    base = _manifest_dict()
    base["security"]["signature"] = "base64:replace-with-signed-manifest"
    base["security"]["trusted_publisher"] = "nagar-core"
    m0 = CapabilityPackManifest.model_validate(base)
    sig_b64 = _sign_canonical(m0, seed)
    verified_dict = dict(base)
    verified_dict["security"] = dict(base["security"])
    verified_dict["security"]["signature"] = sig_b64
    m_verified = CapabilityPackManifest.model_validate(verified_dict)
    assert verify_manifest(m_verified, anchor="external").signature_state == "verified"
    # tamper capability list but keep signature
    tampered_payload = json.loads(json.dumps(verified_dict))
    tampered_payload["capabilities"] = ["slideshow.compose"]
    m_tampered = CapabilityPackManifest.model_validate(tampered_payload)
    report = verify_manifest(m_tampered, anchor="external")
    assert report.signature_state == "invalid_signature"


def test_verify_unknown_publisher_is_warning():
    seed = hashlib.sha256(b"attacker").digest()[:32]
    pk = ed.publickey(seed)
    sig = ed.signature(b"test", seed, pk)
    sig_b64 = "base64:" + base64.b64encode(sig).decode("ascii")
    payload = _manifest_dict()
    payload["security"]["signature"] = sig_b64
    payload["security"]["trusted_publisher"] = "evil-publisher"
    payload["security"]["signature_algorithm"] = "ed25519"
    m = CapabilityPackManifest.model_validate(payload)
    report = verify_manifest(m, anchor="external")
    assert report.signature_state == "unknown_publisher"
    assert report.ok is True  # warning, not error
    assert any(i.code == "unknown_publisher" for i in report.warnings)


def test_verify_bad_encoding_and_algorithm_are_invalid():
    payload = _manifest_dict()
    # not base64: prefix
    payload["security"]["signature"] = "not-base64:abc"
    payload["security"]["trusted_publisher"] = "nagar-core"
    m = CapabilityPackManifest.model_validate(payload)
    report = verify_manifest(m, anchor="external")
    assert report.signature_state == "invalid_signature"
    assert any(i.code == "invalid_signature" for i in report.errors)
    # invalid base64 payload
    payload2 = _manifest_dict()
    payload2["security"]["signature"] = "base64:!!!notbase64!!!"
    payload2["security"]["trusted_publisher"] = "nagar-core"
    m2 = CapabilityPackManifest.model_validate(payload2)
    report2 = verify_manifest(m2, anchor="external")
    assert report2.signature_state == "invalid_signature"
    # wrong length (32 bytes not 64)
    short_sig = base64.b64encode(b"\x00" * 32).decode("ascii")
    payload3 = _manifest_dict()
    payload3["security"]["signature"] = "base64:" + short_sig
    payload3["security"]["trusted_publisher"] = "nagar-core"
    m3 = CapabilityPackManifest.model_validate(payload3)
    report3 = verify_manifest(m3, anchor="external")
    assert report3.signature_state == "invalid_signature"


def test_verify_wrong_key_is_invalid():
    # sign with attacker key but claim nagar-core
    attacker_seed = hashlib.sha256(b"attacker2").digest()[:32]
    attacker_pk = ed.publickey(attacker_seed)
    base = _manifest_dict()
    base["security"]["signature"] = "base64:replace-with-signed-manifest"
    base["security"]["trusted_publisher"] = "nagar-core"
    m0 = CapabilityPackManifest.model_validate(base)
    msg = canonical_manifest_bytes(m0)
    sig = ed.signature(msg, attacker_seed, attacker_pk)
    sig_b64 = "base64:" + base64.b64encode(sig).decode("ascii")
    verified_dict = dict(base)
    verified_dict["security"] = dict(base["security"])
    verified_dict["security"]["signature"] = sig_b64
    verified_dict["security"]["trusted_publisher"] = "nagar-core"
    m = CapabilityPackManifest.model_validate(verified_dict)
    report = verify_manifest(m, anchor="external")
    assert report.signature_state == "invalid_signature"


# ---------------------------------------------------------------------------
# pack shims
# ---------------------------------------------------------------------------


def test_portrait_and_scene_model_shims_importable():
    # These are re-export shims (from vision.models import *); importing them
    # must succeed and contribute to pack_coverage.
    import nexus_ai_agent.creative.packs.portrait.models as pm
    import nexus_ai_agent.creative.packs.scene.models as sm
    import nexus_ai_agent.creative.packs.vision.models as vm

    # shims should expose the shared types
    assert hasattr(pm, "StrictModel")
    assert hasattr(sm, "StrictModel")
    assert hasattr(vm, "StrictModel")
    # portrait operations re-export
    import nexus_ai_agent.creative.packs.portrait.operations as po
    import nexus_ai_agent.creative.packs.scene.operations as so

    assert hasattr(po, "register_portrait_operations")
    assert hasattr(so, "register_scene_operations")


def test_ed25519_module_top_level_executed():
    # Force import and exercise constants
    assert ed.b == 256
    assert ed.q == 2**255 - 19
    assert ed.d is not None
    assert ed.B is not None
    # _H and _expmod
    assert len(ed._H(b"test")) == 64
    assert ed._inv(2) * 2 % ed.q == 1
