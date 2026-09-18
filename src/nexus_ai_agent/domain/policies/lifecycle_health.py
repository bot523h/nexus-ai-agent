"""Pure lifecycle health gate — no I/O, no framework, no storage imports.

The runtime counts its own best-effort lifecycle operations; once the failure
rate exceeds the threshold the lifecycle is latched off for the process
lifetime (fail-safe: it never re-enables itself).
"""

from __future__ import annotations

from typing import Final

# Failure rate strictly above 10% disables lifecycle writes.
HEALTH_GATE_THRESHOLD: Final[float] = 0.10


def health_gate_ok(*, failed: int, total: int, threshold: float = HEALTH_GATE_THRESHOLD) -> bool:
    """Return True while the lifecycle may keep writing.

    ``total == 0`` means nothing has been measured yet — writes stay allowed.
    A rate exactly at the threshold is still allowed; strictly above is not.
    """
    if total <= 0:
        return True
    return (failed / total) <= threshold
