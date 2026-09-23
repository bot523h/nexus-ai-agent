"""P1 domain vocabulary: job CLAIM + LEASE fencing (framework-free).

A lease is time-bounded ownership of one job row. Liveness (expiry) is not
exclusivity: a paused holder can outlive its lease, so every mutating write
on the protected resource must carry a fencing token the store validates
atomically (compare-and-set on ``lease_token`` / ``lease_version``).

States and transitions are pure; persistence lives in the queue adapter.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum


class LeaseClaimOutcome(str, Enum):
    """Result of a single-job claim attempt."""

    CLAIMED = "claimed"
    NOT_FOUND = "not_found"
    NOT_AVAILABLE = "not_available"  # owned by a live (non-expired) lease
    ALREADY_TERMINAL = "already_terminal"


class LeaseMutationOutcome(str, Enum):
    """Result of heartbeat / complete / fail under fencing."""

    OK = "ok"
    STALE_TOKEN = "stale_token"  # fencing rejection
    NOT_FOUND = "not_found"
    WRONG_STATE = "wrong_state"


#: Default lease TTL for a freshly claimed job (seconds).
DEFAULT_LEASE_TTL_SECONDS = 60

#: Minimum accepted TTL — sub-second leases are operational noise.
MIN_LEASE_TTL_SECONDS = 1

#: Maximum accepted TTL — caps runaway ownership if a client misconfigures.
MAX_LEASE_TTL_SECONDS = 24 * 60 * 60


@dataclass(frozen=True)
class JobLease:
    """Fenced ownership handle returned by a successful claim.

    ``lease_token`` is an opaque, unique-per-claim string. ``lease_version`` is
    a monotonic generation: each successful claim (including reclaim after
    expiry) bumps it. Terminal writes must match both token and version.
    """

    job_id: str
    owner_id: str
    lease_token: str
    lease_version: int
    expires_at: datetime
    job_type: str
    payload: dict[str, object]


def utc_now() -> datetime:
    """Timezone-aware UTC clock (tests may monkeypatch this name on the adapter)."""
    return datetime.now(timezone.utc)


def validate_owner_id(owner_id: str) -> str:
    cleaned = owner_id.strip()
    if not cleaned:
        raise ValueError("owner_id must not be empty")
    if "\x00" in cleaned:
        raise ValueError("owner_id must not contain NUL")
    return cleaned


def validate_lease_ttl_seconds(ttl_seconds: int) -> int:
    if not isinstance(ttl_seconds, int) or isinstance(ttl_seconds, bool):
        raise TypeError("lease_ttl_seconds must be an int")
    if ttl_seconds < MIN_LEASE_TTL_SECONDS:
        raise ValueError(f"lease_ttl_seconds must be >= {MIN_LEASE_TTL_SECONDS}")
    if ttl_seconds > MAX_LEASE_TTL_SECONDS:
        raise ValueError(f"lease_ttl_seconds must be <= {MAX_LEASE_TTL_SECONDS}")
    return ttl_seconds


def compute_expiry(now: datetime, ttl_seconds: int) -> datetime:
    """Return ``now + ttl`` as timezone-aware UTC."""
    ttl = validate_lease_ttl_seconds(ttl_seconds)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now + timedelta(seconds=ttl)


def is_lease_expired(expires_at: datetime | None, *, now: datetime) -> bool:
    """True when there is no expiry (legacy unfenced row) or expiry is past.

    Legacy rows written before P1 have ``lease_expires_at IS NULL`` while
    ``status='processing'``. Treating them as expired restores the historical
    ``resume_pending`` reclaim path and prevents permanent locks.
    """
    if expires_at is None:
        return True
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now >= expires_at


def parse_iso_utc(value: str | None) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    # Accept both ``...Z`` and offset forms produced by datetime.isoformat().
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def format_iso_utc(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


__all__ = [
    "DEFAULT_LEASE_TTL_SECONDS",
    "JobLease",
    "LeaseClaimOutcome",
    "LeaseMutationOutcome",
    "MAX_LEASE_TTL_SECONDS",
    "MIN_LEASE_TTL_SECONDS",
    "compute_expiry",
    "format_iso_utc",
    "is_lease_expired",
    "parse_iso_utc",
    "utc_now",
    "validate_lease_ttl_seconds",
    "validate_owner_id",
]
