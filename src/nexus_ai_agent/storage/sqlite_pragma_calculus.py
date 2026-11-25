"""Deterministic SQLite PRAGMA enforcement plus queueing-theoretic timeout calculus.

Two responsibilities live here, both fail-closed:

* :func:`apply_and_verify_sqlite_pragmas` writes a :class:`SQLitePragmaPolicy`
  to a connection and then *reads every value back*, raising
  :class:`SQLitePragmaVerificationError` when SQLite did not actually adopt the
  requested invariant (for example a build compiled without foreign-key
  support).  Claiming a durability or integrity guarantee that the engine never
  activated would be a silent lie, so the read-back is the contract, not the
  write.
* :func:`compute_optimal_busy_timeout_ms` sizes ``busy_timeout`` from an M/G/1
  (Kingman) waiting-time bound instead of guessing a magic number.
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

__all__ = [
    "ContentionCalculusReceipt",
    "PragmaValidationError",
    "QueueInstabilityError",
    "SQLitePragmaPolicy",
    "SQLitePragmaReceipt",
    "SQLitePragmaVerificationError",
    "apply_and_verify_sqlite_pragmas",
    "compute_optimal_busy_timeout_ms",
    "policy_for_contention",
]

_JOURNAL_MODES: Final[frozenset[str]] = frozenset(
    {"DELETE", "TRUNCATE", "PERSIST", "MEMORY", "WAL", "OFF"}
)
_SYNC_VALUES: Final[Mapping[str, int]] = MappingProxyType(
    {"OFF": 0, "NORMAL": 1, "FULL": 2, "EXTRA": 3}
)
_TEMP_STORE_VALUES: Final[Mapping[str, int]] = MappingProxyType(
    {"DEFAULT": 0, "FILE": 1, "MEMORY": 2}
)


class SQLitePragmaVerificationError(RuntimeError):
    """Raised when SQLite did not honour a pragma the caller required."""


class PragmaValidationError(ValueError):
    """Raised when a requested pragma policy is internally inconsistent."""


class QueueInstabilityError(ArithmeticError):
    """Raised when the offered load makes the queue mathematically unstable."""


@dataclass(frozen=True, slots=True)
class SQLitePragmaPolicy:
    """The durability/integrity invariants a connection must prove it adopted."""

    journal_mode: str = "WAL"
    synchronous: str = "FULL"
    foreign_keys: bool = True
    busy_timeout_ms: int = 5000
    temp_store: str = "MEMORY"
    trusted_schema: bool = False
    cell_size_check: bool = True
    require_wal_for_file_backed: bool = True
    allow_relaxed_durability: bool = False

    def __post_init__(self) -> None:
        journal = _normalise_choice("journal_mode", self.journal_mode, _JOURNAL_MODES)
        sync = _normalise_choice("synchronous", self.synchronous, _SYNC_VALUES)
        temp = _normalise_choice("temp_store", self.temp_store, _TEMP_STORE_VALUES)
        object.__setattr__(self, "journal_mode", journal)
        object.__setattr__(self, "synchronous", sync)
        object.__setattr__(self, "temp_store", temp)
        if isinstance(self.busy_timeout_ms, bool) or not isinstance(self.busy_timeout_ms, int):
            raise TypeError("busy_timeout_ms must be an int, not bool or float")
        if self.busy_timeout_ms <= 0:
            raise ValueError("busy_timeout_ms must be strictly positive")
        for flag in (
            "foreign_keys",
            "trusted_schema",
            "cell_size_check",
            "require_wal_for_file_backed",
            "allow_relaxed_durability",
        ):
            if not isinstance(getattr(self, flag), bool):
                raise TypeError(f"{flag} must be a bool")
        if self.synchronous == "NORMAL" and not self.allow_relaxed_durability:
            raise PragmaValidationError(
                "synchronous=NORMAL requires explicit allow_relaxed_durability=True"
            )
        if self.require_wal_for_file_backed and self.journal_mode != "WAL":
            raise ValueError("require_wal_for_file_backed demands journal_mode='WAL'")


@dataclass(frozen=True, slots=True)
class SQLitePragmaReceipt:
    """Immutable proof of the pragma values observed after enforcement."""

    file_backed: bool
    journal_mode: str
    synchronous: str
    foreign_keys: bool
    busy_timeout_ms: int
    temp_store: str
    trusted_schema: bool
    cell_size_check: bool
    verified_pragmas: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "verified_pragmas", MappingProxyType(dict(self.verified_pragmas)))


@dataclass(frozen=True, slots=True)
class ContentionCalculusReceipt:
    """Immutable result of the Kingman bound computation."""

    utilization: float
    mean_wait_ms: float
    busy_timeout_ms: int
    target_tail_prob: float
    verified_pragmas: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not math.isfinite(self.utilization) or not 0.0 <= self.utilization < 1.0:
            raise ValueError("utilization must be finite and in [0, 1)")
        if not math.isfinite(self.mean_wait_ms) or self.mean_wait_ms < 0.0:
            raise ValueError("mean_wait_ms must be finite and non-negative")
        if isinstance(self.busy_timeout_ms, bool) or not isinstance(self.busy_timeout_ms, int):
            raise TypeError("busy_timeout_ms must be an int")
        if self.busy_timeout_ms <= 0:
            raise ValueError("busy_timeout_ms must be strictly positive")
        if not 0.0 < self.target_tail_prob < 1.0:
            raise ValueError("target_tail_prob must lie strictly inside (0, 1)")


def _normalise_choice(name: str, value: object, allowed: Collection[str]) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    upper = value.strip().upper()
    if upper not in allowed:
        raise ValueError(f"{name} must be one of {sorted(allowed)}, got {value!r}")
    return upper


def _pragma_scalar(conn: sqlite3.Connection, name: str) -> object:
    row = conn.execute(f"PRAGMA {name}").fetchone()
    if row is None:
        raise SQLitePragmaVerificationError(f"PRAGMA {name} returned no row")
    return row[0]


def _is_file_backed(conn: sqlite3.Connection) -> bool:
    row = conn.execute("PRAGMA database_list").fetchone()
    if row is None or len(row) < 3:
        raise SQLitePragmaVerificationError("PRAGMA database_list returned no usable row")
    path = row[2]
    return bool(path)


def _apply_journal_mode(
    conn: sqlite3.Connection, policy: SQLitePragmaPolicy, file_backed: bool
) -> str:
    if file_backed:
        try:
            conn.execute(f"PRAGMA journal_mode={policy.journal_mode}")
        except sqlite3.Error as exc:  # pragma: no cover - exotic builds
            raise SQLitePragmaVerificationError(
                f"could not set journal_mode={policy.journal_mode}: {exc}"
            ) from exc
    observed = str(_pragma_scalar(conn, "journal_mode")).lower()
    if file_backed and policy.require_wal_for_file_backed and observed != "wal":
        raise SQLitePragmaVerificationError(
            f"journal_mode is {observed!r}, required 'wal' on a file-backed database"
        )
    if not file_backed and observed not in {"memory", "delete", "truncate", "persist", "off"}:
        raise SQLitePragmaVerificationError(
            f"unexpected journal_mode {observed!r} for an in-memory database"
        )
    return observed


def _as_int(value: object) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        return int(value)
    raise SQLitePragmaVerificationError(f"PRAGMA value {value!r} is not an integer")


def _verify_scalar(conn: sqlite3.Connection, pragma: str, expected: int, *, label: str) -> int:
    observed = _pragma_scalar(conn, pragma)
    observed_int = _as_int(observed)
    if observed_int != expected:
        raise SQLitePragmaVerificationError(
            f"{label}: PRAGMA {pragma} read back {observed!r}, expected {expected}"
        )
    return observed_int


def apply_and_verify_sqlite_pragmas(
    conn: sqlite3.Connection, policy: SQLitePragmaPolicy
) -> SQLitePragmaReceipt:
    """Enforce ``policy`` on ``conn`` and prove each value by reading it back."""

    if not isinstance(conn, sqlite3.Connection):
        raise TypeError("conn must be a sqlite3.Connection")
    file_backed = _is_file_backed(conn)

    journal = _apply_journal_mode(conn, policy, file_backed)

    sync_expected = _SYNC_VALUES[policy.synchronous]
    try:
        conn.execute(f"PRAGMA synchronous={policy.synchronous}")
    except sqlite3.Error as exc:  # pragma: no cover - exotic builds
        raise SQLitePragmaVerificationError(f"could not set synchronous: {exc}") from exc
    _verify_scalar(conn, "synchronous", sync_expected, label="durability mode")

    fk_expected = 1 if policy.foreign_keys else 0
    try:
        conn.execute(f"PRAGMA foreign_keys={'ON' if policy.foreign_keys else 'OFF'}")
    except sqlite3.Error as exc:
        raise SQLitePragmaVerificationError(
            f"foreign-key activation failed (compiled without FK support?): {exc}"
        ) from exc
    _verify_scalar(conn, "foreign_keys", fk_expected, label="referential integrity")

    conn.execute(f"PRAGMA busy_timeout={policy.busy_timeout_ms}")
    _verify_scalar(conn, "busy_timeout", policy.busy_timeout_ms, label="writer contention")

    temp_expected = _TEMP_STORE_VALUES[policy.temp_store]
    conn.execute(f"PRAGMA temp_store={policy.temp_store}")
    _verify_scalar(conn, "temp_store", temp_expected, label="temp storage")

    conn.execute(f"PRAGMA trusted_schema={'ON' if policy.trusted_schema else 'OFF'}")
    _verify_scalar(conn, "trusted_schema", 1 if policy.trusted_schema else 0, label="schema trust")

    conn.execute(f"PRAGMA cell_size_check={'ON' if policy.cell_size_check else 'OFF'}")
    _verify_scalar(
        conn, "cell_size_check", 1 if policy.cell_size_check else 0, label="cell integrity"
    )

    verified = {
        "journal_mode": journal,
        "synchronous": str(sync_expected),
        "foreign_keys": str(fk_expected),
        "busy_timeout": str(policy.busy_timeout_ms),
        "temp_store": str(temp_expected),
        "trusted_schema": str(1 if policy.trusted_schema else 0),
        "cell_size_check": str(1 if policy.cell_size_check else 0),
    }
    return SQLitePragmaReceipt(
        file_backed=file_backed,
        journal_mode=journal,
        synchronous=policy.synchronous,
        foreign_keys=policy.foreign_keys,
        busy_timeout_ms=policy.busy_timeout_ms,
        temp_store=policy.temp_store,
        trusted_schema=policy.trusted_schema,
        cell_size_check=policy.cell_size_check,
        verified_pragmas=verified,
    )


def _require_finite(label: str, value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{label} must be a real number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{label} must be finite, got {value!r}")
    return result


def compute_optimal_busy_timeout_ms(
    arrival_rate_hz: float,
    mean_service_ms: float,
    service_cv: float,
    target_tail_prob: float = 1e-6,
) -> ContentionCalculusReceipt:
    """Size ``busy_timeout`` from a Kingman M/G/1 waiting-time tail bound.

    ``rho = lambda * E[S]`` must be strictly below one; at or above one Little's
    law says the backlog diverges and no timeout can rescue the writer.
    """

    lam = _require_finite("arrival_rate_hz", arrival_rate_hz)
    mean_ms = _require_finite("mean_service_ms", mean_service_ms)
    cv = _require_finite("service_cv", service_cv)
    eps = _require_finite("target_tail_prob", target_tail_prob)
    if lam < 0.0:
        raise ValueError("arrival_rate_hz must be non-negative")
    if mean_ms <= 0.0:
        raise ValueError("mean_service_ms must be strictly positive")
    if cv < 0.0:
        raise ValueError("service_cv must be non-negative")
    if not 0.0 < eps < 1.0:
        raise ValueError("target_tail_prob must lie strictly inside (0, 1)")

    mean_service_s = mean_ms / 1000.0
    rho = lam * mean_service_s
    if rho >= 1.0:
        raise QueueInstabilityError(f"utilization rho={rho!r} >= 1.0: the writer queue is unstable")

    mean_wait_ms = (rho / (1.0 - rho)) * ((1.0 + cv * cv) / 2.0) * mean_ms
    tail_multiplier = math.log(1.0 / eps)
    bound_ms = math.ceil(mean_ms + mean_wait_ms * tail_multiplier)
    return ContentionCalculusReceipt(
        utilization=rho,
        mean_wait_ms=mean_wait_ms,
        busy_timeout_ms=bound_ms,
        target_tail_prob=eps,
    )


def policy_for_contention(
    arrival_rate_hz: float,
    mean_service_ms: float,
    service_cv: float,
    target_tail_prob: float = 1e-6,
    *,
    synchronous: str = "FULL",
    allow_relaxed_durability: bool = False,
) -> tuple[SQLitePragmaPolicy, ContentionCalculusReceipt]:
    """Build a pragma policy whose ``busy_timeout`` is the computed tail bound."""

    receipt = compute_optimal_busy_timeout_ms(
        arrival_rate_hz, mean_service_ms, service_cv, target_tail_prob
    )
    policy = SQLitePragmaPolicy(
        busy_timeout_ms=receipt.busy_timeout_ms,
        synchronous=synchronous,
        allow_relaxed_durability=allow_relaxed_durability,
    )
    return policy, receipt
