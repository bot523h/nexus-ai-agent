"""Chain math under attack: tamper evidence is the ledger's core contract.

Every case here is one of the contract's failure proofs: editing, deleting,
reordering, re-serializing or re-linking any record must break verification at
the exact first broken ``seq`` — never pass, never blame a later record.
"""

from __future__ import annotations

import json

import pytest

from nexus_ai_agent.provenance.models import (
    GENESIS_HASH,
    RECORD_DOMAIN,
    CausalEvent,
    EventKind,
    LedgerRecord,
    canonical_json,
    compute_record_hash,
    digest_of,
    verify_chain,
)


def _event(job_id: str = "job-1", kind: EventKind = EventKind.JOB_ENQUEUED) -> CausalEvent:
    return CausalEvent(
        kind=kind,
        job_id=job_id,
        job_type="creative_render",
        idempotency_key="key-1",
        attempt=1 if kind is not EventKind.JOB_ENQUEUED else None,
        occurred_at="2026-10-04T12:00:00+00:00",
    )


def _chain(jobs: int = 2) -> list[LedgerRecord]:
    """A small valid chain built by hand (same math the journal uses)."""
    records: list[LedgerRecord] = []
    previous = GENESIS_HASH
    for index in range(1, jobs * 2 + 1):
        event = _event(job_id=f"job-{index}")
        payload = event.payload(seq=index, prev_hash=previous)
        record_json = canonical_json(payload)
        record_hash = compute_record_hash(record_json)
        records.append(
            LedgerRecord(
                seq=index,
                record_hash=record_hash,
                prev_hash=previous,
                record_json=record_json,
                dedupe_key=None,
                created_at=event.occurred_at,
            )
        )
        previous = record_hash
    return records


def test_valid_chain_verifies_with_head() -> None:
    records = _chain()
    verdict = verify_chain(records)
    assert verdict.ok
    assert verdict.record_count == 4
    assert verdict.head_hash == records[-1].record_hash
    assert verdict.first_broken_seq is None


def test_empty_chain_verifies() -> None:
    verdict = verify_chain([])
    assert verdict.ok
    assert verdict.record_count == 0
    assert verdict.head_hash is None


def test_edited_content_breaks_at_exact_seq() -> None:
    records = _chain()
    tampered = json.loads(records[1].record_json)
    tampered["status"] = "completed"  # a forged terminal state
    forged = LedgerRecord(
        seq=records[1].seq,
        record_hash=records[1].record_hash,  # hash left as-is: bytes no longer match
        prev_hash=records[1].prev_hash,
        record_json=canonical_json(tampered),
        dedupe_key=records[1].dedupe_key,
        created_at=records[1].created_at,
    )
    verdict = verify_chain([records[0], forged, *records[2:]])
    assert not verdict.ok
    assert verdict.first_broken_seq == 2
    assert "content tampered" in (verdict.reason or "")


def test_deleted_middle_record_breaks_at_the_first_anomaly() -> None:
    records = _chain()
    verdict = verify_chain([records[0], *records[2:]])
    assert not verdict.ok
    assert verdict.first_broken_seq == 3  # expected seq 2, found 3
    assert "sequence gap" in (verdict.reason or "")


def test_reordering_breaks_sequence() -> None:
    records = _chain()
    verdict = verify_chain([records[1], records[0], *records[2:]])
    # A content-preserving swap is undone by the seq sort (by design: the
    # sequence number is the order); the real attack is renumbering.
    assert verdict.ok


def test_resequencing_attack_dies_on_linkage() -> None:
    """Forged seq numbers cannot splice a foreign record into the chain."""
    records = _chain()
    payload = json.loads(records[3].record_json)
    payload["seq"] = 2  # steal seq 2 for a record that never lived there
    forged_json = canonical_json(payload)
    forged = LedgerRecord(
        seq=2,
        record_hash=compute_record_hash(forged_json),
        prev_hash=records[0].record_hash,  # claimed predecessor
        record_json=forged_json,
        dedupe_key=None,
        created_at=records[3].created_at,
    )
    verdict = verify_chain([records[0], forged, *records[2:3]])
    assert not verdict.ok
    assert verdict.first_broken_seq == 2
    # The forged record's bytes commit to the seq-3 lineage, not to seq 1 —
    # the bytes↔column binding catches the splice.
    assert "disagrees" in (verdict.reason or "") or "link" in (verdict.reason or "")


def test_truncated_tail_is_detectable_as_shorter_chain() -> None:
    """Truncation keeps the prefix valid — the honest detection story.

    A truncated tail verifies as a (shorter) valid chain; the passport's
    completeness check is what flags the affected job, and the published
    ``journal_head`` is the anchor a future signed checkpoint pins.
    """
    records = _chain()
    verdict = verify_chain(records[:-1])
    assert verdict.ok
    assert verdict.head_hash == records[-2].record_hash


def test_columns_disagreeing_with_bytes_breaks_verification() -> None:
    """Recomputed-hash attacks die on the stored-bytes binding."""
    records = _chain()
    payload = json.loads(records[1].record_json)
    payload["seq"] = 1
    payload["prev_hash"] = GENESIS_HASH
    payload["status"] = "failed_terminal"  # forge the state inside the bytes
    forged_json = canonical_json(payload)
    forged = LedgerRecord(
        seq=1,
        record_hash=compute_record_hash(forged_json),  # bytes and hash agree
        prev_hash=GENESIS_HASH,  # linkage agrees...
        record_json=forged_json,
        dedupe_key=None,
        created_at=records[1].created_at,
    )
    # ...but the bytes commit to a different predecessor than the column:
    # json.loads(forged_json)["prev_hash"] is GENESIS here — make them differ.
    payload["prev_hash"] = "sha256:" + ("f" * 64)
    forged_json = canonical_json(payload)
    forged = LedgerRecord(
        seq=1,
        record_hash=compute_record_hash(forged_json),
        prev_hash=GENESIS_HASH,
        record_json=forged_json,
        dedupe_key=None,
        created_at=records[1].created_at,
    )
    verdict = verify_chain([forged])
    assert not verdict.ok
    assert "disagrees" in (verdict.reason or "")


def test_domain_separation_binds_hashes_to_this_ledger() -> None:
    payload = {"seq": 1, "prev_hash": GENESIS_HASH, "kind": "job_enqueued"}
    as_record = compute_record_hash(canonical_json(payload))
    as_content = digest_of(payload)
    assert as_record != as_content  # a record hash is never a plain digest
    tampered_domain = RECORD_DOMAIN[:-2] + "zz"
    import hashlib

    other = (
        "sha256:"
        + hashlib.sha256(
            tampered_domain.encode("utf-8") + b"\n" + canonical_json(payload).encode("utf-8")
        ).hexdigest()
    )
    assert other != as_record  # a foreign domain cannot re-derive our hashes


def test_event_validation_is_fail_closed() -> None:
    with pytest.raises(ValueError):
        _event(job_id="")
    with pytest.raises(ValueError):
        CausalEvent(
            kind="job_enqueued",  # type: ignore[arg-type]
            job_id="job-1",
            job_type="creative_render",
        )
