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
    BUNDLE_INCOMPLETE,
    BUNDLE_INVALID,
    BUNDLE_NOT_VERIFIABLE,
    BUNDLE_UNSIGNED,
    BUNDLE_UNTRUSTED_SIGNER,
    BUNDLE_VALID,
    EvidenceBundle,
    TrustAnchor,
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


def test_a_signature_alone_is_not_authenticity(tmp_path):
    """A bundle that carries its own key proves possession, not trust.

    This is finding F06: the old verifier returned VALID for a bundle signed with a
    key generated inside it, which any forger can do. Without a consumer-supplied
    anchor the strongest true statement is UNTRUSTED_SIGNER.
    """
    from nacl import signing

    bundle, files = _bundle(tmp_path)
    key_file = tmp_path / "signing.hex"
    key_file.write_text(signing.SigningKey.generate().encode().hex(), encoding="utf-8")
    signed = sign_bundle(bundle, signing_key=load_signing_key(key_file), signed_at="now")
    self_signed = verify_bundle(signed, files=files)
    assert self_signed.status == BUNDLE_UNTRUSTED_SIGNER
    assert self_signed.ok is False
    assert self_signed.integrity_ok is True  # the bytes are fine; trust is absent


def test_the_consumer_trust_anchor_makes_a_signature_valid(tmp_path):
    from nacl import signing

    bundle, files = _bundle(tmp_path)
    key = signing.SigningKey.generate()
    key_file = tmp_path / "signing.hex"
    key_file.write_text(key.encode().hex(), encoding="utf-8")
    signed = sign_bundle(bundle, signing_key=load_signing_key(key_file), signed_at="now")
    anchor = TrustAnchor(public_key_hex=bytes(key.verify_key).hex())
    verification = verify_bundle(signed, files=files, trust_anchor=anchor)
    assert verification.status == BUNDLE_VALID
    assert verification.ok is True
    assert verification.checks["signature_valid"] is True
    assert verification.checks["trust_anchor_matches_signer"] is True
    assert verification.signer_key_id


def test_an_expected_key_id_mismatch_is_refused(tmp_path):
    from nacl import signing

    bundle, files = _bundle(tmp_path)
    key = signing.SigningKey.generate()
    key_file = tmp_path / "signing.hex"
    key_file.write_text(key.encode().hex(), encoding="utf-8")
    signed = sign_bundle(bundle, signing_key=load_signing_key(key_file), signed_at="now")
    anchor = TrustAnchor(public_key_hex=bytes(key.verify_key).hex(), key_id="not-the-signing-key")
    verification = verify_bundle(signed, files=files, trust_anchor=anchor)
    assert verification.status == BUNDLE_UNTRUSTED_SIGNER
    assert verification.trust_anchor_key_id == "not-the-signing-key"


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
    # The signature is well formed but it is not from the key the consumer trusts:
    # that is UNTRUSTED_SIGNER, not INVALID (the bundle is not corrupted, it is
    # simply not from the expected signer).
    assert verification.status == BUNDLE_UNTRUSTED_SIGNER
    assert verification.checks["trust_anchor_matches_signer"] is False


def test_missing_artifacts_are_incomplete_never_valid(tmp_path):
    """F07: a bundle whose recorded files were not supplied is not "valid"."""
    bundle, _ = _bundle(tmp_path)
    partial = verify_bundle(bundle, files={"report.json": tmp_path / "report.json"})
    assert partial.status == BUNDLE_INCOMPLETE
    assert partial.ok is False
    assert "frame.png" in partial.missing_records
    assert partial.checks["all_artifacts_supplied"] is False


def test_artifacts_that_were_never_offered_cannot_be_complete(tmp_path):
    """Verifying without artifacts checks structure only; that is INCOMPLETE too."""
    bundle, _ = _bundle(tmp_path)
    verification = verify_bundle(bundle)
    assert verification.status == BUNDLE_INCOMPLETE
    assert verification.checks["all_artifacts_supplied"] is False


def test_a_reamed_key_is_reported_not_verified(tmp_path):
    """F16: a malformed key used to be silently treated as "unsigned"."""
    bundle, files = _bundle(tmp_path)
    with pytest.raises(ValidationError):
        TrustAnchor(public_key_hex="zz" * 32)
    # A signed bundle with an unusable trusted key must not read as VALID.
    bundle_payload = bundle.to_dict()
    bundle_payload["signature"] = {
        "algorithm": "ed25519",
        "key_id": "deadbeef",
        "public_key_hex": "ff" * 32,
        "signature_hex": "00" * 64,
        "signed_at": "now",
    }
    forged = EvidenceBundle.from_dict(bundle_payload)
    verification = verify_bundle(forged, files=files, public_key_hex="ff" * 32)
    assert verification.status in {BUNDLE_INVALID, BUNDLE_NOT_VERIFIABLE}
    assert verification.status != BUNDLE_VALID


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
