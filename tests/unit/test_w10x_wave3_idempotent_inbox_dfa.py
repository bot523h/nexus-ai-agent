"""Wave 3: idempotent inbox DFA and authenticated callback algebra."""

from __future__ import annotations

import hashlib

import pytest

from nexus_ai_agent.adapters.sqlite_update_inbox import SQLiteUpdateInbox
from nexus_ai_agent.domain.idempotent_inbox import (
    ByzantineUpdateConflictError,
    CallbackAction,
    CallbackActionEnvelope,
    CallbackCodecError,
    InboxDecision,
    InboxReceipt,
    InboxReceiptTamperError,
    InboxState,
    InvalidInboxTransitionError,
    StaleCallbackError,
    advance,
    payload_digest,
)

SECRET = b"wave3-test-secret"
DIGEST_A = hashlib.sha256(b"payload-a").hexdigest()
DIGEST_B = hashlib.sha256(b"payload-b").hexdigest()
DIGEST_C = hashlib.sha256(b"payload-c").hexdigest()


def test_dfa_all_legal_transitions() -> None:
    assert advance(InboxState.RECEIVED, InboxState.CLAIMED) is InboxState.CLAIMED
    assert advance(InboxState.RECEIVED, InboxState.QUARANTINED) is InboxState.QUARANTINED
    assert advance(InboxState.CLAIMED, InboxState.COMMITTED) is InboxState.COMMITTED
    assert advance(InboxState.CLAIMED, InboxState.QUARANTINED) is InboxState.QUARANTINED
    assert advance(InboxState.COMMITTED, InboxState.COMMITTED) is InboxState.COMMITTED
    assert advance(InboxState.QUARANTINED, InboxState.QUARANTINED) is InboxState.QUARANTINED


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (InboxState.RECEIVED, InboxState.RECEIVED),
        (InboxState.RECEIVED, InboxState.COMMITTED),
        (InboxState.CLAIMED, InboxState.RECEIVED),
        (InboxState.CLAIMED, InboxState.CLAIMED),
        (InboxState.COMMITTED, InboxState.CLAIMED),
        (InboxState.COMMITTED, InboxState.QUARANTINED),
        (InboxState.QUARANTINED, InboxState.COMMITTED),
        (InboxState.QUARANTINED, InboxState.CLAIMED),
    ],
)
def test_dfa_rejects_illegal_transitions(current: InboxState, target: InboxState) -> None:
    with pytest.raises(InvalidInboxTransitionError):
        advance(current, target)


def test_dfa_rejects_non_states() -> None:
    with pytest.raises(TypeError):
        advance("RECEIVED", InboxState.CLAIMED)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        advance(InboxState.RECEIVED, "CLAIMED")  # type: ignore[arg-type]


def test_payload_digest_accepts_bytes_like() -> None:
    assert payload_digest(b"x") == hashlib.sha256(b"x").hexdigest()
    assert payload_digest(bytearray(b"x")) == hashlib.sha256(b"x").hexdigest()
    assert payload_digest(memoryview(b"x")) == hashlib.sha256(b"x").hexdigest()
    with pytest.raises(TypeError):
        payload_digest("x")  # type: ignore[arg-type]


def test_receipt_validation_and_seal() -> None:
    receipt = InboxReceipt.create("telegram", 7, DIGEST_A, InboxState.CLAIMED)
    receipt.verify()
    assert receipt.compute_sealed_digest() == receipt.sealed_digest
    assert InboxReceipt.from_json(receipt.to_json()) == receipt

    bad: list[dict[str, object]] = [
        {
            "source_scope": "",
            "update_id": 1,
            "payload_digest": DIGEST_A,
            "state": InboxState.RECEIVED,
            "sealed_digest": "0" * 64,
        },
        {
            "source_scope": "s",
            "update_id": True,
            "payload_digest": DIGEST_A,
            "state": InboxState.RECEIVED,
            "sealed_digest": "0" * 64,
        },
        {
            "source_scope": "s",
            "update_id": "1",
            "payload_digest": DIGEST_A,
            "state": InboxState.RECEIVED,
            "sealed_digest": "0" * 64,
        },
        {
            "source_scope": "s",
            "update_id": -1,
            "payload_digest": DIGEST_A,
            "state": InboxState.RECEIVED,
            "sealed_digest": "0" * 64,
        },
        {
            "source_scope": "s",
            "update_id": 1,
            "payload_digest": DIGEST_A,
            "state": "RECEIVED",
            "sealed_digest": "0" * 64,
        },
        {
            "source_scope": "s",
            "update_id": 1,
            "payload_digest": "short",
            "state": InboxState.RECEIVED,
            "sealed_digest": "0" * 64,
        },
        {
            "source_scope": "s",
            "update_id": 1,
            "payload_digest": "z" * 64,
            "state": InboxState.RECEIVED,
            "sealed_digest": "0" * 64,
        },
        {
            "source_scope": "s",
            "update_id": 1,
            "payload_digest": DIGEST_A,
            "state": InboxState.RECEIVED,
            "sealed_digest": "short",
        },
    ]
    for kwargs in bad:
        with pytest.raises((TypeError, ValueError)):
            InboxReceipt(**kwargs)  # type: ignore[arg-type]


def test_receipt_verify_detects_tamper() -> None:
    receipt = InboxReceipt.create("telegram", 7, DIGEST_A, InboxState.CLAIMED)
    forged = InboxReceipt(
        source_scope=receipt.source_scope,
        update_id=receipt.update_id,
        payload_digest=DIGEST_B,
        state=receipt.state,
        sealed_digest=receipt.sealed_digest,
    )
    with pytest.raises(InboxReceiptTamperError):
        forged.verify()


def test_receipt_from_json_rejects_corruption() -> None:
    with pytest.raises(InboxReceiptTamperError):
        InboxReceipt.from_json("{not json")
    with pytest.raises(InboxReceiptTamperError):
        InboxReceipt.from_json("[]")
    with pytest.raises(InboxReceiptTamperError):
        InboxReceipt.from_json("{}")
    receipt = InboxReceipt.create("telegram", 1, DIGEST_A, InboxState.CLAIMED)
    document = receipt.to_json().replace(receipt.sealed_digest, "0" * 64)
    with pytest.raises(InboxReceiptTamperError):
        InboxReceipt.from_json(document)


def test_decision_validation() -> None:
    receipt = InboxReceipt.create("telegram", 1, DIGEST_A, InboxState.CLAIMED)
    assert InboxDecision(receipt=receipt, replayed=False, applied=True).applied
    with pytest.raises(TypeError):
        InboxDecision(receipt="x", replayed=False, applied=True)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        InboxDecision(receipt=receipt, replayed=1, applied=True)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        InboxDecision(receipt=receipt, replayed=True, applied=True)


def test_callback_roundtrip_for_every_action() -> None:
    for index, action in enumerate(CallbackAction):
        envelope = CallbackActionEnvelope(action=action, target="en", session_epoch=index)
        encoded = envelope.encode(SECRET)
        assert encoded.isascii() and len(encoded.encode("ascii")) <= 64
        assert CallbackActionEnvelope.decode(encoded, SECRET) == envelope


def test_callback_rejects_malformed_data() -> None:
    good = CallbackActionEnvelope(CallbackAction.SET_LANGUAGE, "fa", 3).encode(SECRET)
    with pytest.raises(CallbackCodecError):
        CallbackActionEnvelope.decode(3, SECRET)  # type: ignore[arg-type]
    with pytest.raises(CallbackCodecError):
        CallbackActionEnvelope.decode("", SECRET)
    with pytest.raises(CallbackCodecError):
        CallbackActionEnvelope.decode("x" * 65, SECRET)
    with pytest.raises(CallbackCodecError):
        CallbackActionEnvelope.decode("é.l.fa.1.abcdef12", SECRET)
    with pytest.raises(CallbackCodecError):
        CallbackActionEnvelope.decode("1.l.fa.1", SECRET)
    with pytest.raises(CallbackCodecError):
        CallbackActionEnvelope.decode("1.l.fa.x.abcdef12", SECRET)
    with pytest.raises(CallbackCodecError):
        CallbackActionEnvelope.decode("1.z.fa.1.abcdef12", SECRET)
    tampered = good[:-1] + ("0" if good[-1] != "0" else "1")
    with pytest.raises(CallbackCodecError):
        CallbackActionEnvelope.decode(tampered, SECRET)
    with pytest.raises(CallbackCodecError):
        CallbackActionEnvelope.decode(good, b"")
    with pytest.raises(CallbackCodecError):
        CallbackActionEnvelope.decode(good, "secret")  # type: ignore[arg-type]


def test_callback_stale_epoch_and_type_guard() -> None:
    encoded = CallbackActionEnvelope(CallbackAction.SET_AGENT, "nagar", 5).encode(SECRET)
    assert CallbackActionEnvelope.decode(encoded, SECRET, minimum_epoch=5).session_epoch == 5
    with pytest.raises(StaleCallbackError):
        CallbackActionEnvelope.decode(encoded, SECRET, minimum_epoch=6)
    with pytest.raises(TypeError):
        CallbackActionEnvelope.decode(encoded, SECRET, minimum_epoch=True)


def test_callback_envelope_validation() -> None:
    bad: list[dict[str, object]] = [
        {"action": "l", "target": "fa", "session_epoch": 1},
        {"action": CallbackAction.SET_LANGUAGE, "target": "", "session_epoch": 1},
        {"action": CallbackAction.SET_LANGUAGE, "target": 3, "session_epoch": 1},
        {"action": CallbackAction.SET_LANGUAGE, "target": "fa.en", "session_epoch": 1},
        {"action": CallbackAction.SET_LANGUAGE, "target": "فا", "session_epoch": 1},
        {"action": CallbackAction.SET_LANGUAGE, "target": "x" * 25, "session_epoch": 1},
        {"action": CallbackAction.SET_LANGUAGE, "target": "fa", "session_epoch": True},
        {"action": CallbackAction.SET_LANGUAGE, "target": "fa", "session_epoch": "1"},
        {"action": CallbackAction.SET_LANGUAGE, "target": "fa", "session_epoch": -1},
        {
            "action": CallbackAction.SET_LANGUAGE,
            "target": "fa",
            "session_epoch": 1,
            "version": True,
        },
        {"action": CallbackAction.SET_LANGUAGE, "target": "fa", "session_epoch": 1, "version": 2},
    ]
    for kwargs in bad:
        with pytest.raises((TypeError, CallbackCodecError)):
            CallbackActionEnvelope(**kwargs)  # type: ignore[arg-type]


def test_callback_encode_rejects_empty_secret_and_oversize_target() -> None:
    envelope = CallbackActionEnvelope(CallbackAction.SET_LANGUAGE, "fa", 1)
    with pytest.raises(CallbackCodecError):
        envelope.encode(b"")
    with pytest.raises(CallbackCodecError):
        envelope.encode("secret")  # type: ignore[arg-type]
    huge = CallbackActionEnvelope(CallbackAction.SET_LANGUAGE, "x" * 24, 10**100)
    with pytest.raises(CallbackCodecError):
        huge.encode(SECRET)


def test_inbox_apply_is_idempotent() -> None:
    inbox = SQLiteUpdateInbox()
    try:
        first = inbox.apply("telegram", 42, DIGEST_A)
        second = inbox.apply("telegram", 42, DIGEST_A)
        assert first.receipt.state is InboxState.COMMITTED
        assert first.applied is True and first.replayed is False
        assert second.receipt == first.receipt
        assert second.applied is False and second.replayed is True
        assert len(inbox) == 1
        assert inbox.state_of("telegram", 42) is InboxState.COMMITTED
        assert inbox.receipt("telegram", 42) == first.receipt
    finally:
        inbox.close()


def test_inbox_byzantine_conflict_rolls_back() -> None:
    inbox = SQLiteUpdateInbox()
    try:
        inbox.ingest("telegram", 42, DIGEST_A)
        with pytest.raises(ByzantineUpdateConflictError):
            inbox.ingest("telegram", 42, DIGEST_B)
        assert inbox.receipt("telegram", 42).payload_digest == DIGEST_A
        assert inbox.state_of("telegram", 42) is InboxState.CLAIMED
    finally:
        inbox.close()


def test_inbox_ingest_commit_and_replay() -> None:
    inbox = SQLiteUpdateInbox()
    try:
        claimed = inbox.ingest("telegram", 1, DIGEST_A)
        assert claimed.receipt.state is InboxState.CLAIMED
        assert inbox.ingest("telegram", 1, DIGEST_A).replayed is True
        committed = inbox.commit("telegram", 1)
        assert committed.receipt.state is InboxState.COMMITTED
        again = inbox.commit("telegram", 1)
        assert again.replayed is True and again.receipt == committed.receipt
        assert inbox.apply("telegram", 1, DIGEST_A).replayed is True
    finally:
        inbox.close()


def test_inbox_commit_rejects_received_and_missing() -> None:
    inbox = SQLiteUpdateInbox()
    try:
        with pytest.raises(KeyError):
            inbox.commit("telegram", 99)
        receipt = InboxReceipt.create("telegram", 5, DIGEST_A, InboxState.RECEIVED)
        inbox.connection.execute(
            "INSERT INTO update_inbox "
            "(source_scope, update_id, payload_digest, state, receipt_json) "
            "VALUES (?, ?, ?, ?, ?)",
            ("telegram", 5, DIGEST_A, "RECEIVED", receipt.to_json()),
        )
        with pytest.raises(InvalidInboxTransitionError):
            inbox.commit("telegram", 5)
        # RECEIVED rows are the one state ingest can advance.
        advanced = inbox.ingest("telegram", 5, DIGEST_A)
        assert advanced.receipt.state is InboxState.CLAIMED
        assert advanced.applied is True
    finally:
        inbox.close()


def test_inbox_quarantine_semantics() -> None:
    inbox = SQLiteUpdateInbox()
    try:
        inbox.ingest("telegram", 1, DIGEST_A)
        quarantined = inbox.quarantine("telegram", 1, reason="forged signature")
        assert quarantined.receipt.state is InboxState.QUARANTINED
        assert quarantined.applied is True
        replay = inbox.quarantine("telegram", 1)
        assert replay.replayed is True and replay.receipt == quarantined.receipt
        assert inbox.apply("telegram", 1, DIGEST_A).replayed is True
        with pytest.raises(KeyError):
            inbox.quarantine("telegram", 2)
        with pytest.raises(TypeError):
            inbox.quarantine("telegram", 1, reason=3)  # type: ignore[arg-type]

        inbox.ingest("telegram", 3, DIGEST_C)
        inbox.commit("telegram", 3)
        with pytest.raises(InvalidInboxTransitionError):
            inbox.quarantine("telegram", 3)
    finally:
        inbox.close()


def test_inbox_isolation_between_scopes_and_ordering() -> None:
    inbox = SQLiteUpdateInbox()
    try:
        inbox.ingest("telegram", 1, DIGEST_A)
        inbox.ingest("webhook", 1, DIGEST_B)
        assert inbox.receipt("telegram", 1).payload_digest == DIGEST_A
        assert inbox.receipt("webhook", 1).payload_digest == DIGEST_B
        assert len(inbox) == 2
        assert inbox.connection is not None
    finally:
        inbox.close()


def test_inbox_rejects_bad_keys() -> None:
    inbox = SQLiteUpdateInbox()
    try:
        with pytest.raises(ValueError):
            inbox.ingest("", 1, DIGEST_A)
        with pytest.raises(ValueError):
            inbox.ingest(3, 1, DIGEST_A)  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            inbox.ingest("telegram", True, DIGEST_A)
        with pytest.raises(TypeError):
            inbox.ingest("telegram", "1", DIGEST_A)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            inbox.ingest("telegram", -1, DIGEST_A)
        with pytest.raises(ValueError):
            inbox.ingest("telegram", 1, "short")
        with pytest.raises(ValueError):
            inbox.ingest("telegram", 1, "z" * 64)
        with pytest.raises(ValueError):
            SQLiteUpdateInbox(path="")
    finally:
        inbox.close()


def test_inbox_detects_stored_receipt_tamper() -> None:
    inbox = SQLiteUpdateInbox()
    try:
        inbox.apply("telegram", 1, DIGEST_A)
        row = inbox.connection.execute(
            "SELECT receipt_json FROM update_inbox WHERE source_scope='telegram' AND update_id=1"
        ).fetchone()
        tampered = row[0].replace(DIGEST_A, DIGEST_B)
        inbox.connection.execute(
            "UPDATE update_inbox SET receipt_json=? WHERE source_scope='telegram' AND update_id=1",
            (tampered,),
        )
        with pytest.raises(InboxReceiptTamperError):
            inbox.receipt("telegram", 1)
    finally:
        inbox.close()


def test_inbox_detects_column_disagreement() -> None:
    inbox = SQLiteUpdateInbox()
    try:
        inbox.apply("telegram", 1, DIGEST_A)
        inbox.connection.execute(
            "UPDATE update_inbox SET state='CLAIMED' WHERE source_scope='telegram' AND update_id=1"
        )
        with pytest.raises(InboxReceiptTamperError):
            inbox.receipt("telegram", 1)
        inbox.connection.execute(
            "UPDATE update_inbox SET state='BOGUS' WHERE source_scope='telegram' AND update_id=1"
        )
        with pytest.raises(InboxReceiptTamperError):
            inbox.receipt("telegram", 1)
    finally:
        inbox.close()


def test_inbox_detects_identity_and_payload_mismatch() -> None:
    inbox = SQLiteUpdateInbox()
    try:
        inbox.apply("telegram", 1, DIGEST_A)
        wrong_identity = InboxReceipt.create("other", 1, DIGEST_A, InboxState.COMMITTED)
        inbox.connection.execute(
            "UPDATE update_inbox SET receipt_json=? WHERE source_scope='telegram' AND update_id=1",
            (wrong_identity.to_json(),),
        )
        with pytest.raises(InboxReceiptTamperError):
            inbox.receipt("telegram", 1)

        inbox.connection.execute(
            "UPDATE update_inbox SET receipt_json=?, payload_digest=? "
            "WHERE source_scope='telegram' AND update_id=1",
            (
                InboxReceipt.create("telegram", 1, DIGEST_B, InboxState.COMMITTED).to_json(),
                DIGEST_A,
            ),
        )
        with pytest.raises(InboxReceiptTamperError):
            inbox.receipt("telegram", 1)
    finally:
        inbox.close()


def test_inbox_missing_receipt_and_close(tmp_path) -> None:
    path = str(tmp_path / "inbox.sqlite")
    inbox = SQLiteUpdateInbox(path)
    with pytest.raises(KeyError):
        inbox.receipt("telegram", 1)
    with pytest.raises(KeyError):
        inbox.state_of("telegram", 1)
    inbox.apply("telegram", 1, DIGEST_A)
    inbox.close()
    reopened = SQLiteUpdateInbox(path)
    try:
        assert reopened.receipt("telegram", 1).state is InboxState.COMMITTED
    finally:
        reopened.close()


def test_callback_tag_is_128_bit_and_direct_poison_pill_is_explicit() -> None:
    envelope = CallbackActionEnvelope(CallbackAction.SET_LANGUAGE, "fa", 7)
    encoded = envelope.encode(SECRET)
    assert len(encoded.rsplit(".", 1)[1]) == 32
    assert CallbackActionEnvelope.decode(encoded, SECRET).session_epoch == 7
    assert advance(InboxState.RECEIVED, InboxState.QUARANTINED) is InboxState.QUARANTINED
