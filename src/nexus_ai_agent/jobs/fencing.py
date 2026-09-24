"""Execution-ownership fencing — one shared abstraction for the job queue.

Unified root-cause layer for the Gate-5 repair (findings F2/F3/R5 and the
side-effect half of F1).  Research basis (recorded in ``docs/DECISION_LOG.md``
D-0016): Kleppmann's fencing-token rule — the fence value must be
*monotonically increasing* per resource and the storage side must reject any
write whose token is older than the highest it has accepted ("How to do
distributed locking", 2016); lease-fingerprint revalidation before every
durable flush (maritime-claims PR#455); stale-rescue rejection via
``(id, state, generation/horizon)`` predicates that update zero rows
(riverqueue/river PR#1373); stage-outcome writes fenced by current lease
ownership (mdc159/shizzle PR#42).

The model:

* An **execution generation** is born at every reservation
  (``PENDING → PROCESSING``): the row's ``attempt`` increments (the monotone
  fence) and a fresh ``owner_token`` UUID is minted (durable owner identity —
  the ``(job_id, attempt, owner_token)`` lease fingerprint).
* Every post-reservation transition carries the
  :class:`ExecutionToken` and is applied as a guarded UPDATE
  (``WHERE id=? AND attempt=? AND owner_token=? AND status IN (...)``).
  A stale worker's token matches zero rows: it can never move a newer
  execution's state, publish over it, or notify about it.
* A lease is the row's ``started_at`` plus :data:`DEFAULT_LEASE_TTL_SECONDS`.
  *Takeover* (startup recovery) is explicit and fenced: only rows whose lease
  expired are reclaimed, and reclaiming invalidates ``owner_token`` (set to
  NULL) so every in-flight write of the displaced holder fails its predicate.

UUIDs alone are deliberately *not* the fence (Kleppmann: an unordered UUID
cannot tell a storage side whether it is stale); the UUID here is the identity
half of the fingerprint, the ``attempt`` generation is the ordering half.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Final

#: Default lease a reserved execution may hold before startup recovery may
#: reclaim it.  Sized to cover the slowest verified lane (multi-minute
#: slideshow renders) while keeping crash takeover bounded; overridable per
#: queue instance for tests and deployments.
DEFAULT_LEASE_TTL_SECONDS: Final[float] = 300.0

#: Result-payload key under which a publication journals its swap state.
#: Written at swap time (crash-recovery journal) and retained in the
#: committed result as publication evidence.
PUBLICATION_JOURNAL_KEY: Final[str] = "_publication"


@dataclass(frozen=True)
class ExecutionToken:
    """Durable identity of one fenced execution generation.

    ``generation`` is the row's ``attempt`` as minted at reservation;
    ``owner_token`` is the fresh per-generation UUID.  Both are validated on
    every fenced UPDATE — matching zero rows is the stale-worker rejection.
    """

    job_id: str
    generation: int
    owner_token: str


def is_current(
    *,
    attempt: int,
    owner_token: str | None,
    token: ExecutionToken,
) -> bool:
    """True iff ``token`` still names the row's live execution generation."""
    return (
        owner_token is not None and owner_token == token.owner_token and attempt == token.generation
    )


def lease_cutoff(*, now: datetime, ttl_seconds: float) -> str:
    """ISO cutoff: rows with ``started_at`` older than this are reclaimable."""
    deadline = now - timedelta(seconds=ttl_seconds)
    return deadline.astimezone(timezone.utc).isoformat(timespec="microseconds")


__all__ = [
    "DEFAULT_LEASE_TTL_SECONDS",
    "PUBLICATION_JOURNAL_KEY",
    "ExecutionToken",
    "is_current",
    "lease_cutoff",
]
