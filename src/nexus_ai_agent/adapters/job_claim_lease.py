"""P1 durable store: atomic job CLAIM + LEASE fencing on the queue sidecar.

Works against the same ``nexus_job_queue`` SQLite table owned by
:class:`~nexus_ai_agent.adapters.in_process_job_queue.InProcessJobQueue`.
Lease columns are added with ``ALTER TABLE`` on first open so existing
sidecars stay readable (no Alembic — the queue owns its own DDL).

Claim strategy (chosen after five prior-art searches; see
``docs/architecture/RESEARCH_V2_P1_P2.md``):

* SQLite ``BEGIN IMMEDIATE`` serialises writers (single-file write lock).
* One ``UPDATE … WHERE status='pending' OR (status='processing' AND
  lease expired)`` with ``rowcount == 1`` as the sole winner signal.
* Each claim mints a fresh ``lease_token`` and bumps ``lease_version``
  (fencing). Heartbeat / complete / fail require matching token+version.

This module does **not** schedule asyncio tasks — it is the ownership
primitive. The existing in-process runner remains the executor for the
single-process composition root; multi-worker ownership uses this store.
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

from nexus_ai_agent.application.ports.job_queue import JobStatus
from nexus_ai_agent.domain.lease import (
    DEFAULT_LEASE_TTL_SECONDS,
    JobLease,
    LeaseClaimOutcome,
    LeaseMutationOutcome,
    compute_expiry,
    format_iso_utc,
    is_lease_expired,
    parse_iso_utc,
    utc_now,
    validate_lease_ttl_seconds,
    validate_owner_id,
)

# Columns introduced by P1. Applied with ALTER TABLE IF-missing so a
# pre-P1 sidecar upgrades in place without Alembic.
_LEASE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("owner_id", "TEXT"),
    ("lease_token", "TEXT"),
    ("lease_version", "INTEGER NOT NULL DEFAULT 0"),
    ("lease_expires_at", "TEXT"),
)


class ClaimLeaseStore:
    """Atomic claim / heartbeat / fenced-complete against ``nexus_job_queue``."""

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

    # ------------------------------------------------------------------ schema

    def _ensure_schema(self) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS nexus_job_queue (
                    id TEXT PRIMARY KEY,
                    job_type TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result_json TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    owner_id TEXT,
                    lease_token TEXT,
                    lease_version INTEGER NOT NULL DEFAULT 0,
                    lease_expires_at TEXT
                )
                """
            )
            existing = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(nexus_job_queue)").fetchall()
            }
            for name, decl in _LEASE_COLUMNS:
                if name not in existing:
                    connection.execute(f"ALTER TABLE nexus_job_queue ADD COLUMN {name} {decl}")

    # ------------------------------------------------------------------ claim

    def claim_job(
        self,
        job_id: str,
        *,
        owner_id: str,
        lease_ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS,
        now: datetime | None = None,
    ) -> tuple[LeaseClaimOutcome, JobLease | None]:
        """Atomically claim one job by id.

        Returns ``(CLAIMED, lease)`` on win. A concurrent loser observing the
        same pending row gets ``NOT_AVAILABLE`` (live owner) — never a second
        lease. Terminal rows return ``ALREADY_TERMINAL``.
        """
        owner = validate_owner_id(owner_id)
        ttl = validate_lease_ttl_seconds(lease_ttl_seconds)
        clock = now or utc_now()
        token = uuid4().hex
        expires = compute_expiry(clock, ttl)
        expires_s = format_iso_utc(expires)
        started_s = format_iso_utc(clock)

        now_s = format_iso_utc(clock)

        with self._db_lock, self._immediate() as connection:
            row = connection.execute(
                """
                SELECT id, job_type, payload_json, status, owner_id,
                       lease_token, lease_version, lease_expires_at
                FROM nexus_job_queue WHERE id = ?
                """,
                (job_id,),
            ).fetchone()
            if row is None:
                return LeaseClaimOutcome.NOT_FOUND, None

            status = str(row["status"])
            if status in {JobStatus.COMPLETED.value, JobStatus.FAILED.value}:
                return LeaseClaimOutcome.ALREADY_TERMINAL, None

            if status == JobStatus.PROCESSING.value:
                expires_at = parse_iso_utc(
                    None if row["lease_expires_at"] is None else str(row["lease_expires_at"])
                )
                if not is_lease_expired(expires_at, now=clock):
                    return LeaseClaimOutcome.NOT_AVAILABLE, None
                # Stale / unfenced processing → reclaim path below.

            # pending OR expired-processing: CAS to processing with fresh fence.
            # Expiry predicate compares stored lease_expires_at to *now* (now_s),
            # never to the newly computed expires_s.
            previous_version = int(row["lease_version"] or 0)
            new_version = previous_version + 1
            cursor = connection.execute(
                """
                UPDATE nexus_job_queue
                SET status = ?,
                    owner_id = ?,
                    lease_token = ?,
                    lease_version = ?,
                    lease_expires_at = ?,
                    started_at = ?,
                    finished_at = NULL,
                    error = NULL,
                    result_json = NULL
                WHERE id = ?
                  AND (
                        status = ?
                     OR (
                            status = ?
                        AND (lease_expires_at IS NULL OR lease_expires_at <= ?)
                     )
                  )
                """,
                (
                    JobStatus.PROCESSING.value,
                    owner,
                    token,
                    new_version,
                    expires_s,
                    started_s,
                    job_id,
                    JobStatus.PENDING.value,
                    JobStatus.PROCESSING.value,
                    now_s,
                ),
            )
            if cursor.rowcount != 1:
                # Race: another writer won between SELECT and UPDATE.
                again = connection.execute(
                    """
                    SELECT status, lease_expires_at FROM nexus_job_queue WHERE id = ?
                    """,
                    (job_id,),
                ).fetchone()
                if again is None:
                    return LeaseClaimOutcome.NOT_FOUND, None
                st = str(again["status"])
                if st in {JobStatus.COMPLETED.value, JobStatus.FAILED.value}:
                    return LeaseClaimOutcome.ALREADY_TERMINAL, None
                return LeaseClaimOutcome.NOT_AVAILABLE, None

            payload = _load_payload(str(row["payload_json"]))
            lease = JobLease(
                job_id=job_id,
                owner_id=owner,
                lease_token=token,
                lease_version=new_version,
                expires_at=expires,
                job_type=str(row["job_type"]),
                payload=payload,
            )
            return LeaseClaimOutcome.CLAIMED, lease

    def claim_next(
        self,
        *,
        owner_id: str,
        lease_ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS,
        job_type: str | None = None,
        now: datetime | None = None,
    ) -> tuple[LeaseClaimOutcome, JobLease | None]:
        """Claim the oldest pending (or stale-processing) job, optionally filtered."""
        owner = validate_owner_id(owner_id)
        ttl = validate_lease_ttl_seconds(lease_ttl_seconds)
        clock = now or utc_now()
        now_s = format_iso_utc(clock)
        token = uuid4().hex
        expires = compute_expiry(clock, ttl)
        expires_s = format_iso_utc(expires)

        with self._db_lock, self._immediate() as connection:
            params: list[object] = [
                JobStatus.PENDING.value,
                JobStatus.PROCESSING.value,
                now_s,
            ]
            type_clause = ""
            if job_type is not None:
                type_clause = " AND job_type = ?"
                params.append(job_type.strip())

            row = connection.execute(
                f"""
                SELECT id, job_type, payload_json, lease_version
                FROM nexus_job_queue
                WHERE (
                      status = ?
                   OR (status = ? AND (lease_expires_at IS NULL OR lease_expires_at <= ?))
                )
                {type_clause}
                ORDER BY created_at, id
                LIMIT 1
                """,
                tuple(params),
            ).fetchone()
            if row is None:
                return LeaseClaimOutcome.NOT_AVAILABLE, None

            job_id = str(row["id"])
            previous_version = int(row["lease_version"] or 0)
            new_version = previous_version + 1
            cursor = connection.execute(
                """
                UPDATE nexus_job_queue
                SET status = ?,
                    owner_id = ?,
                    lease_token = ?,
                    lease_version = ?,
                    lease_expires_at = ?,
                    started_at = ?,
                    finished_at = NULL,
                    error = NULL,
                    result_json = NULL
                WHERE id = ?
                  AND (
                        status = ?
                     OR (status = ? AND (lease_expires_at IS NULL OR lease_expires_at <= ?))
                  )
                """,
                (
                    JobStatus.PROCESSING.value,
                    owner,
                    token,
                    new_version,
                    expires_s,
                    now_s,
                    job_id,
                    JobStatus.PENDING.value,
                    JobStatus.PROCESSING.value,
                    now_s,
                ),
            )
            if cursor.rowcount != 1:
                return LeaseClaimOutcome.NOT_AVAILABLE, None

            payload = _load_payload(str(row["payload_json"]))
            lease = JobLease(
                job_id=job_id,
                owner_id=owner,
                lease_token=token,
                lease_version=new_version,
                expires_at=expires,
                job_type=str(row["job_type"]),
                payload=payload,
            )
            return LeaseClaimOutcome.CLAIMED, lease

    # ----------------------------------------------------------- mutations

    def heartbeat(
        self,
        job_id: str,
        *,
        owner_id: str,
        lease_token: str,
        lease_version: int,
        lease_ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS,
        now: datetime | None = None,
    ) -> tuple[LeaseMutationOutcome, datetime | None]:
        """Extend a live lease. Rejects stale tokens (fencing)."""
        owner = validate_owner_id(owner_id)
        ttl = validate_lease_ttl_seconds(lease_ttl_seconds)
        clock = now or utc_now()
        expires = compute_expiry(clock, ttl)
        expires_s = format_iso_utc(expires)

        with self._db_lock, self._immediate() as connection:
            cursor = connection.execute(
                """
                UPDATE nexus_job_queue
                SET lease_expires_at = ?
                WHERE id = ?
                  AND status = ?
                  AND owner_id = ?
                  AND lease_token = ?
                  AND lease_version = ?
                """,
                (
                    expires_s,
                    job_id,
                    JobStatus.PROCESSING.value,
                    owner,
                    lease_token,
                    int(lease_version),
                ),
            )
            if cursor.rowcount == 1:
                return LeaseMutationOutcome.OK, expires
            return self._diagnose_mutation_failure(
                connection,
                job_id,
                owner_id=owner,
                lease_token=lease_token,
                lease_version=lease_version,
            ), None

    def complete(
        self,
        job_id: str,
        *,
        owner_id: str,
        lease_token: str,
        lease_version: int,
        result: dict[str, object],
        now: datetime | None = None,
    ) -> LeaseMutationOutcome:
        """Mark completed under fencing. Stale holders are rejected."""
        if not isinstance(result, dict):
            raise TypeError("result must be a dictionary")
        owner = validate_owner_id(owner_id)
        clock = now or utc_now()
        finished_s = format_iso_utc(clock)
        result_json = json.dumps(result, ensure_ascii=False, sort_keys=True)

        with self._db_lock, self._immediate() as connection:
            cursor = connection.execute(
                """
                UPDATE nexus_job_queue
                SET status = ?,
                    result_json = ?,
                    error = NULL,
                    finished_at = ?,
                    lease_expires_at = NULL
                WHERE id = ?
                  AND status = ?
                  AND owner_id = ?
                  AND lease_token = ?
                  AND lease_version = ?
                """,
                (
                    JobStatus.COMPLETED.value,
                    result_json,
                    finished_s,
                    job_id,
                    JobStatus.PROCESSING.value,
                    owner,
                    lease_token,
                    int(lease_version),
                ),
            )
            if cursor.rowcount == 1:
                return LeaseMutationOutcome.OK
            return self._diagnose_mutation_failure(
                connection,
                job_id,
                owner_id=owner,
                lease_token=lease_token,
                lease_version=lease_version,
            )

    def fail(
        self,
        job_id: str,
        *,
        owner_id: str,
        lease_token: str,
        lease_version: int,
        error: str,
        now: datetime | None = None,
    ) -> LeaseMutationOutcome:
        """Mark failed under fencing."""
        owner = validate_owner_id(owner_id)
        clock = now or utc_now()
        finished_s = format_iso_utc(clock)

        with self._db_lock, self._immediate() as connection:
            cursor = connection.execute(
                """
                UPDATE nexus_job_queue
                SET status = ?,
                    error = ?,
                    finished_at = ?,
                    lease_expires_at = NULL
                WHERE id = ?
                  AND status = ?
                  AND owner_id = ?
                  AND lease_token = ?
                  AND lease_version = ?
                """,
                (
                    JobStatus.FAILED.value,
                    str(error),
                    finished_s,
                    job_id,
                    JobStatus.PROCESSING.value,
                    owner,
                    lease_token,
                    int(lease_version),
                ),
            )
            if cursor.rowcount == 1:
                return LeaseMutationOutcome.OK
            return self._diagnose_mutation_failure(
                connection,
                job_id,
                owner_id=owner,
                lease_token=lease_token,
                lease_version=lease_version,
            )

    def release_to_pending(
        self,
        job_id: str,
        *,
        owner_id: str,
        lease_token: str,
        lease_version: int,
    ) -> LeaseMutationOutcome:
        """Voluntary release (e.g. graceful shutdown) — fenced."""
        owner = validate_owner_id(owner_id)
        with self._db_lock, self._immediate() as connection:
            cursor = connection.execute(
                """
                UPDATE nexus_job_queue
                SET status = ?,
                    owner_id = NULL,
                    lease_token = NULL,
                    lease_expires_at = NULL,
                    started_at = NULL
                WHERE id = ?
                  AND status = ?
                  AND owner_id = ?
                  AND lease_token = ?
                  AND lease_version = ?
                """,
                (
                    JobStatus.PENDING.value,
                    job_id,
                    JobStatus.PROCESSING.value,
                    owner,
                    lease_token,
                    int(lease_version),
                ),
            )
            if cursor.rowcount == 1:
                return LeaseMutationOutcome.OK
            return self._diagnose_mutation_failure(
                connection,
                job_id,
                owner_id=owner,
                lease_token=lease_token,
                lease_version=lease_version,
            )

    def inspect_lease(self, job_id: str) -> dict[str, object] | None:
        """Read ownership columns for diagnostics / tests."""
        with self._db_lock, self._connection() as connection:
            row = connection.execute(
                """
                SELECT id, status, owner_id, lease_token, lease_version, lease_expires_at
                FROM nexus_job_queue WHERE id = ?
                """,
                (job_id,),
            ).fetchone()
            if row is None:
                return None
            return {
                "id": str(row["id"]),
                "status": str(row["status"]),
                "owner_id": None if row["owner_id"] is None else str(row["owner_id"]),
                "lease_token": None if row["lease_token"] is None else str(row["lease_token"]),
                "lease_version": int(row["lease_version"] or 0),
                "lease_expires_at": (
                    None if row["lease_expires_at"] is None else str(row["lease_expires_at"])
                ),
            }

    # -------------------------------------------------------------- helpers

    def _diagnose_mutation_failure(
        self,
        connection: sqlite3.Connection,
        job_id: str,
        *,
        owner_id: str,
        lease_token: str,
        lease_version: int,
    ) -> LeaseMutationOutcome:
        row = connection.execute(
            """
            SELECT status, owner_id, lease_token, lease_version
            FROM nexus_job_queue WHERE id = ?
            """,
            (job_id,),
        ).fetchone()
        if row is None:
            return LeaseMutationOutcome.NOT_FOUND
        status = str(row["status"])
        if status != JobStatus.PROCESSING.value:
            return LeaseMutationOutcome.WRONG_STATE
        if (
            str(row["owner_id"] or "") != owner_id
            or str(row["lease_token"] or "") != lease_token
            or int(row["lease_version"] or 0) != int(lease_version)
        ):
            return LeaseMutationOutcome.STALE_TOKEN
        return LeaseMutationOutcome.STALE_TOKEN

    @contextmanager
    def _immediate(self) -> Iterator[sqlite3.Connection]:
        """Open a write transaction with ``BEGIN IMMEDIATE`` (SQLite writer lock)."""
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


def _load_payload(payload_json: str) -> dict[str, object]:
    parsed = json.loads(payload_json)
    if not isinstance(parsed, dict):
        raise RuntimeError("invalid persisted payload")
    return {str(key): value for key, value in parsed.items()}


__all__ = ["ClaimLeaseStore"]
