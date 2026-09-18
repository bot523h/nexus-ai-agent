"""Pure reconciler purge policy — no I/O, no framework, no storage imports.

All thresholds are measured within a single reconciler execution
(``rate = anomalies / rows_scanned``).  Every predicate fails safe: an
uncertain input never yields "purge allowed".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

# An anomaly rate strictly above 1% blocks all purging for the execution.
PURGE_MAX_ANOMALY_RATE: Final[float] = 0.01
# Even a clean rate blocks purging once this many anomalies were observed.
PURGE_MAX_ANOMALIES_ABSOLUTE: Final[int] = 500
# A record at most 24 hours old is never purged.
MIN_PURGE_AGE: Final[timedelta] = timedelta(hours=24)
# An orphaned row is blocked from ``<reference> + 24h``.
ORPHAN_BLOCK_PERIOD: Final[timedelta] = timedelta(hours=24)


def anomaly_rate(*, rows_scanned: int, anomalies: int) -> float:
    """Return ``anomalies / rows_scanned`` for one execution (0.0 when nothing scanned)."""
    if rows_scanned <= 0:
        return 0.0
    return anomalies / rows_scanned


def orphan_block_until(reference: datetime | None) -> datetime | None:
    """``reference + 24h``; ``None`` reference fails safe (never purge)."""
    if reference is None:
        return None
    return reference + ORPHAN_BLOCK_PERIOD


@dataclass(frozen=True)
class PurgeDecision:
    allowed: bool
    reasons: tuple[str, ...]


def purge_allowed(
    *,
    rows_scanned: int,
    anomalies: int,
    record_age: timedelta,
    blocked_until: datetime | None,
    now: datetime,
) -> PurgeDecision:
    """Pure, fail-safe predicate for purging one lifecycle-metadata row.

    Purging is allowed only when the whole-execution anomaly guards pass AND
    the individual record is old enough AND its orphan block has elapsed.
    Unknown block state (``blocked_until is None``) blocks the purge.
    """
    reasons: list[str] = []
    rate = anomaly_rate(rows_scanned=rows_scanned, anomalies=anomalies)
    if rate > PURGE_MAX_ANOMALY_RATE:
        reasons.append(f"anomaly_rate {rate:.6f} > {PURGE_MAX_ANOMALY_RATE}")
    if anomalies > PURGE_MAX_ANOMALIES_ABSOLUTE:
        reasons.append(f"anomalies {anomalies} > {PURGE_MAX_ANOMALIES_ABSOLUTE}")
    if record_age <= MIN_PURGE_AGE:
        reasons.append(f"age {record_age} <= {MIN_PURGE_AGE}")
    if blocked_until is None:
        reasons.append("blocked_until unknown (fail-safe)")
    elif now < blocked_until:
        reasons.append(f"blocked until {blocked_until.isoformat()}")
    return PurgeDecision(allowed=not reasons, reasons=tuple(reasons))
