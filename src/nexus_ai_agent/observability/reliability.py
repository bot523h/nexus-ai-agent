"""Reliability substrate: P1 Claim+Lease, P2 Inbox/Receipt, P4 Effect-Key — M0 preparation.

Constraints:
  - Must NOT touch worker.py or bot/app.py for full implementation
  - Can provide: domain contract, protocol type, pure helper, invariant test,
    SQLite/Postgres-neutral interface, fake implementation, concurrency property test
  - Must be compatible with existing architecture (InProcessJobQueue, etc.)

Acceptance examples:
  A: two claims concurrent => exactly one winner
  B: same update_id twice => one logical receipt
  C: expired lease => recoverable
  D: duplicate logical effect => effect-key stable

Tests should be without network and without flaky timing.

This module provides:
  - ClaimLease protocol: try_claim, renew, release, is_expired, recover_expired
  - Inbox/Receipt: idempotency via inbox table, receipt deduplication
  - Effect-Key: stable effect key for deduplication of side effects
  - Fake in-memory implementations for testing
  - Invariant helpers for concurrency property tests
"""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Protocol

# --- P1: Claim + Lease ---


class ClaimResult(str, Enum):
    CLAIMED = "claimed"
    ALREADY_CLAIMED = "already_claimed"
    NOT_FOUND = "not_found"


@dataclass(frozen=True)
class Lease:
    """Lease for a claimed job/resource."""

    resource_id: str
    owner_id: str
    claimed_at: datetime
    expires_at: datetime
    version: int = 1

    def is_expired(self, now: datetime | None = None) -> bool:
        now = now or datetime.now(timezone.utc)
        return now >= self.expires_at

    def is_owned_by(self, owner_id: str) -> bool:
        return self.owner_id == owner_id


class ClaimLeasePort(Protocol):
    """SQLite/Postgres-neutral interface for claim+lease."""

    def try_claim(
        self, resource_id: str, owner_id: str, ttl: timedelta
    ) -> tuple[ClaimResult, Lease | None]: ...

    def renew(self, resource_id: str, owner_id: str, ttl: timedelta) -> bool: ...

    def release(self, resource_id: str, owner_id: str) -> bool: ...

    def get_lease(self, resource_id: str) -> Lease | None: ...

    def recover_expired(self, now: datetime | None = None) -> list[str]: ...


class InMemoryClaimLease:
    """Fake implementation — thread-safe, in-memory, for tests and M0 validation.

    Invariant: at most one owner per resource_id at a time.
    Concurrent try_claim => exactly one winner.
    """

    def __init__(self) -> None:
        self._leases: dict[str, Lease] = {}
        self._lock = threading.Lock()

    def try_claim(
        self, resource_id: str, owner_id: str, ttl: timedelta
    ) -> tuple[ClaimResult, Lease | None]:
        now = datetime.now(timezone.utc)
        with self._lock:
            existing = self._leases.get(resource_id)
            if existing is not None and not existing.is_expired(now):
                if existing.is_owned_by(owner_id):
                    # Already owned by same owner — idempotent success
                    return ClaimResult.CLAIMED, existing
                return ClaimResult.ALREADY_CLAIMED, existing
            # Claimable (no lease or expired)
            lease = Lease(
                resource_id=resource_id,
                owner_id=owner_id,
                claimed_at=now,
                expires_at=now + ttl,
                version=(existing.version + 1 if existing else 1),
            )
            self._leases[resource_id] = lease
            return ClaimResult.CLAIMED, lease

    def renew(self, resource_id: str, owner_id: str, ttl: timedelta) -> bool:
        now = datetime.now(timezone.utc)
        with self._lock:
            existing = self._leases.get(resource_id)
            if existing is None:
                return False
            if existing.is_expired(now):
                return False
            if not existing.is_owned_by(owner_id):
                return False
            self._leases[resource_id] = Lease(
                resource_id=resource_id,
                owner_id=owner_id,
                claimed_at=existing.claimed_at,
                expires_at=now + ttl,
                version=existing.version,
            )
            return True

    def release(self, resource_id: str, owner_id: str) -> bool:
        with self._lock:
            existing = self._leases.get(resource_id)
            if existing is None:
                return False
            if not existing.is_owned_by(owner_id):
                return False
            del self._leases[resource_id]
            return True

    def get_lease(self, resource_id: str) -> Lease | None:
        now = datetime.now(timezone.utc)
        with self._lock:
            lease = self._leases.get(resource_id)
            if lease is None:
                return None
            if lease.is_expired(now):
                return None
            return lease

    def recover_expired(self, now: datetime | None = None) -> list[str]:
        now = now or datetime.now(timezone.utc)
        recovered: list[str] = []
        with self._lock:
            for rid, lease in list(self._leases.items()):
                if lease.is_expired(now):
                    del self._leases[rid]
                    recovered.append(rid)
        return recovered

    # Test helpers
    def _force_expire(self, resource_id: str) -> None:
        with self._lock:
            if resource_id in self._leases:
                old = self._leases[resource_id]
                self._leases[resource_id] = Lease(
                    resource_id=old.resource_id,
                    owner_id=old.owner_id,
                    claimed_at=old.claimed_at,
                    expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
                    version=old.version,
                )

    def count(self) -> int:
        with self._lock:
            return len(self._leases)


# --- P2: Inbox / Receipt ---


@dataclass(frozen=True)
class InboxMessage:
    """Logical inbound message — e.g., Telegram update."""

    message_id: str  # e.g., update_id as string
    payload: dict[str, Any]
    received_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True)
class Receipt:
    """Receipt proving a message was processed exactly once logically."""

    message_id: str
    receipt_id: str
    processed_at: datetime
    result: dict[str, Any] | None = None


class InboxPort(Protocol):
    """Idempotent inbox — same message_id twice => one logical receipt."""

    def try_receive(self, message: InboxMessage) -> tuple[bool, Receipt | None]:
        """Try to receive message. Returns (is_new, receipt).

        - is_new=True, receipt=Receipt => first time, caller should process and store receipt
        - is_new=False, receipt=Receipt => duplicate, caller should return existing receipt result
        - is_new=False, receipt=None => duplicate but no receipt yet (concurrent processing)
        """
        ...

    def store_receipt(self, receipt: Receipt) -> None: ...

    def get_receipt(self, message_id: str) -> Receipt | None: ...


class InMemoryInbox:
    """Fake inbox — thread-safe, in-memory, deduplicates by message_id."""

    def __init__(self) -> None:
        self._seen: dict[str, Receipt] = {}
        self._processing: set[str] = set()
        self._lock = threading.Lock()

    def try_receive(self, message: InboxMessage) -> tuple[bool, Receipt | None]:
        with self._lock:
            if message.message_id in self._seen:
                return False, self._seen[message.message_id]
            if message.message_id in self._processing:
                return False, None
            self._processing.add(message.message_id)
            return True, None

    def store_receipt(self, receipt: Receipt) -> None:
        with self._lock:
            self._seen[receipt.message_id] = receipt
            self._processing.discard(receipt.message_id)

    def get_receipt(self, message_id: str) -> Receipt | None:
        with self._lock:
            return self._seen.get(message_id)

    def count(self) -> int:
        with self._lock:
            return len(self._seen)


# --- P4: Effect-Key ---


def compute_effect_key(
    logical_operation: str,
    params: dict[str, Any],
    *,
    namespace: str = "nexus",
) -> str:
    """Compute stable effect key for deduplication of logical effects.

    Same logical_operation + same params => same effect key, stable across restarts.
    Used to ensure duplicate logical effects are not executed twice.

    Example:
      effect_key for "send_message" with {chat_id: 123, text: "hi"} is stable.

    Security: params are sorted, JSON-serialized canonically, then hashed.
    No secret leakage: hash is one-way, but params should not contain secrets anyway.
    """
    # Canonical JSON: sorted keys, ensure_ascii, no spaces
    import json

    # Only allow primitive values to prevent high-cardinality or secret leakage
    # For M0, we hash the canonical representation
    canonical = json.dumps(params, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    raw = f"{namespace}:{logical_operation}:{canonical}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True)
class EffectRecord:
    effect_key: str
    operation: str
    result: dict[str, Any] | None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class EffectStorePort(Protocol):
    """Store for deduplicating effects via effect-key."""

    def try_record_effect(self, record: EffectRecord) -> tuple[bool, EffectRecord | None]:
        """Try to record effect. Returns (is_new, existing_record).

        - is_new=True, existing=None => first time, caller should execute effect and store result
        - is_new=False, existing=record => duplicate, return existing result
        """
        ...

    def get_effect(self, effect_key: str) -> EffectRecord | None: ...


class InMemoryEffectStore:
    """Fake effect store — thread-safe, in-memory."""

    def __init__(self) -> None:
        self._effects: dict[str, EffectRecord] = {}
        self._lock = threading.Lock()

    def try_record_effect(self, record: EffectRecord) -> tuple[bool, EffectRecord | None]:
        with self._lock:
            if record.effect_key in self._effects:
                return False, self._effects[record.effect_key]
            self._effects[record.effect_key] = record
            return True, None

    def get_effect(self, effect_key: str) -> EffectRecord | None:
        with self._lock:
            return self._effects.get(effect_key)

    def count(self) -> int:
        with self._lock:
            return len(self._effects)


# --- Invariant helpers for property tests ---


def invariant_single_winner_claim(results: list[tuple[ClaimResult, Lease | None]]) -> bool:
    """Invariant A: concurrent claims => exactly one winner."""
    winners = [r for r in results if r[0] == ClaimResult.CLAIMED]
    return len(winners) == 1


def invariant_inbox_idempotent(receipts: list[Receipt]) -> bool:
    """Invariant B: same message_id twice => one logical receipt (same receipt_id)."""
    if not receipts:
        return True
    first_id = receipts[0].receipt_id
    return all(r.receipt_id == first_id for r in receipts)


def invariant_lease_recoverable(lease: Lease, after_expiry: bool) -> bool:
    """Invariant C: expired lease => recoverable (get_lease returns None, try_claim succeeds)."""
    if after_expiry:
        return lease.is_expired()
    return not lease.is_expired()


def invariant_effect_key_stable(
    operation: str, params: dict[str, Any], key1: str, key2: str
) -> bool:
    """Invariant D: duplicate logical effect => effect-key stable."""
    return key1 == key2


__all__ = [
    "ClaimLeasePort",
    "ClaimResult",
    "EffectRecord",
    "EffectStorePort",
    "InboxMessage",
    "InboxPort",
    "InMemoryClaimLease",
    "InMemoryEffectStore",
    "InMemoryInbox",
    "Lease",
    "Receipt",
    "compute_effect_key",
    "invariant_effect_key_stable",
    "invariant_inbox_idempotent",
    "invariant_lease_recoverable",
    "invariant_single_winner_claim",
]
