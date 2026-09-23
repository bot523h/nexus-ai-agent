"""Concurrency and replay proofs for P2 INBOX / RECEIPT."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from nexus_ai_agent.adapters.update_inbox import UpdateInboxStore
from nexus_ai_agent.domain.inbox import AcceptOutcome, ReceiptStatus


def test_concurrent_accept_same_update_id_one_accepted(tmp_path: Path) -> None:
    db = tmp_path / "inbox.sqlite3"
    # Ensure schema exists before the race.
    UpdateInboxStore(db)
    barrier = threading.Barrier(12)
    outcomes: list[AcceptOutcome] = []
    lock = threading.Lock()

    def worker() -> None:
        store = UpdateInboxStore(db)
        barrier.wait(timeout=5)
        outcome, _receipt = store.accept(999, payload={"n": 1})
        with lock:
            outcomes.append(outcome)

    threads = [threading.Thread(target=worker) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
        assert not t.is_alive()

    assert outcomes.count(AcceptOutcome.ACCEPTED) == 1
    assert outcomes.count(AcceptOutcome.DUPLICATE) == 11
    store = UpdateInboxStore(db)
    receipt = store.get(999)
    assert receipt is not None
    assert receipt.status is ReceiptStatus.RECEIVED


def test_replay_after_processed_is_noop(tmp_path: Path) -> None:
    store = UpdateInboxStore(tmp_path / "inbox.sqlite3")
    o, r = store.accept(55, payload={"cmd": "/start"})
    assert o is AcceptOutcome.ACCEPTED
    store.begin_processing(55, receipt_token=r.receipt_token)
    store.mark_processed(55, receipt_token=r.receipt_token)

    side_effects = 0

    def would_process(update_id: int) -> bool:
        nonlocal side_effects
        outcome, receipt = store.accept(update_id)
        if outcome is AcceptOutcome.DUPLICATE and receipt.status is ReceiptStatus.PROCESSED:
            return False  # no second effect
        if outcome is AcceptOutcome.ACCEPTED:
            store.begin_processing(update_id, receipt_token=receipt.receipt_token)
            store.mark_processed(update_id, receipt_token=receipt.receipt_token)
            side_effects += 1
            return True
        return False

    assert would_process(55) is False
    assert would_process(55) is False
    assert side_effects == 0
    final = store.get(55)
    assert final is not None and final.status is ReceiptStatus.PROCESSED
    assert final.attempts == 1


def test_many_distinct_ids_all_accepted(tmp_path: Path) -> None:
    db = tmp_path / "inbox.sqlite3"
    UpdateInboxStore(db)
    n = 100

    def accept_one(uid: int) -> AcceptOutcome:
        store = UpdateInboxStore(db)
        outcome, _ = store.accept(uid, payload={"i": uid})
        return outcome

    with ThreadPoolExecutor(max_workers=16) as pool:
        futures = [pool.submit(accept_one, i + 1) for i in range(n)]
        results = [f.result(timeout=30) for f in as_completed(futures)]

    assert results.count(AcceptOutcome.ACCEPTED) == n
    store = UpdateInboxStore(db)
    for i in range(n):
        assert store.get(i + 1) is not None


def test_process_pipeline_is_single_flight_per_id(tmp_path: Path) -> None:
    """Two workers racing begin_processing: only one may hold PROCESSING."""
    db = tmp_path / "inbox.sqlite3"
    bootstrap = UpdateInboxStore(db)
    _, r = bootstrap.accept(77)
    token = r.receipt_token
    barrier = threading.Barrier(2)
    wins = 0
    lock = threading.Lock()

    def race() -> None:
        nonlocal wins
        store = UpdateInboxStore(db)
        barrier.wait(timeout=5)
        try:
            store.begin_processing(77, receipt_token=token)
            with lock:
                wins += 1
        except Exception:
            # Illegal transition or lost CAS — loser path.
            pass

    t1 = threading.Thread(target=race)
    t2 = threading.Thread(target=race)
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)
    # Exactly one successful transition to PROCESSING.
    assert wins == 1
    final = UpdateInboxStore(db).get(77)
    assert final is not None
    assert final.status is ReceiptStatus.PROCESSING
    assert final.attempts == 1
