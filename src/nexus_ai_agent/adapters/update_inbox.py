"""P2 durable store: update INBOX + RECEIPT with update_id deduplication.

Owns its own SQLite table ``nexus_update_inbox`` (sidecar DDL, no Alembic),
mirroring the job-queue ownership model. The unique primary key on
``update_id`` is the concurrency arbiter: concurrent accepts of the same id
yield exactly one ``ACCEPTED`` and N-1 ``DUPLICATE`` outcomes.

Receipt state machine (domain):

    received  → processing | dead
    processing → processed | dead | received   (received = crash reclaim)
    processed / dead are terminal

Replay of a processed update_id returns the existing receipt and never
creates a second side-effect window.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from nexus_ai_agent.domain.inbox import (
    AcceptOutcome,
    ReceiptStatus,
    ReceiptTransitionError,
    UpdateReceipt,
    assert_transition_allowed,
    format_iso_utc,
    parse_iso_utc,
    utc_now,
    validate_update_id,
)


class UpdateInboxStore:
    """Durable Telegram-style update inbox with receipt state machine."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self._sqlite_path = str(db_path)
        if self._sqlite_path != ":memory:":
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db_lock = threading.Lock()
        self._memory_connection: sqlite3.Connection | None = None
        if self._sqlite_path == ":memory:":
            self._memory_connection = sqlite3.connect(":memory:", check_same_thread=False)
            self._memory_connection.row_factory = sqlite3.Row
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS nexus_update_inbox (
                    update_id INTEGER PRIMARY KEY,
                    status TEXT NOT NULL,
                    receipt_token TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    payload_json TEXT NOT NULL,
                    error TEXT,
                    received_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    processed_at TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_nexus_update_inbox_status
                ON nexus_update_inbox (status, received_at)
                """
            )

    # ---------------------------------------------------------------- accept

    def accept(
        self,
        update_id: int,
        *,
        payload: dict[str, object] | None = None,
        now: datetime | None = None,
    ) -> tuple[AcceptOutcome, UpdateReceipt]:
        """Record an inbound update. First sighting → ACCEPTED; else DUPLICATE.

        The unique primary key is the sole arbiter under concurrency. Payload
        of a duplicate is ignored — the original receipt is returned unchanged.
        """
        uid = validate_update_id(update_id)
        if payload is not None and not isinstance(payload, dict):
            raise TypeError("payload must be a dictionary or None")
        body = dict(payload or {})
        clock = now or utc_now()
        stamp = format_iso_utc(clock)
        token = uuid4().hex
        payload_json = json.dumps(body, ensure_ascii=False, sort_keys=True)

        with self._db_lock, self._immediate() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO nexus_update_inbox
                        (update_id, status, receipt_token, attempts,
                         payload_json, error, received_at, updated_at, processed_at)
                    VALUES (?, ?, ?, 0, ?, NULL, ?, ?, NULL)
                    """,
                    (
                        uid,
                        ReceiptStatus.RECEIVED.value,
                        token,
                        payload_json,
                        stamp,
                        stamp,
                    ),
                )
            except sqlite3.IntegrityError:
                row = connection.execute(
                    "SELECT * FROM nexus_update_inbox WHERE update_id = ?",
                    (uid,),
                ).fetchone()
                if row is None:
                    # Extremely unlikely: conflict then missing row.
                    raise RuntimeError(f"inbox integrity conflict without row for {uid}") from None
                return AcceptOutcome.DUPLICATE, _row_to_receipt(row)

            row = connection.execute(
                "SELECT * FROM nexus_update_inbox WHERE update_id = ?",
                (uid,),
            ).fetchone()
            assert row is not None
            return AcceptOutcome.ACCEPTED, _row_to_receipt(row)

    # ----------------------------------------------------------- transitions

    def begin_processing(
        self,
        update_id: int,
        *,
        receipt_token: str | None = None,
        now: datetime | None = None,
    ) -> UpdateReceipt:
        """``received → processing`` (or reclaim ``received`` after crash)."""
        return self._transition(
            update_id,
            target=ReceiptStatus.PROCESSING,
            receipt_token=receipt_token,
            now=now,
            bump_attempts=True,
        )

    def mark_processed(
        self,
        update_id: int,
        *,
        receipt_token: str | None = None,
        now: datetime | None = None,
    ) -> UpdateReceipt:
        """``processing → processed`` (terminal success)."""
        return self._transition(
            update_id,
            target=ReceiptStatus.PROCESSED,
            receipt_token=receipt_token,
            now=now,
            set_processed_at=True,
        )

    def mark_dead(
        self,
        update_id: int,
        *,
        error: str,
        receipt_token: str | None = None,
        now: datetime | None = None,
    ) -> UpdateReceipt:
        """``received|processing → dead`` (terminal poison)."""
        return self._transition(
            update_id,
            target=ReceiptStatus.DEAD,
            receipt_token=receipt_token,
            now=now,
            error=str(error),
            set_processed_at=True,
        )

    def reclaim_to_received(
        self,
        update_id: int,
        *,
        receipt_token: str | None = None,
        now: datetime | None = None,
    ) -> UpdateReceipt:
        """``processing → received`` for crash-safe retry."""
        return self._transition(
            update_id,
            target=ReceiptStatus.RECEIVED,
            receipt_token=receipt_token,
            now=now,
        )

    def get(self, update_id: int) -> UpdateReceipt | None:
        uid = validate_update_id(update_id)
        with self._db_lock, self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM nexus_update_inbox WHERE update_id = ?",
                (uid,),
            ).fetchone()
            if row is None:
                return None
            return _row_to_receipt(row)

    def list_by_status(self, status: ReceiptStatus, *, limit: int = 100) -> list[UpdateReceipt]:
        if limit < 1:
            raise ValueError("limit must be >= 1")
        with self._db_lock, self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM nexus_update_inbox
                WHERE status = ?
                ORDER BY received_at, update_id
                LIMIT ?
                """,
                (status.value, int(limit)),
            ).fetchall()
        return [_row_to_receipt(row) for row in rows]

    # -------------------------------------------------------------- core

    def _transition(
        self,
        update_id: int,
        *,
        target: ReceiptStatus,
        receipt_token: str | None,
        now: datetime | None,
        error: str | None = None,
        bump_attempts: bool = False,
        set_processed_at: bool = False,
    ) -> UpdateReceipt:
        uid = validate_update_id(update_id)
        clock = now or utc_now()
        stamp = format_iso_utc(clock)

        with self._db_lock, self._immediate() as connection:
            row = connection.execute(
                "SELECT * FROM nexus_update_inbox WHERE update_id = ?",
                (uid,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown update_id: {uid}")

            current = ReceiptStatus(str(row["status"]))
            assert_transition_allowed(current, target)

            if receipt_token is not None and str(row["receipt_token"]) != receipt_token:
                raise PermissionError(
                    f"receipt_token mismatch for update_id={uid}: "
                    "caller does not own this receipt"
                )

            attempts = int(row["attempts"] or 0)
            if bump_attempts:
                attempts += 1

            processed_at = row["processed_at"]
            if set_processed_at:
                processed_at = stamp

            error_value = row["error"] if error is None else error

            connection.execute(
                """
                UPDATE nexus_update_inbox
                SET status = ?,
                    attempts = ?,
                    error = ?,
                    updated_at = ?,
                    processed_at = ?
                WHERE update_id = ?
                  AND status = ?
                """,
                (
                    target.value,
                    attempts,
                    error_value,
                    stamp,
                    processed_at,
                    uid,
                    current.value,
                ),
            )
            # Re-check: concurrent transition may have won.
            updated = connection.execute(
                "SELECT * FROM nexus_update_inbox WHERE update_id = ?",
                (uid,),
            ).fetchone()
            assert updated is not None
            if str(updated["status"]) != target.value:
                # Lost the CAS — surface as illegal/race via domain error.
                raise ReceiptTransitionError(
                    current=ReceiptStatus(str(updated["status"])),
                    target=target,
                )
            return _row_to_receipt(updated)

    @contextmanager
    def _immediate(self) -> Iterator[sqlite3.Connection]:
        with self._connection(immediate=True) as connection:
            yield connection

    @contextmanager
    def _connection(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        if self._memory_connection is not None:
            try:
                if immediate:
                    self._memory_connection.execute("BEGIN IMMEDIATE")
                yield self._memory_connection
            except Exception:
                self._memory_connection.rollback()
                raise
            else:
                self._memory_connection.commit()
            return

        connection = sqlite3.connect(self._sqlite_path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            if immediate:
                connection.execute("BEGIN IMMEDIATE")
            yield connection
        except Exception:
            connection.rollback()
            raise
        else:
            connection.commit()
        finally:
            connection.close()


def _row_to_receipt(row: sqlite3.Row) -> UpdateReceipt:
    payload_raw = json.loads(str(row["payload_json"]))
    if not isinstance(payload_raw, dict):
        raise RuntimeError(f"invalid inbox payload for update_id={row['update_id']}")
    received = parse_iso_utc(str(row["received_at"]))
    updated = parse_iso_utc(str(row["updated_at"]))
    processed = parse_iso_utc(
        None if row["processed_at"] is None else str(row["processed_at"])
    )
    if received is None or updated is None:
        raise RuntimeError(f"invalid inbox timestamps for update_id={row['update_id']}")
    return UpdateReceipt(
        update_id=int(row["update_id"]),
        status=ReceiptStatus(str(row["status"])),
        receipt_token=str(row["receipt_token"]),
        attempts=int(row["attempts"] or 0),
        payload={str(k): v for k, v in payload_raw.items()},
        error=None if row["error"] is None else str(row["error"]),
        received_at=received,
        updated_at=updated,
        processed_at=processed,
    )


__all__ = ["UpdateInboxStore"]
