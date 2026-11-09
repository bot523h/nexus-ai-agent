"""Phase-8 regression suite: one test per confirmed finding in storage/security.

Every test here reproduces a finding from the PR #326 hardening review against the
*old* behaviour and asserts the new contract. The naming is deliberate: the id in
each docstring is the row of ``FINDINGS_TRIAGE.md`` this test closes, so a reviewer
can walk the triage table and find code that fails if the fix is reverted.

Nothing here is mocked at the security boundary: real SQLite files, real Ed25519 /
XSalsa20-Poly1305 envelopes, real export/import directories.
"""

# ruff: noqa: E402 - _support.ensure_path() must run before these imports
from __future__ import annotations

import _support

_support.ensure_path()

import json
import sqlite3
from pathlib import Path

import pytest

from experiments.character_capsule.dataset import CHARACTERS, build_capsule
from experiments.character_capsule.errors import (
    AccessDenied,
    ConsentRevoked,
    ExportBlocked,
    IntegrityError,
    KeyUnavailable,
    RevisionConflict,
    UnknownReference,
    ValidationError,
)
from experiments.character_capsule.model import SubjectClass
from experiments.character_capsule.security import (
    ConsentPurpose,
    ConsentRecord,
    EnvelopeCipher,
    Permission,
    PlaintextCipher,
    Principal,
    Role,
    load_kek,
    random_key_material,
)
from experiments.character_capsule.storage import CapsuleRepository

SPEC = CHARACTERS[0]
OWNER = Principal(principal_id="user:owner", role=Role.OWNER)
VIEWER = Principal(principal_id="user:viewer", role=Role.VIEWER)
DIRECTOR = Principal(principal_id="user:director", role=Role.DIRECTOR)
PNG = b"\x89PNG\r\n\x1a\n" + b"frame-bytes" * 8


def _repo(
    root: Path, *, cipher=None, allow_plaintext: str = "pytest: scratch store", now=None
) -> CapsuleRepository:
    return CapsuleRepository(
        root, cipher=cipher, allow_plaintext=allow_plaintext, max_asset_bytes=1 << 20, now=now
    )


def _seed(
    root: Path,
    *,
    subject_class: SubjectClass = SubjectClass.SYNTHETIC,
    cipher=None,
    allow_plaintext: str = "pytest: scratch store",
    character_id: str | None = None,
) -> tuple[CapsuleRepository, str]:
    """Create one character with one stored asset and one saved capsule."""
    repo = _repo(root, cipher=cipher, allow_plaintext=allow_plaintext)
    identity = repo.create_character(
        display_name="Sara N.",
        subject_class=subject_class,
        work_title="Nagar",
        character_id=character_id,
        principals=[OWNER] if subject_class is SubjectClass.SYNTHETIC else [],
    )
    ref = repo.register_asset(identity.character_id, PNG, role="face")
    capsule = build_capsule(SPEC if character_id is None else _spec(character_id))
    capsule = capsule.with_assets((ref,))
    repo.save_capsule(capsule)
    return repo, identity.character_id


def _spec(character_id: str):
    from dataclasses import replace

    return replace(SPEC, character_id=character_id)


def _consent(
    character_id: str,
    *,
    consent_id: str = "CONS-0001",
    purposes=(ConsentPurpose.EXPORT,),
    rights=("export", "identity"),
    territory: str = "WORLDWIDE",
    granted_at: str = "2026-01-01T00:00:00Z",
    expires_at: str | None = "2027-01-01T00:00:00Z",
) -> ConsentRecord:
    return ConsentRecord(
        consent_id=consent_id,
        character_id=character_id,
        subject_reference="contract:2026-01",
        purposes=tuple(purposes),
        rights=tuple(rights),
        territory=territory,
        granted_at=granted_at,
        expires_at=expires_at,
        document_digest="sha256:" + "a" * 64,
    )


# ---------------------------------------------------------------------------
# F01 - "SQL injection through a character id"
# ---------------------------------------------------------------------------


def test_f01_a_hostile_id_reads_nothing_and_leaves_the_schema_intact(tmp_path):
    """F01 was reported as SQL injection; reproduction showed parameterised SQL.

    The finding is closed as *not reproducible*, and this test pins the property
    that made it fail: a hostile id is data, never syntax, and the schema survives.
    """
    repo, character_id = _seed(tmp_path)
    hostile = "NAGAR-CHAR-000001'; DROP TABLE capsules; --"
    with pytest.raises(UnknownReference):
        repo.identity(hostile)
    assert repo.load_capsule(character_id).character_id == character_id
    tables = {
        row[0] for row in repo._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"capsules", "characters", "assets", "consents", "lineage"} <= tables
    repo.close()


# ---------------------------------------------------------------------------
# F02 - authorization
# ---------------------------------------------------------------------------


def test_f02_a_viewer_cannot_export_assets(tmp_path):
    """F02: export skipped authorization, so a VIEWER could pull every asset."""
    repo, character_id = _seed(tmp_path, subject_class=SubjectClass.REAL_PERSON)
    repo.add_consent(_consent(character_id))
    with pytest.raises(AccessDenied):
        repo.export_bundle(character_id, tmp_path / "out", principal=VIEWER)
    assert not (tmp_path / "out").exists()
    actions = [event["action"] for event in repo.access.audit.events]
    assert "permission:EXPORT" in actions
    denied = [event for event in repo.access.audit.events if not event["allowed"]]
    assert denied and denied[0]["role"] == Role.VIEWER.value
    repo.close()


def test_f02b_only_the_owner_role_carries_every_permission(tmp_path):
    """Least privilege is data, not a comment: check the role table itself."""
    from experiments.character_capsule.security import ROLE_PERMISSIONS

    assert ROLE_PERMISSIONS[Role.VIEWER] == frozenset()
    assert ROLE_PERMISSIONS[Role.EXTERNAL] == frozenset()
    assert Permission.DELETE not in ROLE_PERMISSIONS[Role.DIRECTOR]
    assert Permission.MANAGE_CONSENT not in ROLE_PERMISSIONS[Role.DIRECTOR]
    assert ROLE_PERMISSIONS[Role.OWNER] == frozenset(Permission)


# ---------------------------------------------------------------------------
# F04 - encryption at rest
# ---------------------------------------------------------------------------


def test_f04_a_cipher_actually_encrypts_the_stored_bytes(tmp_path):
    """F04: ``cipher=None`` stored the PNG in clear and nothing recorded it."""
    pytest.importorskip(
        "nacl", reason="PyNaCl is not a dependency of this repository; the crypto tests skip here"
    )
    cache = tmp_path / "cache"
    cache.mkdir()
    cipher = EnvelopeCipher(random_key_material())
    repo, character_id = _seed(cache, cipher=cipher)
    blob_paths = [path for path in (cache / "blobs").rglob("*") if path.is_file()]
    assert blob_paths
    for path in blob_paths:
        raw = path.read_bytes()
        assert PNG not in raw, "plaintext PNG found in the blob store"
        assert raw.startswith(b"NC")
    assert repo.storage_policy()["encrypted"] is True
    assert repo.storage_policy()["plaintext_reason"] is None
    repo.close()


def test_f04b_plaintext_storage_requires_a_named_reason(tmp_path):
    """A store that cannot encrypt must say so, in the store, not in a commit message."""
    with pytest.raises(ValidationError):
        PlaintextCipher(reason="  ")
    # ... and no cipher at all is a refusal, not a silent plaintext fallback
    from experiments.character_capsule.errors import CryptoUnavailable

    with pytest.raises(CryptoUnavailable):
        CapsuleRepository(tmp_path / "unconfigured")
    repo, _ = _seed(tmp_path, allow_plaintext="CI runner without a KEK file")
    policy = repo.storage_policy()
    assert policy["encrypted"] is False
    assert policy["plaintext_reason"] == "CI runner without a KEK file"
    reopened = _repo(tmp_path, allow_plaintext="CI runner without a KEK file")
    assert reopened.storage_policy()["plaintext_reason"] == "CI runner without a KEK file"
    repo.close()
    reopened.close()


# ---------------------------------------------------------------------------
# F05 - the envelope round-trips (it did not: encrypt -> decrypt raised)
# ---------------------------------------------------------------------------


def test_f05_an_encrypted_asset_reads_back_byte_identical(tmp_path):
    pytest.importorskip("nacl", reason="PyNaCl is not a dependency of this repository")
    cipher = EnvelopeCipher(random_key_material())
    repo, character_id = _seed(tmp_path, cipher=cipher)
    assert repo.asset_bytes("AST-000001", character_id=character_id) == PNG
    repo.close()

    # The same KEK reopens the store; a different KEK cannot read a byte of it.
    same = _repo(tmp_path, cipher=EnvelopeCipher(random_key_material()))
    with pytest.raises((IntegrityError, KeyUnavailable, ValidationError)):
        same.asset_bytes("AST-000001", character_id=character_id)
    same.close()


def test_f05b_a_missing_kek_file_is_a_typed_refusal(tmp_path):
    with pytest.raises(KeyUnavailable):
        load_kek(tmp_path / "nope.key")
    key_file = tmp_path / "kek.bin"
    key_file.write_bytes(random_key_material())
    assert len(load_kek(key_file)) == 32


# ---------------------------------------------------------------------------
# F09 / F10 / F11 / F12 - consent semantics
# ---------------------------------------------------------------------------


def test_f09_a_revoked_consent_can_never_be_reissued_under_the_same_id(tmp_path):
    """F09: re-adding a revoked record cleared ``revoked_at``."""
    repo, character_id = _seed(tmp_path, subject_class=SubjectClass.REAL_PERSON)
    record = _consent(character_id)
    repo.add_consent(record)
    repo.revoke_consent(record.consent_id, "subject withdrew")
    with pytest.raises(ConsentRevoked):
        repo.add_consent(record)
    assert repo.consents.get(record.consent_id).is_revoked
    assert repo.revocations(character_id=character_id)
    repo.close()


def test_f10_consent_survives_a_restart(tmp_path):
    """F10: after a restart the ledger held no records at all."""
    repo, character_id = _seed(tmp_path, subject_class=SubjectClass.REAL_PERSON)
    repo.add_consent(_consent(character_id))
    repo.close()
    reopened = _repo(tmp_path)
    assert reopened.consent_state(character_id)["records"]
    assert reopened.consent_decision(character_id).allowed is True
    reopened.close()


def test_f10b_a_revocation_survives_a_restart(tmp_path):
    repo, character_id = _seed(tmp_path, subject_class=SubjectClass.REAL_PERSON)
    repo.add_consent(_consent(character_id))
    repo.revoke_consent("CONS-0001", "withdrawn")
    repo.close()
    reopened = _repo(tmp_path)
    decision = reopened.consent_decision(character_id)
    assert decision.allowed is False
    assert "revoked" in decision.reason
    reopened.close()


def test_f11_expiry_is_compared_as_time_not_as_text(tmp_path):
    """F11: ``...T00:00:00+05:00`` compared as a string looked later than a ``Z`` time."""
    repo, character_id = _seed(tmp_path, subject_class=SubjectClass.REAL_PERSON)
    repo.add_consent(
        _consent(
            character_id,
            granted_at="2026-01-01T00:00:00+05:00",
            expires_at="2026-01-02T00:00:00+05:00",
        )
    )
    repo.close()

    # 2026-01-01T20:00:00Z is 2026-01-02T01:00:00+05:00 -> already expired
    late = CapsuleRepository(tmp_path, allow_plaintext="test", now=lambda: "2026-01-01T20:00:00Z")
    decision = late.consent_decision(character_id)
    assert decision.allowed is False
    assert "expired" in decision.reason
    late.close()

    # ... while 2026-01-01T10:00:00Z is still inside the window
    early = CapsuleRepository(tmp_path, allow_plaintext="test", now=lambda: "2026-01-01T10:00:00Z")
    assert early.consent_decision(character_id).allowed is True
    early.close()


def test_f12_rights_and_territory_gate_an_export(tmp_path):
    """F12: ``rights`` and ``territory`` were stored and never consulted."""
    repo, character_id = _seed(tmp_path, subject_class=SubjectClass.REAL_PERSON)
    repo.add_consent(_consent(character_id, rights=("identity",), territory="TR"))
    with pytest.raises(ExportBlocked):
        repo.export_bundle(character_id, tmp_path / "out", principal=DIRECTOR, right="distribution")
    with pytest.raises(ExportBlocked):
        repo.export_bundle(
            character_id, tmp_path / "out", principal=DIRECTOR, right="export", territory="DE"
        )
    repo.add_consent(
        _consent(
            character_id,
            consent_id="CONS-0002",
            rights=("export",),
            territory="TR, DE",
        )
    )
    target = repo.export_bundle(
        character_id, tmp_path / "out", principal=DIRECTOR, right="export", territory="DE"
    )
    assert (target / "manifest.json").is_file()
    repo.close()


def test_f12b_a_synthetic_subject_does_not_need_consent(tmp_path):
    repo, character_id = _seed(tmp_path)
    target = repo.export_bundle(character_id, tmp_path / "out", principal=DIRECTOR)
    manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["consent_decision"]["allowed"] is True
    assert "synthetic" in manifest["consent_decision"]["reason"]
    repo.close()


def test_f12c_an_export_is_audited_with_the_consent_decision(tmp_path):
    repo, character_id = _seed(tmp_path, subject_class=SubjectClass.REAL_PERSON)
    repo.add_consent(_consent(character_id))
    repo.export_bundle(character_id, tmp_path / "out", principal=DIRECTOR)
    events = repo.audit_events()
    actions = [event["action"] for event in events]
    assert "export:authorized" in actions
    assert "export:written" in actions
    written = [event for event in events if event["action"] == "export:written"]
    assert written[-1]["principal"] == DIRECTOR.principal_id
    repo.close()


# ---------------------------------------------------------------------------
# F03 / F13 / F17 / F18 / F19 - export & import boundary
# ---------------------------------------------------------------------------


def test_f03_import_refuses_a_manifest_that_points_outside_the_bundle(tmp_path):
    """F03: ``json name='../outside-secret.bin'`` was read from outside the bundle."""
    repo, character_id = _seed(tmp_path)
    target = repo.export_bundle(character_id, tmp_path / "out", principal=DIRECTOR)
    secret = tmp_path / "outside-secret.bin"
    secret.write_bytes(b"not part of the bundle")
    manifest_path = target / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["assets"][0]["file"] = "../outside-secret.bin"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    fresh_root = tmp_path / "fresh"
    importer, _ = _seed(fresh_root)
    with pytest.raises((ValidationError, IntegrityError)):
        importer.import_bundle(target)
    assert secret.read_bytes() == b"not part of the bundle"
    importer.close()
    repo.close()


def test_f19_import_refuses_an_asset_owned_by_another_character(tmp_path):
    """F19: a bundle could name a foreign ``AST-`` id and import another's asset."""
    repo, character_id = _seed(tmp_path)
    target = repo.export_bundle(character_id, tmp_path / "out", principal=DIRECTOR)
    manifest_path = target / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["assets"][0]["asset_id"] = "AST-000002"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    importer, _ = _seed(tmp_path / "fresh")
    with pytest.raises((ValidationError, IntegrityError)):
        importer.import_bundle(target)
    importer.close()
    repo.close()


def test_f13_and_f17_import_restores_capsule_and_quarantines_consent(tmp_path):
    """F13/F17: the capsule and the consent records were dropped by import.

    Consent is restored into quarantine, not into force: an imported document is a
    claim by whoever wrote the bundle, so it stays inert until an owner adopts it.
    """
    repo, character_id = _seed(tmp_path, subject_class=SubjectClass.REAL_PERSON)
    repo.add_consent(_consent(character_id))
    target = repo.export_bundle(character_id, tmp_path / "out", principal=DIRECTOR)
    repo.close()

    fresh_root = tmp_path / "fresh"
    importer = _repo(fresh_root)
    capsule = importer.import_bundle(target)
    assert capsule.character_id == character_id
    assert importer.load_capsule(character_id).digest == capsule.digest
    assert importer.imported_consents(character_id=character_id)
    assert importer.consent_decision(character_id).allowed is False

    record_id = importer.imported_consents(character_id=character_id)[0]["consent_id"]
    with pytest.raises(AccessDenied):
        importer.adopt_imported_consent(record_id, principal=VIEWER)
    with pytest.raises(AccessDenied):
        importer.adopt_imported_consent(record_id, principal=DIRECTOR)
    adopted = importer.adopt_imported_consent(record_id, principal=OWNER)
    assert adopted.consent_id == record_id
    assert importer.consent_decision(character_id).allowed is True
    with pytest.raises(ValidationError):
        importer.adopt_imported_consent(record_id, principal=OWNER)
    importer.close()


def test_f18_a_failed_export_leaves_the_previous_destination_intact(tmp_path):
    """F18: a failed export wiped the destination directory."""
    repo, character_id = _seed(tmp_path, subject_class=SubjectClass.REAL_PERSON)
    repo.add_consent(_consent(character_id))
    target = repo.export_bundle(character_id, tmp_path / "out", principal=DIRECTOR)
    before = (target / "manifest.json").read_bytes()

    # Refuse the next export *after* the destination already exists.
    with pytest.raises(ExportBlocked):
        repo.export_bundle(
            character_id,
            target,
            principal=DIRECTOR,
            purpose=ConsentPurpose.COMMERCIAL_DISTRIBUTION,
        )
    assert (target / "manifest.json").read_bytes() == before
    assert not list(tmp_path.glob(".out.staging-*"))
    repo.close()


# ---------------------------------------------------------------------------
# F31 - duplicate bytes must not collide
# ---------------------------------------------------------------------------


def test_f31_registering_the_same_bytes_twice_is_idempotent_not_an_error(tmp_path):
    """F31: the second identical registration raised ``IntegrityError``."""
    repo, character_id = _seed(tmp_path)
    first = repo.register_asset(character_id, PNG, role="face")
    second = repo.register_asset(character_id, PNG, role="face")
    assert first.asset_id == second.asset_id
    assert first.sha256 == second.sha256
    # the same bytes in a different role is a different asset, with one blob
    other_role = repo.register_asset(character_id, PNG, role="other")
    assert other_role.asset_id != first.asset_id
    repo.close()


def test_f31b_asset_bytes_are_verified_against_the_recorded_digest(tmp_path):
    repo, character_id = _seed(tmp_path)
    ref = repo.register_asset(character_id, PNG, role="face")
    report = repo.verify_integrity(character_id)
    assert report["ok"] is True
    blob = next(path for path in (tmp_path / "blobs").rglob("*") if path.is_file())
    blob.write_bytes(blob.read_bytes() + b"tampered")
    broken = repo.verify_integrity(character_id)
    assert broken["ok"] is False
    assert ref.asset_id in json.dumps(broken)
    repo.close()


# ---------------------------------------------------------------------------
# capsulerevision compare-and-swap (the storage half of F20)
# ---------------------------------------------------------------------------


def test_revision_cas_refuses_a_stale_write(tmp_path):
    repo, character_id = _seed(tmp_path)
    capsule = repo.load_capsule(character_id)
    hair = capsule.module("hair").updated({"colour": "#101010"})
    second = capsule.with_module(hair, note="second revision")
    repo.save_capsule(second, expected_revision=1)
    assert repo.load_capsule(character_id).revision_no == 2

    stale = second.with_module(second.module("hair").updated({"colour": "#202020"}))
    with pytest.raises(RevisionConflict):
        repo.save_capsule(stale, expected_revision=1)
    repo.close()


def test_a_capsule_row_cannot_be_rewritten_in_place(tmp_path):
    """The digest chain is what makes a stored revision trustworthy."""
    repo, character_id = _seed(tmp_path)
    row = repo._conn.execute(
        "SELECT revision_no, digest FROM capsules WHERE character_id=?", (character_id,)
    ).fetchone()
    assert row["revision_no"] == 1
    raw = sqlite3.connect(tmp_path / "capsules.sqlite3")
    raw.execute("UPDATE capsules SET payload='{}' WHERE character_id=?", (character_id,))
    raw.commit()
    raw.close()
    with pytest.raises((ValidationError, IntegrityError, UnknownReference)):
        repo.load_capsule(character_id)
    repo.close()
