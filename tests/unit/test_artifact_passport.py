"""Adversarial and mutation suite for ArtifactPassport, ProofReceipt, and DurableStore.

Covers:
- Deterministic passport hash generation and integrity verification
- Causal lineage tampering detection (broken parent hash, forged actor/transaction)
- Execution proof validation
- DB persistence round-trip and fail-closed security guards
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from nexus_ai_agent.creative.studio.passport import (
    ArtifactPassport,
    ExecutionProof,
    IndependentMediaVerificationError,
    ProvenanceCausalChain,
    verify_causal_provenance_chain,
    verify_media_artifact_independently,
)
from nexus_ai_agent.creative.studio.persistence import DurableStore
from nexus_ai_agent.creative.studio.testing import make_project


def make_valid_passport() -> ArtifactPassport:
    chain = ProvenanceCausalChain(
        request_id="req_1001",
        project_id="proj_alpha",
        actor_id="usr_editor1",
        intent_id="intent_55",
        plan_id="plan_99",
        transaction_id="tx_12345",
        command_id="cmd_split_1",
        operation="timeline.split_at_playhead",
    )
    proof = ExecutionProof(
        executor_id="exec_local_v1",
        status="success",
        output_hash="sha256:" + "11" * 32,
        duration_ms=42.5,
        verification_metrics={"clip_count_delta": 1},
        timestamp_utc="2026-10-01T23:30:00Z",
    )
    passport = ArtifactPassport(
        artifact_id="art_clip_split_1",
        artifact_type="timeline_clip",
        content_hash="sha256:" + "aa" * 32,
        causal_chain=chain,
        execution_proof=proof,
    )
    return passport.with_computed_hash()


class TestArtifactPassportAdversarialSuite:
    def test_passport_hash_is_deterministic_and_verifiable(self) -> None:
        p1 = make_valid_passport()
        p2 = make_valid_passport()

        assert p1.passport_hash == p2.passport_hash
        assert p1.verify_integrity() is True

    def test_tampered_payload_invalidates_passport_hash(self) -> None:
        valid_passport = make_valid_passport()

        # Forged actor in causal chain
        forged_chain = valid_passport.causal_chain.model_copy(update={"actor_id": "usr_attacker"})
        tampered_passport = valid_passport.model_copy(update={"causal_chain": forged_chain})

        assert tampered_passport.verify_integrity() is False

    def test_invalid_content_hash_format_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ArtifactPassport(
                artifact_id="art_1",
                artifact_type="type1",
                content_hash="md5:invalid_hash",
                causal_chain=make_valid_passport().causal_chain,
                execution_proof=make_valid_passport().execution_proof,
            )

    def test_invalid_parent_passport_hash_format_rejected(self) -> None:
        with pytest.raises(ValidationError, match="parent passport hash format"):
            ProvenanceCausalChain(
                request_id="req_1",
                project_id="p1",
                actor_id="a1",
                transaction_id="tx1",
                command_id="c1",
                operation="media.play",
                parent_passport_hashes=("invalid_parent_hash",),
            )

    def test_durable_store_passport_roundtrip(self) -> None:
        store = DurableStore(":memory:")
        proj = make_project("proj_alpha")
        store.save_project_state(proj)

        passport = make_valid_passport()
        store.save_artifact_passport(passport)

        loaded = store.load_artifact_passport(passport.passport_hash)
        assert loaded is not None
        assert loaded.passport_hash == passport.passport_hash
        assert loaded.causal_chain.transaction_id == "tx_12345"
        assert loaded.verify_integrity() is True

        store.close()

    def test_bus_attached_durable_store_automatically_persists_passports(self) -> None:
        from nexus_ai_agent.creative.studio import CommandBus
        from nexus_ai_agent.creative.studio.testing import default_authorizer, make_command

        proj = make_project("proj_auto_passport")
        bus = CommandBus(state=proj, authorizer=default_authorizer())
        store = DurableStore(":memory:")
        bus.attach_durable_store(store)

        cmd = make_command(
            "timeline.mark", "c_mark_auto", input={"at": "اینجا", "label": "AutoMark"}
        )
        res = bus.dispatch(cmd)
        # Query persisted project
        loaded_proj = store.load_project_state("proj_auto_passport")
        assert loaded_proj is not None
        assert loaded_proj.state_revision == 1

        # Query auto-persisted passport
        rows = store._conn.execute("SELECT passport_hash FROM artifact_passports").fetchall()
        assert len(rows) == 1
        passport_hash = rows[0]["passport_hash"]
        loaded_passport = store.load_artifact_passport(passport_hash)

        assert loaded_passport is not None
        assert loaded_passport.causal_chain.transaction_id == res.transaction_id
        assert loaded_passport.causal_chain.command_id == "c_mark_auto"
        assert loaded_passport.verify_integrity() is True

        store.close()

    def test_independent_media_verifier_accepts_valid_and_rejects_spoofed_or_corrupted(
        self, tmp_path
    ) -> None:
        import hashlib

        # Valid PNG
        png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
        png_file = tmp_path / "valid.png"
        png_file.write_bytes(png_bytes)
        png_sha = f"sha256:{hashlib.sha256(png_bytes).hexdigest()}"

        report = verify_media_artifact_independently(
            png_file,
            allowed_root=tmp_path,
            declared_sha256=png_sha,
            declared_media_kind="image",
            declared_byte_size=len(png_bytes),
        )
        assert report["verified"] is True
        assert report["byte_size"] == len(png_bytes)

        # Zero-byte file rejected
        empty_file = tmp_path / "empty.mp4"
        empty_file.write_bytes(b"")
        with pytest.raises(IndependentMediaVerificationError, match="Zero-byte"):
            verify_media_artifact_independently(
                empty_file,
                allowed_root=tmp_path,
                declared_sha256=f"sha256:{hashlib.sha256(b'').hexdigest()}",
                declared_media_kind="video",
            )

        # Truncated / byte_size mismatch rejected
        with pytest.raises(IndependentMediaVerificationError, match="Truncated"):
            verify_media_artifact_independently(
                png_file,
                allowed_root=tmp_path,
                declared_sha256=png_sha,
                declared_media_kind="image",
                declared_byte_size=len(png_bytes) + 10,
            )

        # Wrong digest rejected
        with pytest.raises(IndependentMediaVerificationError, match="SHA-256 mismatch"):
            verify_media_artifact_independently(
                png_file,
                allowed_root=tmp_path,
                declared_sha256="sha256:" + "ff" * 32,
                declared_media_kind="image",
            )

        # Spoofed extension / wrong magic header rejected (text file pretending to be MP4 video)
        spoofed = tmp_path / "spoofed.mp4"
        spoofed_bytes = b"not an mp4 container at all"
        spoofed.write_bytes(spoofed_bytes)
        with pytest.raises(IndependentMediaVerificationError, match="Spoofed or corrupted video"):
            verify_media_artifact_independently(
                spoofed,
                allowed_root=tmp_path,
                declared_sha256=f"sha256:{hashlib.sha256(spoofed_bytes).hexdigest()}",
                declared_media_kind="video",
            )

    def test_causal_provenance_chain_verifies_parent_links_and_rejects_broken_chain(self) -> None:
        parent = make_valid_passport()
        child_chain = parent.causal_chain.model_copy(
            update={
                "transaction_id": "tx_12346",
                "command_id": "cmd_split_2",
                "parent_passport_hashes": (parent.passport_hash,),
            }
        )
        child = ArtifactPassport(
            artifact_id="art_clip_split_2",
            artifact_type="timeline_clip",
            content_hash="sha256:" + "bb" * 32,
            causal_chain=child_chain,
            execution_proof=parent.execution_proof,
        ).with_computed_hash()

        assert verify_causal_provenance_chain([parent, child]) is True
        # Missing parent in sequence fails closed
        assert verify_causal_provenance_chain([child]) is False
        # Tampered child fails closed
        tampered_child = child.model_copy(update={"content_hash": "sha256:" + "cc" * 32})
        assert verify_causal_provenance_chain([parent, tampered_child]) is False
