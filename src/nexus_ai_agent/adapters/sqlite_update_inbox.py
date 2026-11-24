"""SQLite-backed idempotent update inbox.

The persistence layer only ever *applies* the domain DFA; it never invents a
transition the domain module forbids.  Every write runs inside ``BEGIN
IMMEDIATE`` so two workers cannot both claim the same update, and every read
re-verifies the stored receipt's sealed digest before returning it.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from typing import cast

from nexus_ai_agent.domain.idempotent_inbox import (
    ByzantineUpdateConflictError,
    InboxDecision,
    InboxReceipt,
    InboxReceiptTamperError,
    InboxState,
    InvalidInboxTransitionError,
    advance,
)

__all__ = ["SQLiteUpdateInbox"]


class SQLiteUpdateInbox:
    """Durable, concurrency-safe intake with absorbing terminal states."""

    def __init__(self, path: str = ":memory:") -> None:
        if not isinstance(path, str) or not path:
            raise ValueError("path must be a non-empty string")
        self._connection = sqlite3.connect(path, isolation_level=None)
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS update_inbox ("
            "source_scope TEXT NOT NULL, "
            "update_id INTEGER NOT NULL, "
            "payload_digest TEXT NOT NULL, "
            "state TEXT NOT NULL, "
            "receipt_json TEXT NOT NULL, "
            "quarantine_reason TEXT, "
            "PRIMARY KEY (source_scope, update_id))"
        )

    @property
    def connection(self) -> sqlite3.Connection:
        return self._connection

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._connection.execute("ROLLBACK")
            raise
        else:
            self._connection.execute("COMMIT")

    @staticmethod
    def _validate_key(source_scope: str, update_id: int, payload: str) -> str:
        if not isinstance(source_scope, str) or not source_scope:
            raise ValueError("source_scope must be a non-empty string")
        if isinstance(update_id, bool) or not isinstance(update_id, int):
            raise TypeError("update_id must be an int")
        if update_id < 0:
            raise ValueError("update_id must be non-negative")
        if not isinstance(payload, str) or len(payload) != 64:
            raise ValueError("payload_digest must be a 64-character SHA-256 hex string")
        try:
            int(payload, 16)
        except ValueError as exc:
            raise ValueError("payload_digest must be hexadecimal") from exc
        return payload.lower()

    def _select(self, source_scope: str, update_id: int) -> tuple[str, str, str, str | None] | None:
        row = self._connection.execute(
            "SELECT payload_digest, state, receipt_json, quarantine_reason "
            "FROM update_inbox WHERE source_scope = ? AND update_id = ?",
            (source_scope, update_id),
        ).fetchone()
        if row is None:
            return None
        return cast(tuple[str, str, str, str | None], row)

    def _load(
        self, row: tuple[str, str, str, str | None], source_scope: str, update_id: int
    ) -> InboxReceipt:
        payload_digest, raw_state, receipt_json, _reason = row
        try:
            state = InboxState(raw_state)
        except ValueError as exc:
            raise InboxReceiptTamperError(f"unknown stored inbox state {raw_state!r}") from exc
        receipt = InboxReceipt.from_json(receipt_json)
        if receipt.state is not state:
            raise InboxReceiptTamperError("stored state column disagrees with the sealed receipt")
        if receipt.source_scope != source_scope or receipt.update_id != update_id:
            raise InboxReceiptTamperError("stored receipt identity does not match its key")
        if receipt.payload_digest != payload_digest:
            raise InboxReceiptTamperError("stored payload digest disagrees with the receipt")
        return receipt

    def _insert(
        self,
        receipt: InboxReceipt,
        *,
        reason: str | None = None,
    ) -> None:
        self._connection.execute(
            "INSERT INTO update_inbox "
            "(source_scope, update_id, payload_digest, state, receipt_json, quarantine_reason) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                receipt.source_scope,
                receipt.update_id,
                receipt.payload_digest,
                receipt.state.value,
                receipt.to_json(),
                reason,
            ),
        )

    def _update(self, receipt: InboxReceipt, *, reason: str | None = None) -> None:
        self._connection.execute(
            "UPDATE update_inbox SET payload_digest = ?, state = ?, receipt_json = ?, "
            "quarantine_reason = ? WHERE source_scope = ? AND update_id = ?",
            (
                receipt.payload_digest,
                receipt.state.value,
                receipt.to_json(),
                reason,
                receipt.source_scope,
                receipt.update_id,
            ),
        )

    def ingest(self, source_scope: str, update_id: int, payload: str) -> InboxDecision:
        """RECEIVED -> CLAIMED, or replay the existing sealed receipt."""

        digest = self._validate_key(source_scope, update_id, payload)
        with self._transaction():
            row = self._select(source_scope, update_id)
            if row is None:
                state = advance(InboxState.RECEIVED, InboxState.CLAIMED)
                receipt = InboxReceipt.create(source_scope, update_id, digest, state)
                self._insert(receipt)
                return InboxDecision(receipt=receipt, replayed=False, applied=True)
            receipt = self._load(row, source_scope, update_id)
            if receipt.payload_digest != digest:
                raise ByzantineUpdateConflictError(
                    f"update {source_scope}/{update_id} already has a different payload digest"
                )
            if receipt.state is InboxState.RECEIVED:
                state = advance(receipt.state, InboxState.CLAIMED)
                claimed = InboxReceipt.create(source_scope, update_id, digest, state)
                self._update(claimed)
                return InboxDecision(receipt=claimed, replayed=False, applied=True)
            return InboxDecision(receipt=receipt, replayed=True, applied=False)

    def commit(self, source_scope: str, update_id: int) -> InboxDecision:
        """CLAIMED -> COMMITTED; COMMITTED is absorbing and replays its receipt."""

        self._validate_key(source_scope, update_id, "0" * 64)
        with self._transaction():
            row = self._select(source_scope, update_id)
            if row is None:
                raise KeyError((source_scope, update_id))
            receipt = self._load(row, source_scope, update_id)
            if receipt.state is InboxState.COMMITTED:
                return InboxDecision(receipt=receipt, replayed=True, applied=False)
            state = advance(receipt.state, InboxState.COMMITTED)
            committed = InboxReceipt.create(source_scope, update_id, receipt.payload_digest, state)
            self._update(committed)
            return InboxDecision(receipt=committed, replayed=False, applied=True)

    def quarantine(self, source_scope: str, update_id: int, reason: str = "") -> InboxDecision:
        """Move a non-terminal update to QUARANTINED; committed rows reject it."""

        self._validate_key(source_scope, update_id, "0" * 64)
        if not isinstance(reason, str):
            raise TypeError("reason must be a string")
        with self._transaction():
            row = self._select(source_scope, update_id)
            if row is None:
                raise KeyError((source_scope, update_id))
            receipt = self._load(row, source_scope, update_id)
            if receipt.state is InboxState.QUARANTINED:
                return InboxDecision(receipt=receipt, replayed=True, applied=False)
            if receipt.state is InboxState.COMMITTED:
                raise InvalidInboxTransitionError(
                    "COMMITTED is absorbing and cannot be quarantined"
                )
            state = advance(receipt.state, InboxState.QUARANTINED)
            quarantined = InboxReceipt.create(
                source_scope, update_id, receipt.payload_digest, state
            )
            self._update(quarantined, reason=reason)
            return InboxDecision(receipt=quarantined, replayed=False, applied=True)

    def apply(self, source_scope: str, update_id: int, payload: str) -> InboxDecision:
        """Idempotent f(f(x)) = f(x): ingest then commit, replaying terminal rows."""

        decision = self.ingest(source_scope, update_id, payload)
        if decision.receipt.state in (InboxState.COMMITTED, InboxState.QUARANTINED):
            return decision
        return self.commit(source_scope, update_id)

    def receipt(self, source_scope: str, update_id: int) -> InboxReceipt:
        row = self._select(source_scope, update_id)
        if row is None:
            raise KeyError((source_scope, update_id))
        return self._load(row, source_scope, update_id)

    def state_of(self, source_scope: str, update_id: int) -> InboxState:
        return self.receipt(source_scope, update_id).state

    def __len__(self) -> int:
        row = self._connection.execute("SELECT COUNT(*) FROM update_inbox").fetchone()
        if row is None:  # pragma: no cover - COUNT always returns a row
            return 0
        return cast(int, row[0])

    def close(self) -> None:
        self._connection.close()
