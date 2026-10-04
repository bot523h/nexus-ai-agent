"""The durable, append-only, hash-chained causal journal.

What this is
------------
A *witness* of committed facts, not an authority.  The queue's own row stays
the source of truth for job state; the artifact's bytes stay the source of
truth for artifact content; this journal is the durable, ordered, tamper-
evident record of *what was observed to happen, in which causal order*, so
that "exactly why and how did this artifact come into existence?" can be
answered deterministically and cross-checked against those authorities
(``causal.passport``).

Laws
----
* **append-only.**  There is no update and no delete statement in this
  module.  The only write is an ``INSERT`` of the next sequence number.
* **chained.**  Record *n* carries the hash of record *n-1*; a first record
  points at :data:`~nexus_ai_agent.causal.models.GENESIS_HASH`.  Sequence
  continuity, hash recomputation and linkage are all checked by
  :meth:`CausalJournal.verify_chain`.
* **fail-closed.**  An append never lands on top of a chain that does not
  verify: a corrupted tail raises :class:`JournalCorrupted` instead of
  silently extending unhistory.  Nothing here repairs, rewrites or
  re-derives a record.
* **idempotent.**  The logical identity of an observation
  (``record_key``, timestamps excluded) is ``UNIQUE``: replaying the same
  committed fact after a lost acknowledgement returns the existing record
  with ``created=False`` — it can never mint a second one.
* **bounded and private.**  Only whitelisted, JSON-exact facts are stored
  (``causal.models.sanitize_facts``); unknown keys, ``NaN``/``Infinity`` and
  oversized payloads are refused at the boundary.

Honest limitation (documented, not hidden): the chain has no external
secret, so a party able to rewrite the *entire* journal consistently can
re-forge it.  Detection of that class of tampering comes from the
cross-authority agreement the passport enforces — the journal's artifact
identity must also appear in the queue's authoritative verification block
*and* match the artifact bytes re-measured on disk.  Rewriting the journal
alone therefore cannot produce a passport that verifies.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Final

from nexus_ai_agent.causal.models import (
    GENESIS_HASH,
    ActorRef,
    AppendOutcome,
    AuthorityRef,
    CausalRecord,
    CausalRefused,
    ChainVerification,
    JournalCorrupted,
    JournalUnavailable,
    NodeRef,
    Stage,
    canonical_json,
    digest_of,
    record_key_for,
    sanitize_facts,
)

#: The journal's own table.  It creates nothing else and touches nothing else
#: (pinned by ``tests/architecture/test_causal_boundary.py``): a witness that
#: could rewrite other tables would be a second authority.
TABLE_NAME: Final[str] = "causal_journal"

_SCHEMA: Final[tuple[str, ...]] = (
    f"""
    CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
        seq INTEGER PRIMARY KEY,
        record_json TEXT NOT NULL,
        subject_node_id TEXT NOT NULL,
        subject_stage TEXT NOT NULL,
        record_key TEXT NOT NULL UNIQUE,
        prev_hash TEXT NOT NULL,
        record_hash TEXT NOT NULL,
        recorded_at TEXT NOT NULL
    )
    """,
    f"CREATE INDEX IF NOT EXISTS {TABLE_NAME}_subject ON {TABLE_NAME} (subject_node_id)",
    f"CREATE INDEX IF NOT EXISTS {TABLE_NAME}_prev ON {TABLE_NAME} (prev_hash)",
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class CausalJournal:
    """Append-only SQLite journal of causal records."""

    def __init__(
        self,
        path: Path | str,
        *,
        now: Callable[[], str] | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.path = Path(path) if str(path) != ":memory:" else None
        self._sqlite_path = str(path)
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._now = now if now is not None else _utc_now_iso
        self._timeout = timeout_seconds
        self._lock = threading.Lock()
        self._memory_connection: sqlite3.Connection | None = None
        if self._sqlite_path == ":memory:":
            self._memory_connection = sqlite3.connect(":memory:", check_same_thread=False)
            self._memory_connection.row_factory = sqlite3.Row
        self._initialize()

    # ------------------------------------------------------------------
    # Storage plumbing
    # ------------------------------------------------------------------
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
        connection = sqlite3.connect(self._sqlite_path, timeout=self._timeout)
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

    def _initialize(self) -> None:
        try:
            with self._lock, self._connection() as connection:
                for statement in _SCHEMA:
                    connection.execute(statement)
        except sqlite3.Error as exc:  # pragma: no cover - environment defect
            raise JournalUnavailable(f"cannot initialize the causal journal: {exc}") from exc

    # ------------------------------------------------------------------
    # Append
    # ------------------------------------------------------------------
    def append(
        self,
        *,
        stage: Stage,
        subject: NodeRef,
        facts: Mapping[str, object],
        parents: Sequence[NodeRef] = (),
        actor: ActorRef | None = None,
        authority: AuthorityRef | None = None,
    ) -> AppendOutcome:
        """Append one observation, or collapse it onto the record it repeats.

        The write happens in a single transaction together with the tail
        check, so two concurrent appenders cannot fork the chain: the second
        one either sees the other's tail (and links after it) or loses the
        ``PRIMARY KEY`` race and retries against the new tail.
        """
        sanitized = sanitize_facts(stage, facts)
        key = record_key_for(
            stage=stage,
            subject=subject,
            parents=parents,
            actor=actor,
            authority=authority,
            facts=sanitized,
        )
        with self._lock:
            for attempt in range(3):
                try:
                    return self._append_once(
                        stage=stage,
                        subject=subject,
                        parents=tuple(parents),
                        actor=actor,
                        authority=authority,
                        facts=sanitized,
                        record_key=key,
                    )
                except sqlite3.IntegrityError:
                    # Two appenders raced for the same seq or the same
                    # record_key.  Re-read: either the record now exists
                    # (duplicate observation) or the tail moved on.
                    existing = self._find_by_key(key)
                    if existing is not None:
                        return AppendOutcome(
                            record=existing, created=False, duplicate_of_seq=existing.seq
                        )
                    if attempt == 2:  # pragma: no cover - extreme contention
                        raise
                except sqlite3.Error as exc:  # pragma: no cover - environment defect
                    raise JournalUnavailable(f"journal append failed: {exc}") from exc
        raise JournalUnavailable("journal append could not be serialized")  # pragma: no cover

    def _append_once(
        self,
        *,
        stage: Stage,
        subject: NodeRef,
        parents: tuple[NodeRef, ...],
        actor: ActorRef | None,
        authority: AuthorityRef | None,
        facts: dict[str, object],
        record_key: str,
    ) -> AppendOutcome:
        with self._connection() as connection:
            existing_row = connection.execute(
                f"SELECT record_json FROM {TABLE_NAME} WHERE record_key = ?", (record_key,)
            ).fetchone()
            if existing_row is not None:
                existing = _parse_record(str(existing_row["record_json"]))
                return AppendOutcome(record=existing, created=False, duplicate_of_seq=existing.seq)

            tail_row = connection.execute(
                f"SELECT seq, record_json, record_hash FROM {TABLE_NAME} ORDER BY seq DESC LIMIT 1"
            ).fetchone()
            if tail_row is None:
                seq = 1
                prev_hash = GENESIS_HASH
            else:
                tail = _parse_record(str(tail_row["record_json"]))
                if tail.seq != int(tail_row["seq"]) or not tail.digest_matches():
                    raise JournalCorrupted(
                        f"journal tail (seq={tail_row['seq']}) does not verify; refusing to append"
                    )
                if tail.record_hash != str(tail_row["record_hash"]):
                    raise JournalCorrupted(
                        "journal tail hash column disagrees with its record; refusing to append"
                    )
                seq = tail.seq + 1
                prev_hash = tail.record_hash

            facts_digest = digest_of(facts)
            recorded_at = str(self._now())
            draft = CausalRecord(
                seq=seq,
                stage=stage,
                subject=subject,
                parents=parents,
                actor=actor,
                authority=authority,
                facts=facts,
                facts_digest=facts_digest,
                record_key=record_key,
                recorded_at=recorded_at,
                prev_hash=prev_hash,
                record_hash="0" * 64,
            )
            record = draft.model_copy(update={"record_hash": draft.compute_hash()})
            connection.execute(
                f"""
                INSERT INTO {TABLE_NAME}
                    (seq, record_json, subject_node_id, subject_stage, record_key,
                     prev_hash, record_hash, recorded_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.seq,
                    canonical_json(record.model_dump(mode="json")),
                    subject.node_id,
                    subject.stage.value,
                    record.record_key,
                    record.prev_hash,
                    record.record_hash,
                    record.recorded_at,
                ),
            )
        return AppendOutcome(record=record, created=True)

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------
    def head_hash(self) -> str:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                f"SELECT record_hash FROM {TABLE_NAME} ORDER BY seq DESC LIMIT 1"
            ).fetchone()
        return str(row["record_hash"]) if row is not None else GENESIS_HASH

    def count(self) -> int:
        with self._lock, self._connection() as connection:
            row = connection.execute(f"SELECT COUNT(*) AS n FROM {TABLE_NAME}").fetchone()
        return int(row["n"])

    def records(self) -> list[CausalRecord]:
        """Every record, in causal order (the only order this journal has)."""
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                f"SELECT record_json FROM {TABLE_NAME} ORDER BY seq"
            ).fetchall()
        return [_parse_record(str(row["record_json"])) for row in rows]

    def records_for(self, node_id: str) -> list[CausalRecord]:
        """Records whose subject is ``node_id``, in causal order."""
        if not node_id:
            raise CausalRefused("records_for needs a node id")
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                f"SELECT record_json FROM {TABLE_NAME} WHERE subject_node_id = ? ORDER BY seq",
                (node_id,),
            ).fetchall()
        return [_parse_record(str(row["record_json"])) for row in rows]

    def reaching(self, node_id: str) -> list[CausalRecord]:
        """Records that mention ``node_id`` as subject or as a parent.

        The lineage walk ("which records explain this artifact?") is defined
        on this relation: an artifact is explained by the records that record
        it and by the records that name it as an input of something else.
        """
        if not node_id:
            raise CausalRefused("reaching needs a node id")
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                f"SELECT record_json FROM {TABLE_NAME} ORDER BY seq"
            ).fetchall()
        hits: list[CausalRecord] = []
        for row in rows:
            record = _parse_record(str(row["record_json"]))
            if record.subject.node_id == node_id or any(
                parent.node_id == node_id for parent in record.parents
            ):
                hits.append(record)
        return hits

    # ------------------------------------------------------------------
    # Verify
    # ------------------------------------------------------------------
    def verify_chain(
        self,
        *,
        expected_head: str | None = None,
        expected_count: int | None = None,
    ) -> ChainVerification:
        """Walk the whole chain; refuse (do not repair) anything that fails.

        Detects: in-place edits (hash recomputation), interior deletion or
        reordering (sequence continuity + linkage), truncation when an
        external anchor is supplied (``expected_head``/``expected_count``),
        and column/JSON disagreement.
        """
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                f"SELECT seq, record_json, record_hash, prev_hash, subject_node_id, "
                f"subject_stage, record_key, recorded_at FROM {TABLE_NAME} ORDER BY seq"
            ).fetchall()
        head = GENESIS_HASH
        expected_seq = 1
        for row in rows:
            try:
                record = _parse_record(str(row["record_json"]))
            except JournalCorrupted as exc:
                return ChainVerification(
                    ok=False,
                    record_count=len(rows),
                    head_hash=head,
                    first_bad_seq=int(row["seq"]),
                    reason=f"unreadable record: {exc}",
                )
            if record.seq != expected_seq or int(row["seq"]) != expected_seq:
                return ChainVerification(
                    ok=False,
                    record_count=len(rows),
                    head_hash=head,
                    first_bad_seq=expected_seq,
                    reason="sequence gap (a record was removed or reordered)",
                )
            if record.prev_hash != head:
                return ChainVerification(
                    ok=False,
                    record_count=len(rows),
                    head_hash=head,
                    first_bad_seq=record.seq,
                    reason="broken link to the previous record",
                )
            if record.record_hash != str(row["record_hash"]) or record.prev_hash != str(
                row["prev_hash"]
            ):
                return ChainVerification(
                    ok=False,
                    record_count=len(rows),
                    head_hash=head,
                    first_bad_seq=record.seq,
                    reason="indexed columns disagree with the record body",
                )
            if not record.digest_matches():
                return ChainVerification(
                    ok=False,
                    record_count=len(rows),
                    head_hash=head,
                    first_bad_seq=record.seq,
                    reason="record hash was recomputed and does not match its content",
                )
            if (
                str(row["prev_hash"]) != record.prev_hash
                or int(row["seq"]) != record.seq
                or str(row["subject_node_id"]) != record.subject.node_id
                or str(row["subject_stage"]) != record.subject.stage.value
                or str(row["record_key"]) != record.record_key
                or str(row["recorded_at"]) != record.recorded_at
            ):
                return ChainVerification(
                    ok=False,
                    record_count=len(rows),
                    head_hash=head,
                    first_bad_seq=record.seq,
                    reason="indexed columns disagree with the record body",
                )
            if digest_of(record.facts) != record.facts_digest:
                return ChainVerification(
                    ok=False,
                    record_count=len(rows),
                    head_hash=head,
                    first_bad_seq=record.seq,
                    reason="facts digest does not match the stored facts",
                )
            head = record.record_hash
            expected_seq += 1
        if expected_count is not None and expected_count != len(rows):
            return ChainVerification(
                ok=False,
                record_count=len(rows),
                head_hash=head,
                reason=f"record count {len(rows)} != anchored {expected_count}",
            )
        if expected_head is not None and expected_head != head:
            return ChainVerification(
                ok=False,
                record_count=len(rows),
                head_hash=head,
                reason="head hash does not match the external anchor",
            )
        return ChainVerification(ok=True, record_count=len(rows), head_hash=head)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------
    def _find_by_key(self, record_key: str) -> CausalRecord | None:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                f"SELECT record_json FROM {TABLE_NAME} WHERE record_key = ?", (record_key,)
            ).fetchone()
        return _parse_record(str(row["record_json"])) if row is not None else None


def _parse_record(raw: str) -> CausalRecord:
    try:
        return CausalRecord.model_validate_json(raw)
    except Exception as exc:  # noqa: BLE001 - any parse failure is corruption
        raise JournalCorrupted(f"stored record is not a valid causal record: {exc}") from exc


__all__ = ["TABLE_NAME", "CausalJournal"]
