"""Unit contract for P2 INBOX / RECEIPT (domain + UpdateInboxStore)."""

from __future__ import annotations

from pathlib import Path

import pytest

from nexus_ai_agent.adapters.update_inbox import UpdateInboxStore
from nexus_ai_agent.domain.inbox import (
    LEGAL_TRANSITIONS,
    AcceptOutcome,
    ReceiptStatus,
    ReceiptTransitionError,
    assert_transition_allowed,
    validate_update_id,
)


def test_domain_update_id_and_transitions() -> None:
    assert validate_update_id(1) == 1
    with pytest.raises(ValueError):
        validate_update_id(0)
    with pytest.raises(ValueError):
        validate_update_id(-3)
    with pytest.raises(TypeError):
        validate_update_id(True)  # type: ignore[arg-type]

    assert_transition_allowed(ReceiptStatus.RECEIVED, ReceiptStatus.PROCESSING)
    with pytest.raises(ReceiptTransitionError):
        assert_transition_allowed(ReceiptStatus.PROCESSED, ReceiptStatus.RECEIVED)
    # Terminal states have empty out-edges.
    assert LEGAL_TRANSITIONS[ReceiptStatus.PROCESSED] == frozenset()
    assert LEGAL_TRANSITIONS[ReceiptStatus.DEAD] == frozenset()


def test_accept_first_then_duplicate(tmp_path: Path) -> None:
    store = UpdateInboxStore(tmp_path / "inbox.sqlite3")
    o1, r1 = store.accept(42, payload={"text": "hi"})
    assert o1 is AcceptOutcome.ACCEPTED
    assert r1.update_id == 42
    assert r1.status is ReceiptStatus.RECEIVED
    assert r1.payload == {"text": "hi"}
    assert r1.receipt_token

    o2, r2 = store.accept(42, payload={"text": "replay-ignored"})
    assert o2 is AcceptOutcome.DUPLICATE
    assert r2.receipt_token == r1.receipt_token
    assert r2.payload == {"text": "hi"}  # original preserved
    assert r2.status is ReceiptStatus.RECEIVED


def test_full_happy_path_state_machine(tmp_path: Path) -> None:
    store = UpdateInboxStore(tmp_path / "inbox.sqlite3")
    _, r = store.accept(7, payload={"chat_id": 1})
    processing = store.begin_processing(7, receipt_token=r.receipt_token)
    assert processing.status is ReceiptStatus.PROCESSING
    assert processing.attempts == 1

    done = store.mark_processed(7, receipt_token=r.receipt_token)
    assert done.status is ReceiptStatus.PROCESSED
    assert done.processed_at is not None

    # Replay after processed: still DUPLICATE, status stays PROCESSED.
    o, again = store.accept(7)
    assert o is AcceptOutcome.DUPLICATE
    assert again.status is ReceiptStatus.PROCESSED

    # Illegal transition from terminal.
    with pytest.raises(ReceiptTransitionError):
        store.begin_processing(7)


def test_dead_letter_and_reclaim(tmp_path: Path) -> None:
    store = UpdateInboxStore(tmp_path / "inbox.sqlite3")
    _, r = store.accept(9)
    store.begin_processing(9, receipt_token=r.receipt_token)
    reclaimed = store.reclaim_to_received(9, receipt_token=r.receipt_token)
    assert reclaimed.status is ReceiptStatus.RECEIVED

    store.begin_processing(9, receipt_token=r.receipt_token)
    dead = store.mark_dead(9, error="poison", receipt_token=r.receipt_token)
    assert dead.status is ReceiptStatus.DEAD
    assert dead.error == "poison"
    with pytest.raises(ReceiptTransitionError):
        store.mark_processed(9)


def test_receipt_token_mismatch_rejected(tmp_path: Path) -> None:
    store = UpdateInboxStore(tmp_path / "inbox.sqlite3")
    store.accept(11)
    with pytest.raises(PermissionError):
        store.begin_processing(11, receipt_token="not-the-token")


def test_list_by_status_and_get(tmp_path: Path) -> None:
    store = UpdateInboxStore(tmp_path / "inbox.sqlite3")
    store.accept(1)
    store.accept(2)
    store.begin_processing(1)
    received = store.list_by_status(ReceiptStatus.RECEIVED)
    assert [r.update_id for r in received] == [2]
    got = store.get(1)
    assert got is not None and got.status is ReceiptStatus.PROCESSING
    assert store.get(999) is None


def test_out_of_order_ids_are_independent(tmp_path: Path) -> None:
    """High-water-mark anti-pattern: lower id after higher id must still accept."""
    store = UpdateInboxStore(tmp_path / "inbox.sqlite3")
    o_hi, _ = store.accept(100)
    o_lo, _ = store.accept(50)
    assert o_hi is AcceptOutcome.ACCEPTED
    assert o_lo is AcceptOutcome.ACCEPTED
    assert store.get(50) is not None
    assert store.get(100) is not None


def test_memory_backend(tmp_path: Path) -> None:
    del tmp_path  # unused — memory path
    store = UpdateInboxStore(":memory:")
    o, r = store.accept(1, payload={})
    assert o is AcceptOutcome.ACCEPTED
    assert r.update_id == 1
