"""Reliability contracts: P1 Claim+Lease, P2 Inbox/Receipt, P4 Effect-Key — M0.

Acceptance:
  A: two claims concurrent => exactly one winner
  B: same update_id twice => one logical receipt
  C: expired lease => recoverable
  D: duplicate logical effect => effect-key stable

No network, no flaky timing, deterministic.
"""

import threading
from datetime import datetime, timedelta, timezone

from nexus_ai_agent.observability.reliability import (
    ClaimResult,
    EffectRecord,
    InboxMessage,
    InMemoryClaimLease,
    InMemoryEffectStore,
    InMemoryInbox,
    Receipt,
    compute_effect_key,
    invariant_effect_key_stable,
    invariant_inbox_idempotent,
    invariant_single_winner_claim,
)


def test_claim_single_winner_concurrent():
    """A: two claims concurrent => exactly one winner."""
    store = InMemoryClaimLease()
    results: list = []

    def try_claim(owner, store=store, results=results):  # noqa: B023
        res = store.try_claim("resource-1", owner, ttl=timedelta(seconds=10))
        results.append(res)

    t1 = threading.Thread(target=try_claim, args=("owner-a",))
    t2 = threading.Thread(target=try_claim, args=("owner-b",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert invariant_single_winner_claim(results)
    # Exactly one claimed, one already_claimed
    claimed = [r for r in results if r[0] == ClaimResult.CLAIMED]
    already = [r for r in results if r[0] == ClaimResult.ALREADY_CLAIMED]
    assert len(claimed) == 1
    assert len(already) == 1


def test_claim_same_owner_idempotent():
    store = InMemoryClaimLease()
    r1, _ = store.try_claim("res", "owner", timedelta(seconds=10))
    r2, _ = store.try_claim("res", "owner", timedelta(seconds=10))
    assert r1 == ClaimResult.CLAIMED
    assert r2 == ClaimResult.CLAIMED  # same owner, idempotent


def test_claim_expired_recoverable():
    """C: expired lease => recoverable."""
    store = InMemoryClaimLease()
    store.try_claim("res", "owner-a", timedelta(seconds=10))
    # Force expire
    store._force_expire("res")
    # Now owner-b should be able to claim
    result, lease = store.try_claim("res", "owner-b", timedelta(seconds=10))
    assert result == ClaimResult.CLAIMED
    assert lease is not None
    assert lease.owner_id == "owner-b"


def test_recover_expired_lists_recovered():
    store = InMemoryClaimLease()
    store.try_claim("r1", "owner", timedelta(seconds=10))
    store.try_claim("r2", "owner", timedelta(seconds=10))
    store._force_expire("r1")
    recovered = store.recover_expired()
    assert "r1" in recovered
    assert "r2" not in recovered


def test_lease_renew():
    store = InMemoryClaimLease()
    store.try_claim("res", "owner", timedelta(seconds=10))
    ok = store.renew("res", "owner", timedelta(seconds=20))
    assert ok is True
    # Wrong owner cannot renew
    ok2 = store.renew("res", "other", timedelta(seconds=20))
    assert ok2 is False


def test_lease_release():
    store = InMemoryClaimLease()
    store.try_claim("res", "owner", timedelta(seconds=10))
    ok = store.release("res", "other")
    assert ok is False
    ok = store.release("res", "owner")
    assert ok is True
    # Now claimable again
    result, _ = store.try_claim("res", "new-owner", timedelta(seconds=10))
    assert result == ClaimResult.CLAIMED


def test_inbox_same_update_id_one_receipt():
    """B: same update_id twice => one logical receipt."""
    inbox = InMemoryInbox()
    msg = InboxMessage(message_id="update-123", payload={"text": "hi"})
    is_new1, receipt1 = inbox.try_receive(msg)
    assert is_new1 is True
    assert receipt1 is None

    # Store receipt after processing
    receipt = Receipt(
        message_id="update-123",
        receipt_id="receipt-xyz",
        processed_at=datetime.now(timezone.utc),
        result={"ok": True},
    )
    inbox.store_receipt(receipt)

    # Second receive with same id
    is_new2, receipt2 = inbox.try_receive(msg)
    assert is_new2 is False
    assert receipt2 is not None
    assert receipt2.receipt_id == "receipt-xyz"

    # Invariant: same receipt_id
    assert invariant_inbox_idempotent([receipt, receipt2])


def test_inbox_concurrent_processing():
    inbox = InMemoryInbox()
    msg = InboxMessage(message_id="update-456", payload={})
    is_new1, _ = inbox.try_receive(msg)
    assert is_new1 is True
    # Second concurrent try while first is processing (no receipt yet)
    is_new2, receipt2 = inbox.try_receive(msg)
    assert is_new2 is False
    assert receipt2 is None  # still processing


def test_effect_key_stable():
    """D: duplicate logical effect => effect-key stable."""
    params = {"chat_id": 123, "text": "hello"}
    k1 = compute_effect_key("send_message", params)
    k2 = compute_effect_key("send_message", params)
    assert k1 == k2
    assert invariant_effect_key_stable("send_message", params, k1, k2)


def test_effect_key_different_params_different_key():
    k1 = compute_effect_key("send_message", {"chat_id": 1, "text": "hi"})
    k2 = compute_effect_key("send_message", {"chat_id": 2, "text": "hi"})
    assert k1 != k2


def test_effect_key_sorted_params():
    # Order of keys should not matter
    k1 = compute_effect_key("op", {"a": 1, "b": 2})
    k2 = compute_effect_key("op", {"b": 2, "a": 1})
    assert k1 == k2


def test_effect_store_deduplication():
    store = InMemoryEffectStore()
    key = compute_effect_key("send_message", {"chat_id": 1})
    rec = EffectRecord(effect_key=key, operation="send_message", result={"message_id": 10})
    is_new, existing = store.try_record_effect(rec)
    assert is_new is True
    assert existing is None

    # Duplicate
    rec2 = EffectRecord(effect_key=key, operation="send_message", result={"message_id": 10})
    is_new2, existing2 = store.try_record_effect(rec2)
    assert is_new2 is False
    assert existing2 is not None
    assert existing2.effect_key == key


def test_effect_key_no_secret_leakage():
    # Effect key is hash, not raw params
    params = {"secret": "should_not_appear_in_key"}
    key = compute_effect_key("op", params)
    assert "should_not_appear_in_key" not in key
    assert len(key) == 32  # truncated sha256 hex


def test_claim_lease_invariant_single_winner_property():
    # Property test without randomness: multiple owners, one resource
    for _ in range(5):  # repeat to catch race
        store = InMemoryClaimLease()
        results: list = []
        owners = [f"owner-{i}" for i in range(5)]

        def claim(owner, store=store, results=results):  # noqa: B023
            r = store.try_claim("shared", owner, timedelta(seconds=10))
            results.append(r)

        threads = [threading.Thread(target=claim, args=(o,)) for o in owners]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert invariant_single_winner_claim(results)
