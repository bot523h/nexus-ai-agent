"""Pure retention predicates and the resumability product contract."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Final


class JournalStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    RETRYING = "retrying"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


ALLOWED_TRANSITIONS: Final[dict[JournalStatus, frozenset[JournalStatus]]] = {
    JournalStatus.PENDING: frozenset({JournalStatus.RUNNING}),
    JournalStatus.RUNNING: frozenset(
        {
            JournalStatus.SUCCEEDED,
            JournalStatus.FAILED,
            JournalStatus.BLOCKED,
            JournalStatus.CANCELLED,
        }
    ),
    # A retry edge increments attempts and applies exponential backoff.
    JournalStatus.FAILED: frozenset({JournalStatus.RETRYING}),
    JournalStatus.RETRYING: frozenset({JournalStatus.RUNNING}),
    JournalStatus.BLOCKED: frozenset({JournalStatus.PENDING}),
    JournalStatus.SUCCEEDED: frozenset(),
    JournalStatus.CANCELLED: frozenset(),
}


@dataclass(frozen=True)
class RetentionRecord:
    created_at: datetime
    last_accessed_at: datetime | None = None
    active_until: datetime | None = None


RESUMABILITY_WINDOW: Final[timedelta] = timedelta(days=30)
FORK_AFTER_RESUMABILITY: Final[str] = "fork_new_thread_from_message_history"
RETRY_BACKOFF: Final[str] = "exponential_backoff_on_failed_to_retrying"


def _utc(value: datetime) -> datetime:
    return (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )


def pinned(record: RetentionRecord, *, now: datetime) -> bool:
    current = _utc(now)
    return record.active_until is not None and current < _utc(record.active_until)


def deletable(record: RetentionRecord, *, now: datetime) -> bool:
    """Pure fail-safe predicate; unknown access/active state is retained."""
    if record.last_accessed_at is None:
        return False
    current = _utc(now)
    if pinned(record, now=current):
        return False
    return current - _utc(record.last_accessed_at) >= RESUMABILITY_WINDOW
