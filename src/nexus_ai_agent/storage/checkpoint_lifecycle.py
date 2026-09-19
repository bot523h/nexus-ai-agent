"""Storage-independent checkpoint retention policy.

The policy is deliberately separate from LangGraph's internal schema.  An
adapter records lifecycle events and supplies records; this module decides
which records are safe to delete.  That separation prevents a library upgrade
from silently turning cleanup into data loss.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol

#: The lifecycle index table name, shared by both backends.  On PostgreSQL
#: it is created by the explicit, isolated Alembic revision
#: ``f4a9c2e71b08`` (PR3 option A); on SQLite the store owns it via
#: ``CREATE TABLE IF NOT EXISTS``.
LIFECYCLE_TABLE_NAME = "nexus_checkpoint_lifecycle"


class LifecycleStore(Protocol):
    """Structural contract for the lifecycle index (SQLite or PostgreSQL).

    The runtime recording saver and the CLI reconciler/inspect code are
    typed against this protocol, so the backend is swappable at the
    composition root without touching either consumer.  ``path`` is the
    store's local, host-scoped anchor for the reconciler cleanup lock —
    for the SQLite store it is the file path; for the PostgreSQL store it
    is a deterministic temp-dir anchor derived from the database URL
    (never the database itself).
    """

    path: str | Path

    def upsert(self, record: CheckpointRecord) -> None: ...

    def records(self) -> list[CheckpointRecord]: ...

    def touch_thread(self, thread_id: str, accessed_at: datetime) -> bool: ...

    def delete_index(self, record: CheckpointRecord) -> None: ...

    def schema_fingerprint(self) -> str: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class CheckpointRecord:
    thread_id: str
    checkpoint_id: str
    created_at: datetime
    last_accessed_at: datetime | None = None
    active_until: datetime | None = None


@dataclass(frozen=True)
class RetentionPolicy:
    max_age: timedelta = timedelta(days=30)
    active_grace: timedelta = timedelta(days=7)

    def __post_init__(self) -> None:
        if self.max_age <= timedelta(0):
            raise ValueError("max_age must be positive")
        if self.active_grace < timedelta(0):
            raise ValueError("active_grace cannot be negative")


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


DEFAULT_RETENTION_POLICY = RetentionPolicy()


def eligible_for_deletion(
    record: CheckpointRecord,
    *,
    now: datetime,
    policy: RetentionPolicy = DEFAULT_RETENTION_POLICY,
) -> bool:
    """Return true only for an old, inactive checkpoint.

    Active threads are protected until ``active_until`` and receive an
    additional grace period.  Callers must still enforce referential safety
    for blobs and descendants in their database adapter.
    """
    current = _utc(now)
    created = _utc(record.created_at)
    if current - created < policy.max_age:
        return False
    if record.active_until is not None:
        protected_until = _utc(record.active_until) + policy.active_grace
        if current < protected_until:
            return False
    if record.last_accessed_at is not None:
        last_access = _utc(record.last_accessed_at)
        if current - last_access < policy.max_age:
            return False
    return True


def eligible_records(
    records: list[CheckpointRecord],
    *,
    now: datetime,
    policy: RetentionPolicy = DEFAULT_RETENTION_POLICY,
) -> list[CheckpointRecord]:
    """Return a stable, deterministic deletion candidate list."""
    return [record for record in records if eligible_for_deletion(record, now=now, policy=policy)]
