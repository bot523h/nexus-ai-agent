from __future__ import annotations

import math
import operator
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import ClassVar


class PresenceError(ValueError):
    """Base class for invalid presence configuration or operations."""


class InvalidPresenceTTL(PresenceError):
    """A TTL is zero, negative, non-finite, or outside the supported bound."""


class InvalidPresenceUser(PresenceError):
    """A presence key is not an integer user identifier."""


class PresenceCapacityError(PresenceError):
    """The bounded store cannot accept a new user without dropping live state."""


class PresenceClockError(PresenceError):
    """The injected monotonic clock returned an unusable value."""


@dataclass
class PresenceStore:
    """Thread-safe, process-local presence with validated monotonic TTLs.

    Presence is intentionally ephemeral: a new process starts empty, and
    separate workers do not share this store.  This prevents pretending that an
    in-memory singleton is durable or cross-process truth.  ``max_entries`` and
    explicit capacity errors keep long-lived workers from growing the mapping
    without bound; expired entries are purged on every public operation.
    """

    ttl_seconds: float = 120.0
    clock: Callable[[], float] = time.monotonic
    max_entries: int = 10_000
    _online_until: dict[int, float] = field(default_factory=dict)
    MAX_TTL_SECONDS: ClassVar[float] = 365.0 * 24.0 * 60.0 * 60.0

    def __post_init__(self) -> None:
        self.ttl_seconds = self._validate_ttl(self.ttl_seconds)
        if not callable(self.clock):
            raise TypeError("clock must be callable")
        if isinstance(self.max_entries, bool) or not isinstance(self.max_entries, int):
            raise TypeError("max_entries must be an integer")
        if self.max_entries < 1:
            raise ValueError("max_entries must be positive")
        self._lock = threading.RLock()
        # A caller may provide an initial mapping for controlled restoration;
        # reject rather than allowing invalid timestamps to enter the store.
        for user_id, expires_at in self._online_until.items():
            self._validate_user_id(user_id)
            self._validate_clock_value(expires_at)
        if len(self._online_until) > self.max_entries:
            raise PresenceCapacityError("initial presence mapping exceeds max_entries")
        # Take a store-owned, revalidated copy so a caller retaining the initial
        # mapping cannot mutate live state behind ``_lock`` or bypass the
        # capacity and timestamp checks.
        self._online_until = {
            self._validate_user_id(uid): self._validate_clock_value(expires_at)
            for uid, expires_at in self._online_until.items()
        }

    def mark_online(self, user_id: int, *, ttl_seconds: float | None = None) -> None:
        """Record one online lease, refreshing an existing lease atomically."""

        uid = self._validate_user_id(user_id)
        with self._lock:
            now = self._now()
            self._purge_expired(now)
            ttl = self.ttl_seconds if ttl_seconds is None else self._validate_ttl(ttl_seconds)
            if uid not in self._online_until and len(self._online_until) >= self.max_entries:
                raise PresenceCapacityError(
                    f"presence store reached max_entries={self.max_entries}"
                )
            self._online_until[uid] = now + ttl

    def mark_offline(self, user_id: int) -> None:
        """Remove a user and opportunistically clean all stale leases."""

        uid = self._validate_user_id(user_id)
        with self._lock:
            now = self._now()
            self._purge_expired(now)
            self._online_until.pop(uid, None)

    def is_online(self, user_id: int) -> bool:
        """Return current liveness and remove every expired entry encountered."""

        uid = self._validate_user_id(user_id)
        with self._lock:
            now = self._now()
            self._purge_expired(now)
            expires_at = self._online_until.get(uid)
            return expires_at is not None and expires_at > now

    def cleanup(self) -> int:
        """Purge stale entries and return the number removed."""

        with self._lock:
            return self._purge_expired(self._now())

    @property
    def size(self) -> int:
        """Return live mapping size after cleanup (useful for observability/tests)."""

        self.cleanup()
        with self._lock:
            return len(self._online_until)

    @property
    def online_count(self) -> int:
        """Alias for ``size`` with presence-oriented naming."""

        return self.size

    def _purge_expired(self, now: float) -> int:
        expired = [uid for uid, expires_at in self._online_until.items() if expires_at <= now]
        for uid in expired:
            self._online_until.pop(uid, None)
        return len(expired)

    def _now(self) -> float:
        try:
            value = self.clock()
        except (TypeError, ValueError, OverflowError) as exc:
            raise PresenceClockError("clock must return a real number") from exc
        # Validate the raw clock result before any coercion: ``float(True)`` or
        # ``float("123")`` would otherwise slip past the type guard.
        return self._validate_clock_value(value)

    @staticmethod
    def _validate_clock_value(value: float) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise PresenceClockError("clock must return a real number")
        result = float(value)
        if not math.isfinite(result):
            raise PresenceClockError("clock must return a finite number")
        return result

    @classmethod
    def _validate_ttl(cls, ttl_seconds: float) -> float:
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, (int, float)):
            raise InvalidPresenceTTL("ttl_seconds must be a real number")
        ttl = float(ttl_seconds)
        if not math.isfinite(ttl) or ttl <= 0.0:
            raise InvalidPresenceTTL("ttl_seconds must be finite and greater than zero")
        if ttl > cls.MAX_TTL_SECONDS:
            raise InvalidPresenceTTL(f"ttl_seconds must not exceed {cls.MAX_TTL_SECONDS:g} seconds")
        return ttl

    @staticmethod
    def _validate_user_id(user_id: int) -> int:
        if isinstance(user_id, bool):
            raise InvalidPresenceUser("user_id must be an integer")
        try:
            normalized = operator.index(user_id)
        except TypeError as exc:
            raise InvalidPresenceUser("user_id must be an integer") from exc
        return int(normalized)


_DEFAULT_STORE = PresenceStore()


def mark_online(user_id: int, *, ttl_seconds: float | None = None) -> None:
    _DEFAULT_STORE.mark_online(user_id, ttl_seconds=ttl_seconds)


def mark_offline(user_id: int) -> None:
    _DEFAULT_STORE.mark_offline(user_id)


def is_online(user_id: int) -> bool:
    return _DEFAULT_STORE.is_online(user_id)


__all__ = [
    "InvalidPresenceTTL",
    "InvalidPresenceUser",
    "PresenceCapacityError",
    "PresenceClockError",
    "PresenceError",
    "PresenceStore",
    "is_online",
    "mark_offline",
    "mark_online",
]
