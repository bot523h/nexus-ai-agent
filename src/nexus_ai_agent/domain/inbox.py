"""P2 domain vocabulary: update INBOX + RECEIPT state machine (framework-free).

Telegram (and any at-least-once transport) may deliver the same ``update_id``
more than once. The inbox records receipt *before* side effects; the receipt
state machine makes illegal transitions fail closed. Dedup is a unique key on
``update_id`` — never a high-water mark (out-of-order delivery must not drop
a lower id that arrives after a higher one).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum


class ReceiptStatus(str, Enum):
    """Durable lifecycle of one inbox row."""

    RECEIVED = "received"
    PROCESSING = "processing"
    PROCESSED = "processed"
    DEAD = "dead"


class AcceptOutcome(str, Enum):
    """Result of presenting an update_id to the inbox."""

    ACCEPTED = "accepted"  # first sighting — row inserted
    DUPLICATE = "duplicate"  # update_id already present — no second row


class ReceiptTransitionError(ValueError):
    """Raised when a caller requests an illegal status transition."""

    def __init__(self, *, current: ReceiptStatus, target: ReceiptStatus) -> None:
        self.current = current
        self.target = target
        super().__init__(f"illegal receipt transition: {current.value} -> {target.value}")


#: Legal directed edges of the receipt state machine.
LEGAL_TRANSITIONS: dict[ReceiptStatus, frozenset[ReceiptStatus]] = {
    ReceiptStatus.RECEIVED: frozenset({ReceiptStatus.PROCESSING, ReceiptStatus.DEAD}),
    ReceiptStatus.PROCESSING: frozenset(
        {ReceiptStatus.PROCESSED, ReceiptStatus.DEAD, ReceiptStatus.RECEIVED}
    ),
    # Crash mid-processing may return the row to RECEIVED for retry (reclaim),
    # or terminal PROCESSED / DEAD. RECEIVED <- PROCESSING is the only reverse edge.
    ReceiptStatus.PROCESSED: frozenset(),
    ReceiptStatus.DEAD: frozenset(),
}


@dataclass(frozen=True)
class UpdateReceipt:
    """Snapshot of one inbox row."""

    update_id: int
    status: ReceiptStatus
    receipt_token: str
    attempts: int
    payload: dict[str, object]
    error: str | None
    received_at: datetime
    updated_at: datetime
    processed_at: datetime | None


def assert_transition_allowed(current: ReceiptStatus, target: ReceiptStatus) -> None:
    """Raise :class:`ReceiptTransitionError` when ``current → target`` is illegal."""
    allowed = LEGAL_TRANSITIONS.get(current, frozenset())
    if target not in allowed:
        raise ReceiptTransitionError(current=current, target=target)


def validate_update_id(update_id: int) -> int:
    if isinstance(update_id, bool) or not isinstance(update_id, int):
        raise TypeError("update_id must be an int")
    # Telegram update_id is a positive integer that increases over time; 0 is
    # not observed in production payloads and is rejected as malformed.
    if update_id <= 0:
        raise ValueError("update_id must be a positive integer")
    return update_id


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def format_iso_utc(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def parse_iso_utc(value: str | None) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


__all__ = [
    "AcceptOutcome",
    "LEGAL_TRANSITIONS",
    "ReceiptStatus",
    "ReceiptTransitionError",
    "UpdateReceipt",
    "assert_transition_allowed",
    "format_iso_utc",
    "parse_iso_utc",
    "utc_now",
    "validate_update_id",
]
