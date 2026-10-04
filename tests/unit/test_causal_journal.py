"""The causal journal's laws: append-only, chained, idempotent, fail-closed.

These are unit-level experiments against a real SQLite file (no mocks): the
journal's contract *is* its durable behaviour, so the tests open the file,
write, close, reopen and tamper with the bytes the way an operator or an
attacker would.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from nexus_ai_agent.causal.journal import TABLE_NAME, CausalJournal
from nexus_ai_agent.causal.models import (
    GENESIS_HASH,
    ActorRef,
    CausalRefused,
    JournalCorrupted,
    NodeRef,
    PassportRefused,
    Stage,
    artifact_node_id,
    attempt_node_id,
    digest_of,
    digest_of_text,
    job_node_id,
    record_key_for,
    request_node_id,
)

RECORDER = ActorRef(kind="service", actor_id="test")


def _job(journal: CausalJournal, *, job_id: str = "abc123") -> None:
    request = NodeRef(
        stage=Stage.REQUEST, node_id=request_node_id("creative_render", "key-1"), role="request"
    )
    journal.append(
        stage=Stage.REQUEST,
        subject=request,
        actor=RECORDER,
        facts={
            "job_type": "creative_render",
            "idempotency_digest": digest_of_text("key-1"),
            "payload_digest": digest_of({"a": 1}),
        },
    )
    journal.append(
        stage=Stage.JOB,
        subject=NodeRef(stage=Stage.JOB, node_id=job_node_id(job_id), role="job"),
        actor=RECORDER,
        parents=(request,),
        facts={
            "job_type": "creative_render",
            "idempotency_digest": digest_of_text("key-1"),
            "payload_digest": digest_of({"a": 1}),
            "created": True,
        },
    )


def test_append_is_chained_from_genesis_and_verifies(tmp_path: Path) -> None:
    journal = CausalJournal(tmp_path / "causal.sqlite")
    _job(journal)

    records = journal.records()
    assert [record.seq for record in records] == [1, 2]
    assert records[0].prev_hash == GENESIS_HASH
    assert records[1].prev_hash == records[0].record_hash
    assert all(record.digest_matches() for record in records)
    verification = journal.verify_chain()
    assert verification.ok
    assert verification.record_count == 2
    assert verification.head_hash == records[-1].record_hash == journal.head_hash()


def test_replaying_the_same_committed_fact_creates_no_second_record(tmp_path: Path) -> None:
    """A lost acknowledgement must collapse, not duplicate (identity law)."""
    ticks = iter(["2026-10-04T00:00:00+00:00", "2026-10-04T00:00:01+00:00"])
    journal = CausalJournal(tmp_path / "causal.sqlite", now=lambda: next(ticks))
    _job(journal)
    before = journal.count()

    request = NodeRef(
        stage=Stage.REQUEST, node_id=request_node_id("creative_render", "key-1"), role="request"
    )
    replay = journal.append(
        stage=Stage.REQUEST,
        subject=request,
        actor=RECORDER,
        facts={
            "job_type": "creative_render",
            "idempotency_digest": digest_of_text("key-1"),
            "payload_digest": digest_of({"a": 1}),
        },
    )

    assert replay.created is False
    assert replay.duplicate_of_seq == 1
    assert journal.count() == before
    # The original observation keeps its own time; the replay added nothing.
    assert journal.records()[0].recorded_at == "2026-10-04T00:00:00+00:00"
    assert journal.verify_chain().ok


def test_record_key_excludes_time_but_binds_content() -> None:
    subject = NodeRef(stage=Stage.JOB, node_id=job_node_id("j1"), role="job")
    facts = {"job_type": "t", "created": True}
    first = record_key_for(
        stage=Stage.JOB, subject=subject, parents=(), actor=RECORDER, authority=None, facts=facts
    )
    second = record_key_for(
        stage=Stage.JOB,
        subject=subject,
        parents=(),
        actor=RECORDER,
        authority=None,
        facts={**facts, "created": False},
    )
    assert first == record_key_for(
        stage=Stage.JOB, subject=subject, parents=(), actor=RECORDER, authority=None, facts=facts
    )
    assert first != second


def test_in_place_edit_is_detected(tmp_path: Path) -> None:
    db = tmp_path / "causal.sqlite"
    journal = CausalJournal(db)
    _job(journal)
    journal.append(
        stage=Stage.ATTEMPT,
        subject=NodeRef(stage=Stage.ATTEMPT, node_id=attempt_node_id("abc123", 1), role="r"),
        actor=RECORDER,
        parents=(NodeRef(stage=Stage.JOB, node_id=job_node_id("abc123"), role="job"),),
        facts={"job_type": "creative_render", "attempt": 1, "reservation": "granted"},
    )

    with sqlite3.connect(db) as connection:
        connection.execute(
            f"UPDATE {TABLE_NAME} SET record_json = replace(record_json, ?, ?) WHERE seq = 3",
            ('"attempt":1', '"attempt":9'),
        )

    verification = journal.verify_chain()
    assert verification.ok is False
    assert verification.first_bad_seq == 3
    assert "recomputed" in (verification.reason or "")


def test_interior_deletion_is_detected(tmp_path: Path) -> None:
    db = tmp_path / "causal.sqlite"
    journal = CausalJournal(db)
    _job(journal)
    with sqlite3.connect(db) as connection:
        connection.execute(f"DELETE FROM {TABLE_NAME} WHERE seq = 1")

    verification = journal.verify_chain()
    assert verification.ok is False
    assert verification.reason is not None
    assert "sequence gap" in verification.reason


def test_truncation_is_detected_against_an_anchor(tmp_path: Path) -> None:
    db = tmp_path / "causal.sqlite"
    journal = CausalJournal(db)
    _job(journal)
    head, count = journal.head_hash(), journal.count()

    with sqlite3.connect(db) as connection:
        connection.execute(f"DELETE FROM {TABLE_NAME} WHERE seq = 2")

    # Internally consistent... but not the history that was anchored.
    assert journal.verify_chain().ok is True
    anchored = journal.verify_chain(expected_head=head, expected_count=count)
    assert anchored.ok is False
    assert "anchor" in (anchored.reason or "") or "count" in (anchored.reason or "")


def test_append_refuses_to_extend_a_corrupted_chain(tmp_path: Path) -> None:
    db = tmp_path / "causal.sqlite"
    journal = CausalJournal(db)
    _job(journal)
    with sqlite3.connect(db) as connection:
        connection.execute(
            f"UPDATE {TABLE_NAME} SET record_json = 'not json' WHERE seq = 2",
        )
    with pytest.raises(JournalCorrupted):
        journal.append(
            stage=Stage.ATTEMPT,
            subject=NodeRef(stage=Stage.ATTEMPT, node_id=attempt_node_id("abc123", 1), role="r"),
            actor=RECORDER,
            facts={"job_type": "creative_render", "attempt": 1, "reservation": "granted"},
        )


def test_unplanned_facts_are_refused(tmp_path: Path) -> None:
    journal = CausalJournal(tmp_path / "causal.sqlite")
    with pytest.raises(CausalRefused, match="unplanned keys"):
        journal.append(
            stage=Stage.REQUEST,
            subject=NodeRef(stage=Stage.REQUEST, node_id="request:x", role="r"),
            actor=RECORDER,
            facts={"job_type": "t", "user_id": 42},
        )
    assert journal.count() == 0


@pytest.mark.parametrize(
    "facts",
    [
        {"job_type": "t", "idempotency_digest": float("nan")},
        {"job_type": "t", "idempotency_digest": float("inf")},
        {"job_type": "t", "idempotency_digest": {"nested": {"too": "deep"}}},
        {"job_type": "t", "idempotency_digest": b"bytes"},
        {"job_type": "t", "idempotency_digest": "x" * (64 * 1024 + 1)},
    ],
)
def test_non_json_or_oversized_facts_are_refused(tmp_path: Path, facts: dict[str, object]) -> None:
    journal = CausalJournal(tmp_path / "causal.sqlite")
    with pytest.raises(CausalRefused):
        journal.append(
            stage=Stage.REQUEST,
            subject=NodeRef(stage=Stage.REQUEST, node_id="request:x", role="r"),
            actor=RECORDER,
            facts=facts,
        )


def test_artifact_identity_is_content_addressed() -> None:
    digest = "sha256:" + "ab" * 32
    assert artifact_node_id(digest) == digest
    with pytest.raises(CausalRefused):
        artifact_node_id("sha256:short")


def test_journal_owns_exactly_one_table(tmp_path: Path) -> None:
    """A witness that could write other tables would be a second authority."""
    db = tmp_path / "causal.sqlite"
    journal = CausalJournal(db)
    _job(journal)
    with sqlite3.connect(db) as connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }
    assert tables == {TABLE_NAME}


def test_reopening_the_file_preserves_the_chain(tmp_path: Path) -> None:
    db = tmp_path / "causal.sqlite"
    first = CausalJournal(db)
    _job(first)
    head = first.head_hash()

    second = CausalJournal(db)
    assert second.head_hash() == head
    assert second.verify_chain().ok
    assert [record.subject.node_id for record in second.records_for(job_node_id("abc123"))] == [
        job_node_id("abc123")
    ]


def test_stage_without_a_fact_contract_cannot_be_recorded(tmp_path: Path) -> None:
    """The unproduced spine stages are a vocabulary, not a writable surface."""
    journal = CausalJournal(tmp_path / "causal.sqlite")
    with pytest.raises(CausalRefused, match="no fact contract"):
        journal.append(
            stage=Stage.INTENT,
            subject=NodeRef(stage=Stage.INTENT, node_id="intent:x", role="r"),
            actor=RECORDER,
            facts={},
        )


def test_refusals_carry_typed_reason_codes() -> None:
    refusal = PassportRefused("evidence_missing", "no record")
    assert refusal.reason_code == "evidence_missing"
    assert "evidence_missing" in str(refusal)
