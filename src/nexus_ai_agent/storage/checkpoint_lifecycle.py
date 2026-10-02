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

from nexus_ai_agent.domain.policies.retention import RESUMABILITY_WINDOW

#: Name of the policy implemented by :func:`eligible_for_deletion`.
#:
#: This is NOT the same policy as
#: :data:`~nexus_ai_agent.domain.policies.retention.POLICY_NAME`
#: (``resumability-evidence-v1``), and the two are deliberately kept distinct —
#: see the scope warning on :func:`eligible_for_deletion` for the measured
#: divergence and ``tests/unit/test_retention_policy_divergence.py`` for the
#: executable truth table that pins it.
ELIGIBILITY_POLICY_NAME = "storage-reclaim-v1"

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
    #: Defaults to the single product decision
    #: (:data:`~nexus_ai_agent.domain.retention.DEFAULT_RETENTION_DECISION`),
    #: re-exported as
    #: :data:`~nexus_ai_agent.domain.policies.retention.RESUMABILITY_WINDOW`.
    #: It is deliberately not a second literal ``timedelta(days=30)``: one
    #: product number, one place to change it.
    max_age: timedelta = RESUMABILITY_WINDOW
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

    Policy: :data:`ELIGIBILITY_POLICY_NAME` — *storage-reclaim eligibility*.
    Active threads are protected until ``active_until`` **plus a grace tail**
    (``active_grace``), and age is measured from ``created_at``.  Callers must
    still enforce referential safety for blobs and descendants in their
    database adapter.

    Scope warning — a second, different retention policy exists in
    :mod:`nexus_ai_agent.domain.policies.retention`
    (:data:`~nexus_ai_agent.domain.policies.retention.POLICY_NAME`,
    *resumability evidence*, via :func:`~nexus_ai_agent.domain.policies.retention.deletable`).
    The two are **not one concept** and must not be merged.  Measured over a
    120-cell sweep of (created age x access age x pin state) they agree on 87
    cells and disagree on 33, all of them on exactly two independent axes:

    ==========================================  ==========  ==========  =========
    state                                       deletable   eligible    cells
    ==========================================  ==========  ==========  =========
    pin expired 1d ago (inside the grace tail)  True        **False**   9
    created >=30d ago, never accessed           **False**   True        6
    created <30d ago, accessed >=30d ago        True        **False**   18 (*)
    ==========================================  ==========  ==========  =========

    (*) unreachable in a real index — a checkpoint cannot be accessed before it
    exists — but real at function level, so it is pinned too.

    * **axis 1 — post-pin grace.**  This policy keeps protecting a record for
      ``active_grace`` *after* its pin lapses; the resumability policy has no
      such concept and treats a lapsed pin as no protection at all.
    * **axis 2 — which timestamp is authoritative for age.**  This policy ages
      from ``created_at`` (NOT NULL in both stores, so always available) and
      reads an absent ``last_accessed_at`` as "never accessed, therefore not
      recent".  The resumability policy ages from ``last_accessed_at`` *only*
      and ignores ``created_at`` entirely, so an absent access stamp is
      *unknown* and the record is retained.  The axis therefore cuts both
      ways: missing evidence retains under one and reclaims under the other,
      and a young record with an old access stamp does the reverse.

    Neither axis is a bug; each is a deliberate policy choice, and unifying
    them would mean deleting one of the two.  This predicate is currently
    **dead in ``src/``** — nothing in the runtime calls it — so there is no
    production pressure to unify and no evidence that either policy is the
    intended one.  ``tests/unit/test_retention_policy_divergence.py`` pins the
    full sweep, so a future change to either side is a visible, deliberate
    decision rather than silent drift.

    A decision about a whole **thread** goes through neither predicate: use
    :func:`~nexus_ai_agent.domain.policies.retention.thread_retention`, which
    quantifies over every row the thread owns.
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
