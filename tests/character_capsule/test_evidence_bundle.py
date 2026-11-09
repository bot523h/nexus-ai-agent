"""Evidence bundles: content addressing, one hard binding, honest signatures.

The security property under test is *not* "the bundle exists". It is that changing
one byte of a described artifact, or removing the signature, changes the verdict a
second party reaches.
"""

# ruff: noqa: E402 - _support.ensure_path() must run before these imports
from __future__ import annotations

import _support

_support.ensure_path()

import json

import pytest

from experiments.character_capsule.errors import KeyUnavailable, ValidationError
from experiments.character_capsule.evidence import (
    BUNDLE_INVALID,
    BUNDLE_UNSIGNED,
    BUNDLE_VALID,
    EvidenceBundle,
    bundle_from_files,
    load_signing_key,
    sign_bundle,
    verify_bundle,
)

LINEAGE = ["sha256:" + "0" * 64]


def _bundle(tmp_path, *, tamper_name=None):
    report = tmp_path / "report.json"
    report.write_text('{"verdict":"VERIFIED"}', encoding="utf-8")
    frame = tmp_path / "frame.png"
    frame.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 16)
    return bundle_from_files(
        bundle_id="EVD-TEST-0001",
        subject={"shot_id": "SH-001", "verdict": "VERIFIED", "media_type": "image/png"},
        assertions={"dimensions": [{"name": "face", "distance": 0.0}]},
        files={
            str(report): ("report", "application/json"),
            str(frame): ("frame", "image/png"),
        },
        lineage=LINEAGE,
        created_at="2026-11-09T00:00:00Z",
        disclosure={"ai_generated": True, "real_person": False},
    ), {"report.json": report, "frame.png": frame}


def test_bundle_binds_every_artifact_it_names(tmp_path):
    bundle, files = _bundle(tmp_path)
    verification = verify_bundle(bundle, files=files)
    assert verification.status == BUNDLE_UNSIGNED
    assert verification.checks["files_match"] is True
    assert verification.checks["hard_binding_matches_records"] is True
    assert verification.checks["lineage_present"] is True


def test_tampering_with_an_artifact_invalidates_the_bundle(tmp_path):
    bundle, files = _bundle(tmp_path)
    files["frame.png"].write_bytes(b"\x89PNG\r\n\x1a\n" + b"1" * 16)
    verification = verify_bundle(bundle, files=files)
    assert verification.checks["files_match"] is False
    assert verification.ok is False


def test_tampering_with_a_record_breaks_the_hard_binding(tmp_path):
    bundle, files = _bundle(tmp_path)
    payload = bundle.to_dict()
    payload["records"][0]["size_bytes"] += 1
    forged = EvidenceBundle.from_dict(payload)
    verification = verify_bundle(forged, files=files)
    assert verification.checks["hard_binding_matches_records"] is False
    assert verification.status == BUNDLE_UNSIGNED
    assert not verification.ok


def test_signing_then_verifying_is_valid(tmp_path):
    from nacl import signing

    bundle, files = _bundle(tmp_path)
    key_file = tmp_path / "signing.hex"
    key_file.write_text(signing.SigningKey.generate().encode().hex(), encoding="utf-8")
    key = load_signing_key(key_file)
    signed = sign_bundle(bundle, signing_key=key, signed_at="2026-11-09T00:00:00Z")
    verification = verify_bundle(signed, files=files)
    assert verification.status == BUNDLE_VALID
    assert verification.checks["signature_valid"] is True


def test_signature_does_not_survive_an_edit(tmp_path):
    from nacl import signing

    bundle, _ = _bundle(tmp_path)
    key_file = tmp_path / "signing.hex"
    key_file.write_text(signing.SigningKey.generate().encode().hex(), encoding="utf-8")
    signed = sign_bundle(bundle, signing_key=load_signing_key(key_file), signed_at="now")
    payload = signed.to_dict()
    payload["subject"]["verdict"] = "VERIFIED_WITH_LIMITATIONS"
    forged = EvidenceBundle.from_dict(payload)
    verification = verify_bundle(forged)
    assert verification.status == BUNDLE_INVALID
    assert verification.checks["signature_valid"] is False


def test_a_foreign_key_does_not_verify(tmp_path):
    from nacl import signing

    bundle, _ = _bundle(tmp_path)
    signer = tmp_path / "signer.hex"
    signer.write_text(signing.SigningKey.generate().encode().hex(), encoding="utf-8")
    signed = sign_bundle(bundle, signing_key=load_signing_key(signer), signed_at="now")
    other = signing.SigningKey.generate().verify_key
    verification = verify_bundle(signed, public_key_hex=bytes(other).hex())
    assert verification.status == BUNDLE_INVALID


def test_no_key_configured_is_a_typed_refusal(tmp_path, monkeypatch):
    monkeypatch.delenv("CHARACTER_CAPSULE_SIGNING_KEY_FILE", raising=False)
    with pytest.raises(KeyUnavailable):
        load_signing_key(tmp_path / "missing.hex")


def test_bundle_requires_lineage_and_records():
    with pytest.raises(ValidationError):
        bundle_from_files(
            bundle_id="EVD-TEST-0002",
            subject={"shot_id": "SH-002"},
            assertions={},
            files={},
            lineage=LINEAGE,
            created_at="2026-11-09T00:00:00Z",
        )


def test_bundle_round_trips_through_json(tmp_path):
    bundle, _ = _bundle(tmp_path)
    restored = EvidenceBundle.from_dict(json.loads(bundle.to_json()))
    assert restored.digest == bundle.digest
    assert restored.claim_digest == bundle.claim_digest


def test_c2pa_shape_is_declared_as_shape_only(tmp_path):
    bundle, _ = _bundle(tmp_path)
    shape = bundle.c2pa_manifest_shape()
    assert shape["hard_binding"]["alg"] == "sha256"
    assert len(shape["ingredients"]) == len(bundle.lineage)
    assert "no C2PA SDK" in shape["note"]


def test_disclosure_is_carried_in_the_claim(tmp_path):
    bundle, _ = _bundle(tmp_path)
    assert bundle.disclosure["ai_generated"] is True
    assert bundle.claim["disclosure"]["real_person"] is False
