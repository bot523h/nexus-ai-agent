"""Delivery signing spike — wave-4 step2.

Covers the fail-closed contract without requiring PyNaCl to be installed:
the HMAC fallback and the NaCl path share the same ``sign``/``verify``
interface (base64 transport, constant-time compare, :class:`SigningError`
on every failure), so every test is green without the extra and an
installed NaCl is exercised without any code change.
"""

from __future__ import annotations

import base64
import json
import secrets

import pytest

from nexus_ai_agent.creative.packs.delivery import signing as sut


def _set_key(monkeypatch: pytest.MonkeyPatch) -> str:
    key_hex = secrets.token_hex(32)  # 64 hex chars → 32 bytes
    monkeypatch.setenv("NEXUS_SIGNING_KEY", key_hex)
    return key_hex


def test_round_trip_dict_manifest(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_key(monkeypatch)
    manifest = {"package_id": "nexus.color.delivery", "version": "1.0.0", "ops": ["export_otio"]}
    sig = sut.sign_manifest(manifest)
    # base64 must decode
    assert base64.b64decode(sig, validate=True)
    # verify must not raise
    sut.verify_manifest(manifest, sig)


def test_round_trip_bytes_manifest(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_key(monkeypatch)
    manifest = b'{"hello":"world"}'
    sig = sut.sign_manifest(manifest)
    sut.verify_manifest(manifest, sig)


def test_canonicalisation_is_order_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_key(monkeypatch)
    a = {"b": 1, "a": 2}
    b = {"a": 2, "b": 1}
    # Same logical JSON must yield same signature
    assert sut.sign_manifest(a) == sut.sign_manifest(b)
    # Cross-verify
    sut.verify_manifest(b, sut.sign_manifest(a))


def test_tamper_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_key(monkeypatch)
    manifest = {"seq": [1, 2, 3], "name": "master"}
    sig = sut.sign_manifest(manifest)
    tampered = {"seq": [1, 2, 999], "name": "master"}
    with pytest.raises(sut.SigningError, match="tampered|verification failed"):
        sut.verify_manifest(tampered, sig)
    # Bit-flip in the signature itself must also be rejected
    bad = sig[:-4] + ("AAAA" if sig[-4:] != "AAAA" else "BBBB")
    with pytest.raises(sut.SigningError):
        sut.verify_manifest(manifest, bad)


def test_missing_key_is_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NEXUS_SIGNING_KEY", raising=False)
    with pytest.raises(sut.SigningError, match="NEXUS_SIGNING_KEY is not set"):
        sut.sign_manifest({"x": 1})
    with pytest.raises(sut.SigningError, match="NEXUS_SIGNING_KEY is not set"):
        sut.verify_manifest({"x": 1}, "abcd")


def test_empty_key_is_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXUS_SIGNING_KEY", "   ")
    with pytest.raises(sut.SigningError, match="not set"):
        sut.sign_manifest({"x": 1})


def test_malformed_base64_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_key(monkeypatch)
    with pytest.raises(sut.SigningError, match="base64"):
        sut.verify_manifest({"x": 1}, "!!!not-base64!!!")


def test_signed_manifest_dataclass_verify(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_key(monkeypatch)
    manifest = {"k": "v"}
    sig = sut.sign_manifest(manifest)
    sm = sut.SignedManifest(manifest=manifest, signature=sig)
    sm.verify()  # must not raise
    sm_tampered = sut.SignedManifest(manifest={"k": "other"}, signature=sig)
    with pytest.raises(sut.SigningError):
        sm_tampered.verify()


def test_different_keys_produce_different_signatures(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXUS_SIGNING_KEY", secrets.token_hex(32))
    a = sut.sign_manifest({"x": 1})
    monkeypatch.setenv("NEXUS_SIGNING_KEY", secrets.token_hex(32))
    b = sut.sign_manifest({"x": 1})
    assert a != b


def test_non_ascii_manifest_is_stable(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_key(monkeypatch)
    manifest = {"title": "سلام دنیا — نگار", "fps": 24}
    sig = sut.sign_manifest(manifest)
    # JSON with ensure_ascii=False must round-trip through utf-8
    canonical = json.dumps(
        manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    sut.verify_manifest(canonical, sig)
