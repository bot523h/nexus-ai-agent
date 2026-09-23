"""Reference outbox dispatcher (P3/P4): claim → deliver → record, database-first.

This is the *reference implementation* of the dispatcher contract. It is
deliberately boring and database-first: one SQLite sidecar owns a single
``nexus_effect_outbox`` table, nothing else. There is no Redis, no broker, no
push notification — a "please perform this external effect" is first a durable
row, then an at-least-once delivery attempt.

Guarantee ladder (never "exactly-once delivery"):

* **atomic intent** — ``append_or_get`` is one statement against the
  UNIQUE(operation_type, effect_key) key, so re-submitting the same logical
  intent returns the stored outcome instead of minting a second row (P4 dedupe
  at the source).
* **at-least-once delivery** — a crash after commit and before dispatch leaves
  the row PENDING; a crash after the effect and before the SUCCEEDED write
  leaves a stale CLAIM that is later re-claimed; re-claims *see the same effect
  key*.
* **at-most-once logical effect** — per (operation_type, effect_key) there is
  at most one live claim and one terminal record; downstream idempotency is the
  domain-appropriate extra step, documented per effect class.
* **explicit ambiguity** — an unknown remote result is never laundered into
  SUCCEEDED (crash window C4); the row stays recoverable and visible.

All monotonic counters are epochs tied to a single logical definition used by
``domain/policies/outbox_policy.py``; the dispatcher does not read a wall clock
for verdicts — dispatch/reconcile inject ``now`` so the crash matrix is
deterministic and sleep-free in tests.

The store records *outcomes* (status, attempt, last_error, outcome), never
payloads or secrets in log lines; ``last_error`` is truncated at 300 chars so
a verbose provider response cannot be used as a side channel.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Final

from nexus_ai_agent.application.ports.outbox_port import DeliveryError, EffectAdapter
from nexus_ai_agent.domain.policies import effect_key as effect_key_api
from nexus_ai_agent.domain.policies.outbox_policy import (
    CLAIM_LEASE_SECONDS,
    EffectStatus,
    Snapshot,
    classify_failure,
    classify_row,
)

_DDL = """
CREATE TABLE IF NOT EXISTS nexus_effect_outbox (
    seq             INTEGER PRIMARY KEY,
    operation_type  TEXT NOT NULL,
    effect_key      TEXT NOT NULL,
    attempt_id      TEXT NOT NULL,
    destination     TEXT NOT NULL,
    logical_entity  TEXT NOT NULL,
    payload_json    TEXT NOT NULL,
    status          TEXT NOT NULL,
    attempt         INTEGER NOT NULL DEFAULT 0,
    claimed_by      TEXT,
    lease_until     INTEGER NOT NULL DEFAULT 0,
    not_before      INTEGER NOT NULL DEFAULT 0,
    next_retry_at   INTEGER NOT NULL DEFAULT 0,
    created_at      INTEGER NOT NULL,
    finished_at     INTEGER,
    outcome_json    TEXT,
    last_error      TEXT,
    UNIQUE (operation_type, effect_key)
)

"""

#: Column list shared by every read (keeps row shapes identical under dict()).
_ROW_COLUMNS = (
    "seq, operation_type, effect_key, attempt_id, destination, logical_entity, "
    "payload_json, status, attempt, claimed_by, lease_until, not_before, "
    "next_retry_at, created_at, finished_at, outcome_json, last_error"
)

#: Cap on a persisted error message (side-channel guard; see module docstring).
_ERROR_MAX_CHARS: Final[int] = 300


@dataclass(frozen=True)
class Outcome:
    """Public result of one dispatch pass over one effect key."""

    operation_type: str
    effect_key: str
    action: str  # dispatched | dedupe | retrying | permanent | error
    status: str


def _now_epoch() -> int:
    return int(time.time())


class OutboxDispatcherStore:
    """SQLite store backing the reference dispatcher.

    All mutation is a single, guarded statement under a cross-thread lock.
    SQLite serialises writers, so the guarded-UPDATE with a rowcount check is
    the single-writer claim (the SQLite analogue of ``FOR UPDATE SKIP LOCKED``;
    two processes cannot both win the guarded UPDATE).
    """

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._lock = threading.Lock()
        self._memory = db_path == ":memory:"
        self._mem_conn: sqlite3.Connection | None = None
        if self._memory:
            self._mem_conn = sqlite3.connect(":memory:", check_same_thread=False)
            self._mem_conn.row_factory = sqlite3.Row
            self._mem_conn.execute(_DDL)
            self._mem_conn.commit()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        if self._mem_conn is not None:
            yield self._mem_conn
            self._mem_conn.commit()
            return
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute(_DDL)
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    # -- write path --------------------------------------------------------

    def append_or_get(
        self,
        *,
        operation_type: str,
        effect_key: str,
        attempt_id: str,
        destination: str,
        logical_entity: str,
        payload: dict[str, object],
        now: int,
    ) -> tuple[dict, bool]:
        """Durably record one delivery intent, or return the existing row.

        The dedupe is the UNIQUE(operation_type, effect_key) constraint — the
        database's atomicity, not an in-memory set (mutation B must turn the
        suite red if this is removed).
        """
        payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        with self._lock, self._connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO nexus_effect_outbox
                    (operation_type, effect_key, attempt_id, destination, logical_entity,
                     payload_json, status, attempt, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, ?)
                ON CONFLICT (operation_type, effect_key) DO NOTHING
                RETURNING seq, operation_type, effect_key, attempt_id, destination,
                          logical_entity, payload_json, status, attempt, claimed_by,
                          lease_until, not_before, next_retry_at, created_at,
                          finished_at, outcome_json, last_error
                """,
                (
                    operation_type,
                    effect_key,
                    attempt_id,
                    destination,
                    logical_entity,
                    payload_json,
                    now,
                ),
            )
            inserted = cursor.fetchone()
            if inserted is not None:
                return dict(inserted), True
            # Conflict: an earlier intent owns this (operation_type, effect_key).
            # The re-insert was a no-op BY THE UNIQUE CONSTRAINT (mutation B
            # removes that backstop and must turn the suite red).
            existing = connection.execute(
                f"SELECT {_ROW_COLUMNS} FROM nexus_effect_outbox "
                "WHERE operation_type = ? AND effect_key = ?",
                (operation_type, effect_key),
            ).fetchone()
            return dict(existing), False

    def claim(
        self,
        *,
        operation_type: str,
        effect_key: str,
        dispatcher_id: str,
        attempt_id: str,
        now: int,
    ) -> bool:
        """Atomically claim a claimable row using the pure policy verdict.

        The guarded ``UPDATE ... WHERE attempt = expected`` makes the old
        world-state the precondition: if two workers race, exactly one rowcount
        is 1. Returns ``True`` only to the winner.
        """
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT status, claimed_by, lease_until, not_before, next_retry_at, attempt "
                "FROM nexus_effect_outbox WHERE operation_type = ? AND effect_key = ?",
                (operation_type, effect_key),
            ).fetchone()
            if row is None:
                return False
            verdict = classify_row(
                Snapshot(
                    sequence=0,
                    effect_key=effect_key,
                    status=EffectStatus(str(row["status"])),
                    attempt=int(row["attempt"]),
                    claimed_by=row["claimed_by"],
                    lease_until=int(row["lease_until"]),
                    not_before=int(row["not_before"]),
                    next_retry_at=int(row["next_retry_at"]),
                    created_at=0,
                ),
                now=now,
                dispatcher_id=dispatcher_id,
            )
            if verdict != "claim":
                return False
            cursor = connection.execute(
                """
                UPDATE nexus_effect_outbox
                SET status = 'claimed',
                    attempt = attempt + 1,
                    claimed_by = ?,
                    attempt_id = ?,
                    lease_until = ?,
                    last_error = NULL
                WHERE operation_type = ? AND effect_key = ? AND attempt = ?
                """,
                (
                    dispatcher_id,
                    attempt_id,
                    now + CLAIM_LEASE_SECONDS,
                    operation_type,
                    effect_key,
                    int(row["attempt"]),
                ),
            )
            return cursor.rowcount == 1

    def mark_succeeded(
        self,
        *,
        operation_type: str,
        effect_key: str,
        attempt_id: str,
        outcome: dict[str, object],
        now: int,
    ) -> bool:
        """Record SUCCEEDED only for the row still owned by ``attempt_id``."""
        with self._lock, self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE nexus_effect_outbox
                SET status = 'succeeded', finished_at = ?, outcome_json = ?,
                    claimed_by = NULL, lease_until = 0, last_error = NULL
                WHERE operation_type = ? AND effect_key = ? AND attempt_id = ?
                """,
                (
                    now,
                    json.dumps(outcome, ensure_ascii=False, sort_keys=True),
                    operation_type,
                    effect_key,
                    attempt_id,
                ),
            )
            return cursor.rowcount == 1

    def record_attempt(
        self,
        *,
        operation_type: str,
        effect_key: str,
        attempt_id: str,
        retryable: bool,
        now: int,
        error: str,
    ) -> EffectStatus:
        """Persist a failed attempt and return the row's new state.

        The ``attempt_id`` guard fences stale claimants (C3/C5): a worker whose
        claim was superseded cannot write an outcome for a newer attempt.
        """
        # attempt number comes from the row, not the caller.
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT attempt FROM nexus_effect_outbox "
                "WHERE operation_type = ? AND effect_key = ? AND attempt_id = ?",
                (operation_type, effect_key, attempt_id),
            ).fetchone()
            if row is None:
                return EffectStatus.CLAIMED  # superseded: no write, signal "stale"
            attempt = int(row["attempt"])
            state, _ = classify_failure(retryable=retryable, attempt=attempt)
            truncated = error[:_ERROR_MAX_CHARS]
            if state is EffectStatus.FAILED_PERMANENT:
                connection.execute(
                    """
                    UPDATE nexus_effect_outbox
                    SET status = 'failed_permanent', finished_at = ?,
                        claimed_by = NULL, lease_until = 0, last_error = ?
                    WHERE operation_type = ? AND effect_key = ? AND attempt_id = ?
                    """,
                    (now, truncated, operation_type, effect_key, attempt_id),
                )
            else:
                next_at = _retry_delay(attempt) + now
                connection.execute(
                    """
                    UPDATE nexus_effect_outbox
                    SET status = 'failed_retryable', claimed_by = NULL, lease_until = 0,
                        last_error = ?, next_retry_at = ?
                    WHERE operation_type = ? AND effect_key = ? AND attempt_id = ?
                    """,
                    (truncated, next_at, operation_type, effect_key, attempt_id),
                )
            return state

    # -- read path ---------------------------------------------------------

    def snapshot(self, operation_type: str, effect_key: str) -> dict | None:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                f"SELECT {_ROW_COLUMNS} FROM nexus_effect_outbox "
                "WHERE operation_type = ? AND effect_key = ?",
                (operation_type, effect_key),
            ).fetchone()
            return dict(row) if row is not None else None

    def pending_rows(self) -> list[dict]:
        """Every non-terminal row, oldest first (reconciliation seed)."""
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                f"SELECT {_ROW_COLUMNS} FROM nexus_effect_outbox "
                "WHERE status IN ('pending', 'claimed', 'failed_retryable') ORDER BY seq"
            ).fetchall()
            return [dict(row) for row in rows]

    def all_rows(self) -> list[dict]:
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                f"SELECT {_ROW_COLUMNS} FROM nexus_effect_outbox ORDER BY seq"
            ).fetchall()
            return [dict(row) for row in rows]


def _retry_delay(attempt: int) -> int:
    # Bounded exponential, mirroring the pure policy's backoff shape.
    return min(2 ** max(attempt - 1, 0), 60)


def _decode_payload(row: dict | None) -> dict[str, object]:
    if not row:
        return {}
    parsed = json.loads(str(row.get("payload_json") or "{}"))
    return {str(key): value for key, value in parsed.items()} if isinstance(parsed, dict) else {}


def _decode_outcome(row: dict | None) -> dict[str, object]:
    if not row or not row.get("outcome_json"):
        return {}
    parsed = json.loads(str(row["outcome_json"]))
    return {str(key): value for key, value in parsed.items()} if isinstance(parsed, dict) else {}


class OutboxDispatcher:
    """Reference dispatcher: bound effect adapters + pure policy + store.

    Args:
        store: the SQLite store (file path string or ``":memory:"``).
        adapters: per-operation-type ``EffectAdapter`` instances.
        dispatcher_id: stable lease-owner identity for this process (not a secret).
    """

    def __init__(
        self,
        store: OutboxDispatcherStore,
        adapters: dict[str, EffectAdapter],
        *,
        dispatcher_id: str = "dispatcher-1",
    ) -> None:
        self.store = store
        self.adapters = adapters
        self.dispatcher_id = dispatcher_id

    # -- producer side -------------------------------------------------------

    def record_intent(
        self,
        *,
        operation_type: str,
        logical_entity: str | int,
        logical_revision: str | int,
        destination: str | int,
        logical_slot: str | int | None = None,
        payload: dict[str, object],
        attempt_id: str,
        now: int | None = None,
    ) -> dict:
        """Build the deterministic effect key from logical intent and record it.

        Returns the stored row (``created=False`` when the intent already
        existed — i.e. the caller re-submitted a duplicate).
        """
        key = effect_key_api.effect_key(
            operation_type=operation_type,
            logical_entity=logical_entity,
            logical_revision=logical_revision,
            destination=destination,
            logical_slot=logical_slot,
        )
        typed = effect_key_api.serialize_effect_key(operation_type, key)
        row, _created = self.store.append_or_get(
            operation_type=operation_type,
            effect_key=typed,
            attempt_id=attempt_id,
            destination=str(destination),
            logical_entity=str(logical_entity),
            payload=payload,
            now=now if now is not None else _now_epoch(),
        )
        return row

    # -- consumer side ---------------------------------------------------------

    async def dispatch(
        self,
        *,
        operation_type: str,
        effect_key: str,
        attempt_id: str,
        now: int | None = None,
    ) -> Outcome:
        """One pass over one effect key: claim → deliver → record."""
        t = now if now is not None else _now_epoch()
        if self.store.claim(
            operation_type=operation_type,
            effect_key=effect_key,
            dispatcher_id=self.dispatcher_id,
            attempt_id=attempt_id,
            now=t,
        ):
            return await self._deliver(operation_type=operation_type, effect_key=effect_key, now=t)
        row = self.store.snapshot(operation_type, effect_key)
        if row is None:
            return Outcome(operation_type, effect_key, "error", "missing")
        status = str(row["status"])
        if status in ("succeeded", "failed_permanent"):
            return Outcome(operation_type, effect_key, "dedupe", status)
        return Outcome(operation_type, effect_key, "retrying", status)

    async def _deliver(self, *, operation_type: str, effect_key: str, now: int) -> Outcome:
        row = self.store.snapshot(operation_type, effect_key)
        adapter = self.adapters.get(operation_type)
        attempt_id = str(row["attempt_id"]) if row else ""
        if adapter is None:
            self.store.record_attempt(
                operation_type=operation_type,
                effect_key=effect_key,
                attempt_id=attempt_id,
                retryable=False,
                now=now,
                error=f"no adapter registered for {operation_type}",
            )
            return Outcome(operation_type, effect_key, "permanent", "failed_permanent")
        payload = _decode_payload(row)
        destination = str(row["destination"]) if row else ""
        try:
            result = await adapter.deliver(
                effect_key=effect_key, payload=payload, destination=destination
            )
            success = self.store.mark_succeeded(
                operation_type=operation_type,
                effect_key=effect_key,
                attempt_id=attempt_id,
                outcome=result,
                now=now,
            )
            if not success:
                return Outcome(operation_type, effect_key, "retrying", "superseded")
            return Outcome(operation_type, effect_key, "dispatched", "succeeded")
        except DeliveryError as exc:
            self.store.record_attempt(
                operation_type=operation_type,
                effect_key=effect_key,
                attempt_id=attempt_id,
                retryable=exc.retryable,
                now=now,
                error=str(exc),
            )
            status = "failed_permanent" if not exc.retryable else "failed_retryable"
            action = "permanent" if not exc.retryable else "retrying"
            return Outcome(operation_type, effect_key, action, status)
        except Exception as exc:  # noqa: BLE001 — an unknown remote result is a retryable ambiguity
            self.store.record_attempt(
                operation_type=operation_type,
                effect_key=effect_key,
                attempt_id=attempt_id,
                retryable=True,
                now=now,
                error=f"{type(exc).__name__}: {exc}",
            )
            return Outcome(operation_type, effect_key, "retrying", "failed_retryable")
