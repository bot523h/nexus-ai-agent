"""The durable causal journal — an append-only, hash-chained SQLite store.

Properties (each enforced here, tested in ``tests/unit/test_provenance_journal.py``):

* **Append-only.** The only write is ``append``; there is no update or delete
  API. Tampering with the file is out of process scope and is *detected* by
  :func:`nexus_ai_agent.provenance.models.verify_chain`.
* **Hash-chained from genesis.** Every record commits to ``prev_hash`` and to
  its exact canonical bytes; deletion, splicing, reordering or editing any
  record breaks verification with the exact first broken ``seq``.
* **Exactly-once per logical event.** Transition events dedupe on
  ``(kind, job_id, attempt)`` under a UNIQUE key: a retry after a lost
  acknowledgement re-appends nothing. Observations (duplicate enqueues) are
  appended every time — their count IS the evidence.
* **Single-writer serialized.** Head read + hash + insert happen inside one
  transaction under a process lock; concurrent appenders serialize (the
  queue's observer calls arrive from concurrent job tasks).
* **Honest gap model.** The journal does not share the queue's transaction:
  a crash between the queue's durable commit and the journal commit leaves a
  detectable hole, which the passport reports (never invents) and the
  explicit backfill (``provenance.backfill``) reconstructs *labeled*.

Storage location: its own sidecar (``<queue-db>-causal.sqlite``) — the ledger
must never be a schema guest inside the authority it observes.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from nexus_ai_agent.provenance.models import (
    GENESIS_HASH,
    CausalEvent,
    LedgerRecord,
    canonical_json,
    compute_record_hash,
    dedupe_key,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS nexus_causal_journal (
    seq INTEGER PRIMARY KEY,
    record_hash TEXT NOT NULL UNIQUE,
    prev_hash TEXT NOT NULL,
    kind TEXT NOT NULL,
    job_id TEXT NOT NULL,
    attempt INTEGER,
    dedupe_key TEXT UNIQUE,
    record_json TEXT NOT NULL,
    created_at TEXT NOT NULL
)
"""

_INDEX = """
CREATE INDEX IF NOT EXISTS idx_causal_journal_job ON nexus_causal_journal (job_id, seq)
"""

_RECORD_COLUMNS = "seq, record_hash, prev_hash, record_json, dedupe_key, created_at"


@dataclass(frozen=True)
class AppendResult:
    """Outcome of one ``append`` call (idempotency made visible)."""

    record: LedgerRecord | None
    duplicate: bool


class CausalJournal:
    """Durable, single-writer, hash-chained causal journal (SQLite sidecar)."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self._sqlite_path = str(db_path)
        self._lock = threading.Lock()
        self._memory_connection: sqlite3.Connection | None = None
        if self._sqlite_path == ":memory:":
            self._memory_connection = sqlite3.connect(":memory:", check_same_thread=False)
            self._memory_connection.row_factory = sqlite3.Row
        else:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute(_SCHEMA)
            connection.execute(_INDEX)

    # -- writes -----------------------------------------------------------

    def append(self, event: CausalEvent, *, backfilled: bool = False) -> AppendResult:
        """Append one event exactly once (transition events), chained.

        ``backfilled=True`` marks a record reconstructed from the authoritative
        row by recovery — it is honest evidence about the *fact*, explicitly
        not a claim about having been written at event time.
        """
        key = dedupe_key(event.kind, event.job_id, event.attempt)
        detail = dict(event.detail)
        if backfilled:
            detail["backfilled"] = True
            detail["reconstructed_from"] = "nexus_job_queue"
        with self._lock, self._connection() as connection:
            if key is not None:
                existing = connection.execute(
                    f"SELECT {_RECORD_COLUMNS} FROM nexus_causal_journal WHERE dedupe_key = ?",
                    (key,),
                ).fetchone()
                if existing is not None:
                    return AppendResult(record=self._record(existing), duplicate=True)
            head = connection.execute(
                "SELECT seq, record_hash FROM nexus_causal_journal ORDER BY seq DESC LIMIT 1"
            ).fetchone()
            seq = int(head[0]) + 1 if head is not None else 1
            prev_hash = str(head[1]) if head is not None else GENESIS_HASH
            payload = event.payload(seq=seq, prev_hash=prev_hash)
            payload["detail"] = detail
            record_json = canonical_json(payload)
            record_hash = compute_record_hash(record_json)
            connection.execute(
                "INSERT INTO nexus_causal_journal"
                " (seq, record_hash, prev_hash, kind, job_id, attempt, dedupe_key,"
                "  record_json, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    seq,
                    record_hash,
                    prev_hash,
                    event.kind.value,
                    event.job_id,
                    event.attempt,
                    key,
                    record_json,
                    event.occurred_at,
                ),
            )
            record = LedgerRecord(
                seq=seq,
                record_hash=record_hash,
                prev_hash=prev_hash,
                record_json=record_json,
                dedupe_key=key,
                created_at=event.occurred_at,
            )
            return AppendResult(record=record, duplicate=False)

    # -- reads ------------------------------------------------------------

    def records_for_job(self, job_id: str) -> list[LedgerRecord]:
        """All records of one job, chain order."""
        with self._connection() as connection:
            rows = connection.execute(
                f"SELECT {_RECORD_COLUMNS} FROM nexus_causal_journal WHERE job_id = ? ORDER BY seq",
                (job_id,),
            ).fetchall()
        return [self._record(row) for row in rows]

    def all_records(self) -> list[LedgerRecord]:
        """The whole chain, in order (the passport reconciles per job)."""
        with self._connection() as connection:
            rows = connection.execute(
                f"SELECT {_RECORD_COLUMNS} FROM nexus_causal_journal ORDER BY seq"
            ).fetchall()
        return [self._record(row) for row in rows]

    def head(self) -> tuple[int, str] | None:
        """``(seq, record_hash)`` of the chain head — the anchorable fact."""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT seq, record_hash FROM nexus_causal_journal ORDER BY seq DESC LIMIT 1"
            ).fetchone()
        return (int(row[0]), str(row[1])) if row is not None else None

    def count(self) -> int:
        with self._connection() as connection:
            row = connection.execute("SELECT COUNT(*) FROM nexus_causal_journal").fetchone()
        return int(row[0])

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _record(row: sqlite3.Row) -> LedgerRecord:
        key = row["dedupe_key"]
        return LedgerRecord(
            seq=int(row["seq"]),
            record_hash=str(row["record_hash"]),
            prev_hash=str(row["prev_hash"]),
            record_json=str(row["record_json"]),
            dedupe_key=str(key) if key is not None else None,
            created_at=str(row["created_at"]),
        )

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        if self._memory_connection is not None:
            try:
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
            yield connection
        except Exception:
            connection.rollback()
            raise
        else:
            connection.commit()
        finally:
            connection.close()


__all__ = ["AppendResult", "CausalJournal"]
