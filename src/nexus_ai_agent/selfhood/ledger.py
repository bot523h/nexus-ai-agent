"""Self-Ledger: append-only, hash-chained SQLite store of the system's self-state.

Same hash-chain discipline as the causal journal (domain-separated hash over the
exact stored bytes) under its own domain and sidecar. There is no mutation API;
tampering is *detected* by :meth:`SelfLedger.verify`. A bare chain cannot see
loss of the newest records, so ``verify(expected_head=...)`` accepts an
externally held head hash — the honest limit, stated rather than hidden.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from nexus_ai_agent.provenance.models import GENESIS_HASH, canonical_json, sha256_hex

RECORD_DOMAIN = "nexus.self-ledger.record.v1"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS nexus_self_ledger (
    seq INTEGER PRIMARY KEY,
    record_hash TEXT NOT NULL UNIQUE,
    prev_hash TEXT NOT NULL,
    record_json TEXT NOT NULL,
    created_at TEXT NOT NULL
)
"""


class SelfEntryKind(str, Enum):
    CHARTER_ADOPTED = "charter_adopted"
    COMMITMENT_OPENED = "commitment_opened"
    COMMITMENT_CLOSED = "commitment_closed"
    OBSERVATION = "observation"
    VITAL_READING = "vital_reading"
    VITAL_DEVIATION = "vital_deviation"


def compute_record_hash(record_json: str) -> str:
    return sha256_hex(RECORD_DOMAIN.encode("utf-8") + b"\n" + record_json.encode("utf-8"))


@dataclass(frozen=True)
class PendingEntry:
    kind: SelfEntryKind
    subject: str
    detail: dict[str, Any]
    occurred_at: str | None = None


@dataclass(frozen=True)
class SelfRecord:
    seq: int
    record_hash: str
    prev_hash: str
    record_json: str
    created_at: str

    @property
    def payload(self) -> dict[str, Any]:
        parsed = json.loads(self.record_json)
        if not isinstance(parsed, dict):
            raise ValueError(f"record {self.seq}: not a JSON object")
        return parsed

    @property
    def kind(self) -> SelfEntryKind:
        return SelfEntryKind(str(self.payload["kind"]))


@dataclass(frozen=True)
class SelfVerdict:
    ok: bool
    record_count: int
    head_hash: str | None
    first_broken_seq: int | None = None
    reason: str | None = None


@dataclass(frozen=True)
class Commitment:
    commitment_id: str
    text: str
    opened_seq: int


def _bad(count: int, seq: int, reason: str) -> SelfVerdict:
    return SelfVerdict(False, count, None, seq, reason)


def _opened(records: list[SelfRecord]) -> list[Commitment]:
    return [
        Commitment(r.payload["subject"], str(r.payload["detail"].get("text", "")), r.seq)
        for r in records
        if r.kind is SelfEntryKind.COMMITMENT_OPENED
    ]


def _open(records: list[SelfRecord]) -> list[Commitment]:
    closed = {r.payload["subject"] for r in records if r.kind is SelfEntryKind.COMMITMENT_CLOSED}
    return [c for c in _opened(records) if c.commitment_id not in closed]


class SelfLedger:
    """Durable single-writer, append-only, hash-chained self-state store."""

    def __init__(self, db_path: Path | str, *, connect_timeout: float = 30.0) -> None:
        self.db_path = Path(db_path)
        self._timeout = connect_timeout
        self._lock = threading.Lock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(_SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path, timeout=self._timeout, isolation_level=None)
        try:
            yield connection
        finally:
            connection.close()

    # -- writes -----------------------------------------------------------

    def append(
        self,
        kind: SelfEntryKind,
        *,
        subject: str,
        detail: dict[str, Any] | None = None,
        occurred_at: str | None = None,
        precondition: Callable[[list[SelfRecord]], None] | None = None,
    ) -> SelfRecord:
        """Append one record.

        ``precondition`` runs INSIDE the write transaction on the records read
        under the write lock, so a check-then-append (e.g. "this commitment is
        not open yet") cannot race another thread or process.
        """
        entry = PendingEntry(kind, subject, detail or {}, occurred_at)
        return self.append_many([entry], precondition=precondition)[0]

    def append_many(
        self,
        entries: Sequence[PendingEntry],
        *,
        precondition: Callable[[list[SelfRecord]], None] | None = None,
    ) -> list[SelfRecord]:
        """Append several records in ONE transaction: all of them or none."""
        for entry in entries:
            if not entry.subject:
                raise ValueError("subject must not be empty")
        written: list[SelfRecord] = []
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                if precondition is not None:
                    precondition(self._read(connection))
                head = connection.execute(
                    "SELECT seq, record_hash FROM nexus_self_ledger ORDER BY seq DESC LIMIT 1"
                ).fetchone()
                seq = int(head[0]) if head else 0
                prev = str(head[1]) if head else GENESIS_HASH
                for entry in entries:
                    seq += 1
                    stamp = entry.occurred_at or datetime.now(timezone.utc).isoformat()
                    record_json = canonical_json(
                        {
                            "seq": seq,
                            "prev_hash": prev,
                            "kind": entry.kind.value,
                            "subject": entry.subject,
                            "detail": entry.detail,
                            "occurred_at": stamp,
                        }
                    )
                    record_hash = compute_record_hash(record_json)
                    connection.execute(
                        "INSERT INTO nexus_self_ledger (seq, record_hash, prev_hash, record_json,"
                        " created_at) VALUES (?, ?, ?, ?, ?)",
                        (seq, record_hash, prev, record_json, stamp),
                    )
                    written.append(SelfRecord(seq, record_hash, prev, record_json, stamp))
                    prev = record_hash
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
        return written

    def open_commitment(self, commitment_id: str, text: str) -> SelfRecord:
        def _not_yet_opened(records: list[SelfRecord]) -> None:
            if any(c.commitment_id == commitment_id for c in _opened(records)):
                raise ValueError(f"commitment {commitment_id!r} already exists")

        return self.append(
            SelfEntryKind.COMMITMENT_OPENED,
            subject=commitment_id,
            detail={"text": text},
            precondition=_not_yet_opened,
        )

    def close_commitment(self, commitment_id: str, *, outcome: str) -> SelfRecord:
        def _is_open(records: list[SelfRecord]) -> None:
            if commitment_id not in {c.commitment_id for c in _open(records)}:
                raise ValueError(f"commitment {commitment_id!r} is not open")

        return self.append(
            SelfEntryKind.COMMITMENT_CLOSED,
            subject=commitment_id,
            detail={"outcome": outcome},
            precondition=_is_open,
        )

    # -- reads ------------------------------------------------------------

    @staticmethod
    def _read(connection: sqlite3.Connection) -> list[SelfRecord]:
        rows = connection.execute(
            "SELECT seq, record_hash, prev_hash, record_json, created_at"
            " FROM nexus_self_ledger ORDER BY seq"
        ).fetchall()
        return [SelfRecord(*row) for row in rows]

    def records(self) -> list[SelfRecord]:
        with self._connect() as connection:
            return self._read(connection)

    def open_commitments(self) -> list[Commitment]:
        return _open(self.records())

    def verify(self, *, expected_head: str | None = None) -> SelfVerdict:
        """Recompute the whole chain from genesis; pinpoint the first break."""
        return self.verify_records(self.records(), expected_head=expected_head)

    @staticmethod
    def verify_records(
        records: list[SelfRecord], *, expected_head: str | None = None
    ) -> SelfVerdict:
        """Verify an already-read snapshot, so callers can verify and use the *same* rows."""
        previous = GENESIS_HASH
        for index, record in enumerate(records, start=1):
            if record.seq != index:
                return _bad(len(records), record.seq, f"sequence gap: expected {index}")
            if record.prev_hash != previous:
                return _bad(len(records), record.seq, "prev_hash does not link (deleted/spliced)")
            if compute_record_hash(record.record_json) != record.record_hash:
                return _bad(len(records), record.seq, "stored bytes do not match record_hash")
            try:
                payload = record.payload
                consistent = (
                    payload.get("seq") == record.seq
                    and payload.get("prev_hash") == record.prev_hash
                    and payload.get("occurred_at") == record.created_at
                )
            except ValueError:
                consistent = False
            if not consistent:
                return _bad(len(records), record.seq, "record_json disagrees with its columns")
            previous = record.record_hash
        head = records[-1].record_hash if records else None
        if expected_head is not None and head != expected_head:
            return SelfVerdict(
                False, len(records), head, None, "head differs from the externally anchored head"
            )
        return SelfVerdict(True, len(records), head)


__all__ = [
    "RECORD_DOMAIN",
    "Commitment",
    "SelfEntryKind",
    "SelfLedger",
    "SelfRecord",
    "SelfVerdict",
    "compute_record_hash",
]
