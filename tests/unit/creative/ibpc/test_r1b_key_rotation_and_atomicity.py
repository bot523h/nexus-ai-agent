"""Wave 3-R1b — one transaction per consumption, and verification by recorded key.

Two defects are pinned here:

1. **Atomicity.** ``consume_approval`` used to read the binding, verify the MAC,
   compare the payload, check expiry, read the state and *then* call
   ``consume()`` — six separate transactions. A competing writer could commit
   between the verify and the transition. The whole sequence is now one
   ``BEGIN IMMEDIATE`` unit on one connection, so two workers racing for the same
   ``approval_id`` produce exactly one winner and one typed replay refusal.

2. **Key rotation.** ``approval_bindings`` did not persist the signing ``key_id``
   and verification used the *active* key, so rotating ``key-v1`` → ``key-v2``
   silently invalidated every approval issued before the rotation. The column is
   now persisted (with a deterministic migration for pre-rotation databases) and
   verification looks the key up by the recorded id: an approval signed by
   ``key-v1`` still verifies while ``key-v1`` is in the ring, and fails closed
   the moment it is revoked.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from nexus_ai_agent.nagar.ibpc.approval import (
    ApprovalAlreadyConsumedError,
    ApprovalBindingMismatchError,
    ApprovalBindingPayload,
    ApprovalExpiredError,
    ApprovalIdentity,
    DurableCanonicalApprovalLedger,
)
from nexus_ai_agent.nagar.ibpc.approval_mac import KeyProviderError, StaticKeyRing
from nexus_ai_agent.nagar.ibpc.models import ApprovalBinding

T0 = "2026-11-25T00:00:00Z"
KEY_V1 = b"1" * 32
KEY_V2 = b"2" * 32
PRINCIPAL = "prn_" + "4" * 32
PROJECT = "project-r1b"


def _ring(*, active: str = "key-v1", keys: dict[str, bytes] | None = None) -> StaticKeyRing:
    material = {"key-v1": KEY_V1, "key-v2": KEY_V2} if keys is None else keys
    return StaticKeyRing(material, active_key_id=active)


def _expiry(minutes: int = 10) -> str:
    issued = datetime.fromisoformat(T0.replace("Z", "+00:00"))
    expires = (issued + timedelta(minutes=minutes)).astimezone(timezone.utc)
    return expires.strftime("%Y-%m-%dT%H:%M:%SZ")


def _payload(**overrides: str) -> ApprovalBindingPayload:
    values = {
        "project_id": PROJECT,
        "session_id": "sess_r1b",
        "turn_id": "turn-1",
        "base_revision_id": "rev:7",
        "plan_sha256": "sha256:" + "a" * 64,
        "preview_sha256": "sha256:" + "b" * 64,
        "scope_signature": "sha256:" + "c" * 64,
        "principal_id": PRINCIPAL,
        "expires_at_utc": _expiry(),
    }
    values.update(overrides)
    return ApprovalBindingPayload(**values)


def _identity(**overrides: object) -> ApprovalIdentity:
    values = {
        "principal_id": PRINCIPAL,
        "tenant_id": "ten",
        "project_id": PROJECT,
        "session_id": "sess_r1b",
        "turn_id": "turn-1",
        "sequence_number": 1,
        "source_revision": 7,
        "source_asset_digest": "sha256:" + "d" * 64,
        "ast_digest": "sha256:" + "e" * 64,
        "plan_digest": "sha256:" + "a" * 64,
        "preview_digest": "sha256:" + "b" * 64,
    }
    values.update(overrides)
    return ApprovalIdentity(**values)  # type: ignore[arg-type]


def _register(ledger: DurableCanonicalApprovalLedger, approval_id: str) -> ApprovalBindingPayload:
    payload = _payload()
    ledger.create(
        ApprovalBinding(
            approval_id=approval_id,
            session_id=payload.session_id,
            turn_id=payload.turn_id,
            sequence_number=1,
            tenant_id="ten",
            project_id=payload.project_id,
            principal_id=payload.principal_id,
            source_revision=7,
            source_asset_digest="sha256:" + "d" * 64,
            ast_digest="sha256:" + "e" * 64,
            plan_digest=payload.plan_sha256,
            preview_digest=payload.preview_sha256,
        )
    )
    ledger.approve(approval_id, identity=_identity(), turn_is_active=lambda: True)
    ledger.bind_canonical(approval_id, payload)
    return payload


@pytest.fixture()
def ledger(tmp_path: Path):
    store = DurableCanonicalApprovalLedger(
        tmp_path / "approvals.sqlite3", key_provider=_ring(), now=lambda: T0
    )
    yield store
    store.close()


class TestTheSigningKeyIdIsPersisted:
    def test_the_binding_records_the_key_that_signed_it(self, ledger) -> None:
        _register(ledger, "ap-1")
        assert ledger.binding_key_id("ap-1") == "key-v1"

    def test_the_column_survives_a_reopen(self, tmp_path: Path) -> None:
        path = tmp_path / "approvals.sqlite3"
        first = DurableCanonicalApprovalLedger(path, key_provider=_ring(), now=lambda: T0)
        _register(first, "ap-1")
        first.close()
        second = DurableCanonicalApprovalLedger(path, key_provider=_ring(), now=lambda: T0)
        try:
            assert second.binding_key_id("ap-1") == "key-v1"
            assert second.verify_canonical("ap-1").expires_at_utc == _expiry()
        finally:
            second.close()

    def test_a_missing_key_id_fails_closed(self, ledger) -> None:
        with pytest.raises(ApprovalBindingMismatchError):
            ledger.binding_key_id("unknown-approval")


class TestKeyRotation:
    def test_an_approval_signed_before_rotation_still_consumes(self, tmp_path: Path) -> None:
        path = tmp_path / "approvals.sqlite3"
        old = DurableCanonicalApprovalLedger(path, key_provider=_ring(), now=lambda: T0)
        payload = _register(old, "ap-rot")
        old.close()

        # the ring rotates the active key; key-v1 stays for verification
        rotated = DurableCanonicalApprovalLedger(
            path, key_provider=_ring(active="key-v2"), now=lambda: T0
        )
        try:
            assert rotated.binding_key_id("ap-rot") == "key-v1"
            consumed = rotated.consume_approval(
                "ap-rot", identity=_identity(), payload=payload, job_id="job-rot"
            )
            assert consumed.state.value == "CONSUMED"
        finally:
            rotated.close()

    def test_a_new_approval_is_signed_with_the_rotated_active_key(self, tmp_path: Path) -> None:
        path = tmp_path / "approvals.sqlite3"
        store = DurableCanonicalApprovalLedger(
            path, key_provider=_ring(active="key-v2"), now=lambda: T0
        )
        try:
            _register(store, "ap-new")
            assert store.binding_key_id("ap-new") == "key-v2"
        finally:
            store.close()

    def test_a_revoked_key_fails_closed_with_a_typed_refusal(self, tmp_path: Path) -> None:
        path = tmp_path / "approvals.sqlite3"
        old = DurableCanonicalApprovalLedger(path, key_provider=_ring(), now=lambda: T0)
        payload = _register(old, "ap-revoked")
        old.close()

        # key-v1 has been removed from the ring: only key-v2 remains
        revoked = DurableCanonicalApprovalLedger(
            path,
            key_provider=StaticKeyRing({"key-v2": KEY_V2}, active_key_id="key-v2"),
            now=lambda: T0,
        )
        try:
            with pytest.raises(ApprovalBindingMismatchError) as exc:
                revoked.verify_canonical("ap-revoked")
            assert "key-v1" in str(exc.value)
            with pytest.raises(ApprovalBindingMismatchError):
                revoked.consume_approval("ap-revoked", identity=_identity(), payload=payload)
            # a refused verification never consumes the token
            assert revoked.get("ap-revoked").state.value == "APPROVED"
        finally:
            revoked.close()

    def test_verification_never_uses_the_active_key_instead_of_the_recorded_one(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "approvals.sqlite3"
        old = DurableCanonicalApprovalLedger(path, key_provider=_ring(), now=lambda: T0)
        _register(old, "ap-old")
        old.close()
        rotated = DurableCanonicalApprovalLedger(
            path, key_provider=_ring(active="key-v2"), now=lambda: T0
        )
        try:
            # if verification recomputed the MAC with key-v2 this would raise
            assert rotated.verify_canonical("ap-old").principal_id == PRINCIPAL
        finally:
            rotated.close()

    def test_an_unavailable_key_provider_is_a_typed_refusal(self, tmp_path: Path) -> None:
        class _Broken:
            active_key_id = "key-v1"

            def key(self, key_id: str):
                raise KeyProviderError("no such key")

        store = DurableCanonicalApprovalLedger(
            tmp_path / "approvals.sqlite3", key_provider=_ring(), now=lambda: T0
        )
        try:
            _register(store, "ap-broken")
            store._key_provider = _Broken()  # type: ignore[assignment]
            store._macs.clear()
            store._key_ids.clear()
            with pytest.raises(ApprovalBindingMismatchError):
                store.verify_canonical("ap-broken")
        finally:
            store.close()


class TestTheSchemaMigration:
    def test_a_pre_rotation_database_is_migrated_and_backfilled(self, tmp_path: Path) -> None:
        path = tmp_path / "approvals.sqlite3"
        legacy = sqlite3.connect(str(path))
        legacy.executescript(
            "CREATE TABLE approval_bindings ("
            " approval_id TEXT PRIMARY KEY,"
            " project_id TEXT NOT NULL,"
            " session_id TEXT NOT NULL,"
            " turn_id TEXT NOT NULL,"
            " base_revision_id TEXT NOT NULL,"
            " plan_sha256 TEXT NOT NULL,"
            " preview_sha256 TEXT NOT NULL,"
            " scope_signature TEXT NOT NULL,"
            " principal_id TEXT NOT NULL,"
            " expires_at_utc TEXT NOT NULL,"
            " mac TEXT NOT NULL);"
        )
        legacy.execute(
            "INSERT INTO approval_bindings VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "ap-legacy",
                PROJECT,
                "sess_r1b",
                "turn-1",
                "rev:7",
                "sha256:" + "a" * 64,
                "sha256:" + "b" * 64,
                "sha256:" + "c" * 64,
                PRINCIPAL,
                _expiry(),
                "0" * 64,
            ),
        )
        legacy.commit()
        legacy.close()

        store = DurableCanonicalApprovalLedger(path, key_provider=_ring(), now=lambda: T0)
        try:
            # the legacy row is backfilled with the ring's active key id
            assert store.binding_key_id("ap-legacy") == "key-v1"
        finally:
            store.close()
        columns = {
            row[1]
            for row in sqlite3.connect(str(path)).execute("PRAGMA table_info(approval_bindings)")
        }
        assert "key_id" in columns


class TestOneTransactionPerConsumption:
    @staticmethod
    def _trace(ledger) -> tuple[list[tuple[str, bool]], object]:
        """Record every statement the ledger executes plus its in-transaction flag."""
        seen: list[tuple[str, bool]] = []

        def record(sql: str) -> None:
            seen.append((sql.strip(), bool(ledger._conn.in_transaction)))

        ledger._conn.set_trace_callback(record)
        return seen, ledger._conn

    def test_verification_and_consumption_open_exactly_one_write_transaction(self, ledger) -> None:
        """The whole read → verify → expiry → CAS sequence is ONE write unit.

        Measured from SQLite itself: every statement that touches the binding or
        approval tables during one consumption runs *inside* the transaction. If
        the verify step ran in autocommit (the pre-R1b shape) a competing writer
        could rewrite the binding between the MAC check and the transition.
        """
        payload = _register(ledger, "ap-txn")
        seen, conn = self._trace(ledger)
        try:
            ledger.consume_approval("ap-txn", identity=_identity(), payload=payload)
        finally:
            conn.set_trace_callback(None)
        begins = [s for s, _ in seen if s.upper().startswith("BEGIN IMMEDIATE")]
        assert len(begins) == 1, seen
        touched = [
            (sql, inside)
            for sql, inside in seen
            if "approval_bindings" in sql or "FROM approvals" in sql or "INTO approvals" in sql
        ]
        assert touched, seen
        assert all(inside for _, inside in touched), touched
        # the transition itself happened inside that same unit
        assert any("INSERT OR REPLACE INTO approvals" in sql for sql, _ in touched), touched

    def test_a_failed_payload_check_reads_and_refuses_inside_one_unit(self, ledger) -> None:
        _register(ledger, "ap-unittest")
        seen, conn = self._trace(ledger)
        try:
            with pytest.raises(ApprovalBindingMismatchError):
                ledger.consume_approval(
                    "ap-unittest",
                    identity=_identity(),
                    payload=_payload(scope_signature="sha256:" + "f" * 64),
                )
        finally:
            conn.set_trace_callback(None)
        begins = [s for s, _ in seen if s.upper().startswith("BEGIN IMMEDIATE")]
        assert len(begins) == 1, seen
        touched = [
            inside for sql, inside in seen if "approval_bindings" in sql or "FROM approvals" in sql
        ]
        assert touched and all(touched), seen
        assert ledger.get("ap-unittest").state.value == "APPROVED"

    def test_two_connections_racing_produce_one_winner_and_one_replay_refusal(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "approvals.sqlite3"
        first = DurableCanonicalApprovalLedger(path, key_provider=_ring(), now=lambda: T0)
        payload = _register(first, "ap-race")
        second = DurableCanonicalApprovalLedger(path, key_provider=_ring(), now=lambda: T0)

        results: list[str] = []
        barrier = threading.Barrier(2)
        lock = threading.Lock()

        def consume(ledger: DurableCanonicalApprovalLedger) -> None:
            barrier.wait()
            try:
                ledger.consume_approval("ap-race", identity=_identity(), payload=payload)
                outcome = "consumed"
            except ApprovalAlreadyConsumedError:
                outcome = "replay"
            except Exception as exc:  # noqa: BLE001 - the outcome must be one of the two
                outcome = f"{type(exc).__name__}: {exc}"
            with lock:
                results.append(outcome)

        threads = [threading.Thread(target=consume, args=(ledger,)) for ledger in (first, second)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert sorted(results) == ["consumed", "replay"], results
        # exactly one CONSUMED row, and the reservation is the winner's
        assert first.get("ap-race").state.value == "CONSUMED"
        first.close()
        second.close()

    def test_a_verify_failure_never_consumes(self, ledger) -> None:
        payload = _register(ledger, "ap-tamper")
        tampered = ApprovalBindingPayload(**{**payload.__dict__, "base_revision_id": "rev:99"})
        with pytest.raises(ApprovalBindingMismatchError):
            ledger.consume_approval("ap-tamper", identity=_identity(), payload=tampered)
        assert ledger.get("ap-tamper").state.value == "APPROVED"

    def test_an_expired_token_never_consumes(self, tmp_path: Path) -> None:
        late = "2026-11-25T00:11:00Z"
        store = DurableCanonicalApprovalLedger(
            tmp_path / "approvals.sqlite3", key_provider=_ring(), now=lambda: late
        )
        try:
            payload = _register(store, "ap-late")
            with pytest.raises(ApprovalExpiredError):
                store.consume_approval("ap-late", identity=_identity(), payload=payload)
            assert store.get("ap-late").state.value == "APPROVED"
        finally:
            store.close()
