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
* **Writer-serialized across processes.** Head read + hash + insert happen
  inside one ``BEGIN IMMEDIATE`` transaction under a process lock: SQLite's
  write lock serializes every process sharing the sidecar (a deferred
  transaction would let two processes read the same head and race on the
  insert); concurrent job tasks serialize on the in-process lock first.
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
    EventKind,
    LedgerRecord,
    canonical_json,
    compute_record_hash,
    dedupe_key,
)


class CausalConflictError(RuntimeError):
    """A redelivered transition claimed different evidence for one event.

    The (kind, job_id, attempt) key already has a record whose causal claim
    (status/error/payload_digest/result_digest) contradicts the redelivery.
    The journal keeps the FIRST-recorded truth, quarantines the rejected
    claim as a durable ``event_conflict`` observation, and raises — a
    conflicting claim is never silently absorbed as a duplicate.
    """

    def __init__(self, key: str, kept_seq: int, rejected_event: CausalEvent) -> None:
        self.key = key
        self.kept_seq = kept_seq
        self.rejected_event = rejected_event
        super().__init__(
            f"conflicting evidence for already-recorded event {key!r}: "
            f"record seq={kept_seq} stands, redelivered "
            f"{rejected_event.kind.value} (status={rejected_event.status!r}, "
            f"payload_digest={rejected_event.payload_digest!r}, "
            f"result_digest={rejected_event.result_digest!r}) was quarantined"
        )


#: The causal-claim fields compared on redelivery. ``occurred_at`` and
#: ``detail`` are annotations (a labeled reconstruction legitimately differs
#: from the live record it mirrors); they never trigger a conflict.
_CLAIM_FIELDS: tuple[str, ...] = ("status", "error", "payload_digest", "result_digest")


def _claims_conflict(existing: dict, event: CausalEvent) -> bool:
    return any(existing.get(field) != getattr(event, field) for field in _CLAIM_FIELDS)


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

    def __init__(self, db_path: Path | str, *, connect_timeout: float = 30.0) -> None:
        self.db_path = Path(db_path)
        self._sqlite_path = str(db_path)
        #: SQLite busy timeout (seconds): how long a writer waits for the
        #: write lock another process holds before surfacing "database is
        #: locked". The default covers production contention; tests shrink it.
        self._connect_timeout = connect_timeout
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

        Cross-process safety: the whole read-head → derive → insert sequence
        runs inside ONE ``BEGIN IMMEDIATE`` transaction, so SQLite's write
        lock (not just the in-process mutex) serializes concurrent appenders;
        a second process waits (busy timeout), then reads the NEW head. No
        duplicate ``seq`` and no silently dropped event can occur — the only
        remaining failure mode is the busy timeout itself, which surfaces.

        Exactly-once: transition events dedupe on ``(kind, job_id, attempt)``.
        A redelivery with IDENTICAL causal claims (status, error, payload and
        result digests) is an honest retry → ``duplicate=True``. A redelivery
        with CONFLICTING claims keeps the first-recorded record, quarantines
        the rejected claim as a durable ``event_conflict`` observation, and
        raises :class:`CausalConflictError` — different truth is never
        silently absorbed.

        ``backfilled=True`` marks a record reconstructed from the authoritative
        row by recovery — honest evidence about the *fact*, explicitly not a
        claim about having been written at event time.
        """
        key = dedupe_key(event.kind, event.job_id, event.attempt)
        detail = dict(event.detail)
        if backfilled:
            detail["backfilled"] = True
            detail["reconstructed_from"] = "nexus_job_queue"
        last_error: Exception | None = None
        conflict: tuple[str, LedgerRecord, CausalEvent] | None = None
        for _ in range(3):
            if conflict is not None:
                break
            try:
                with self._lock, self._write_connection() as connection:
                    if key is not None:
                        existing = connection.execute(
                            f"SELECT {_RECORD_COLUMNS} FROM nexus_causal_journal"
                            " WHERE dedupe_key = ?",
                            (key,),
                        ).fetchone()
                        if existing is not None:
                            record = self._record(existing)
                            if _claims_conflict(record.payload, event):
                                # Quarantine AFTER this transaction closes: it
                                # needs its own write transaction, and the
                                # caller's lock is not reentrant.
                                conflict = (key, record, event)
                                break
                            return AppendResult(record=record, duplicate=True)
                    head = connection.execute(
                        "SELECT seq, record_hash FROM nexus_causal_journal"
                        " ORDER BY seq DESC LIMIT 1"
                    ).fetchone()
                    seq = int(head[0]) + 1 if head is not None else 1
                    prev_hash = str(head[1]) if head is not None else GENESIS_HASH
                    payload = event.payload(seq=seq, prev_hash=prev_hash)
                    payload["detail"] = detail
                    record_json = canonical_json(payload)
                    record_hash = compute_record_hash(record_json)
                    connection.execute(
                        "INSERT INTO nexus_causal_journal"
                        " (seq, record_hash, prev_hash, kind, job_id, attempt,"
                        "  dedupe_key, record_json, created_at)"
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
                    return AppendResult(
                        record=LedgerRecord(
                            seq=seq,
                            record_hash=record_hash,
                            prev_hash=prev_hash,
                            record_json=record_json,
                            dedupe_key=key,
                            created_at=event.occurred_at,
                        ),
                        duplicate=False,
                    )
            except sqlite3.IntegrityError as exc:
                # Defensive: with BEGIN IMMEDIATE this cannot happen between
                # two correct appenders; if it ever does (a foreign writer
                # bypassing this API), re-read the head and retry instead of
                # dropping the event.
                last_error = exc
                continue
        if conflict is not None:
            conflict_key, kept, rejected = conflict
            self._quarantine_conflict(conflict_key, kept, rejected)
            raise CausalConflictError(conflict_key, kept.seq, rejected)
        raise last_error if last_error is not None else RuntimeError("append failed")

    def _quarantine_conflict(self, key: str, kept: LedgerRecord, rejected: CausalEvent) -> None:
        """Durably record a rejected conflicting claim (best-effort, no dedupe)."""
        try:
            self.append(
                CausalEvent(
                    kind=EventKind.EVENT_CONFLICT,
                    job_id=rejected.job_id,
                    job_type=rejected.job_type,
                    idempotency_key=rejected.idempotency_key,
                    attempt=rejected.attempt,
                    status=rejected.status,
                    error=rejected.error,
                    payload_digest=rejected.payload_digest,
                    result_digest=rejected.result_digest,
                    detail={
                        "rejected_kind": rejected.kind.value,
                        "kept_seq": kept.seq,
                        "kept_record_hash": kept.record_hash,
                        "rejected_claims": {
                            field: getattr(rejected, field) for field in _CLAIM_FIELDS
                        },
                    },
                    occurred_at=rejected.occurred_at,
                )
            )
        except Exception:  # noqa: BLE001 - the raise below carries the conflict
            pass

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
        connection = sqlite3.connect(self._sqlite_path, timeout=self._connect_timeout)
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

    @contextmanager
    def _write_connection(self) -> Iterator[sqlite3.Connection]:
        """A connection holding the database WRITE lock for the whole body.

        ``BEGIN IMMEDIATE`` acquires SQLite's RESERVED lock at transaction
        start — so the head read, the hash derivation and the insert are all
        serialized against every OTHER PROCESS sharing the sidecar, not just
        against threads of this process (a plain deferred transaction would
        let two processes read the same head and race on the insert). The
        in-process lock stays as the cheap first serializer.
        """
        if self._memory_connection is not None:
            # Single-process in-memory journal: the CALLER already holds the
            # thread lock (append acquires it around the whole transaction);
            # SQLite table locks are meaningless inside one shared connection.
            try:
                yield self._memory_connection
            except Exception:
                self._memory_connection.rollback()
                raise
            else:
                self._memory_connection.commit()
            return
        connection = sqlite3.connect(self._sqlite_path, timeout=self._connect_timeout)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
        except Exception:
            connection.rollback()
            raise
        else:
            connection.commit()
        finally:
            connection.close()


__all__ = ["AppendResult", "CausalConflictError", "CausalJournal"]
