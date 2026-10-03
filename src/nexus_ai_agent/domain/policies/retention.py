"""Pure retention predicates and the resumability product contract.

Three levels of decision live here, each built on the one below it, with no
level re-implementing another:

* **row level** -- :func:`row_reason` is the *single* implementation of the
  row-level decision under :data:`POLICY_NAME`; it returns the evidence
  (:class:`RetentionReason`).  :func:`deletable` is only its boolean
  projection, and :func:`pinned` is the pin condition it consults.
* **thread level** -- :func:`thread_retention` quantifies over *every* row a
  thread owns.  This is the safety-critical level.
* **product level** -- the window and the expired-resume behaviour are
  re-exported from :mod:`nexus_ai_agent.domain.retention`, never re-spelled.

The thread level's quantifier asymmetry **is** the safety property:

===================  ==================  =================================
Concept              Quantifier         Why
===================  ==================  =================================
protection           EXISTENTIAL        one pinned row is enough to protect
                                        the thread it belongs to
destruction          UNIVERSAL          every row must be independently
                                        deletable before the thread may go
no rows at all       UNKNOWN            unknown is retained (fail-closed)
===================  ==================  =================================

A thread verdict must never be taken from a single row.  The lifecycle index
is read without ``ORDER BY`` (both the SQLite and the PostgreSQL store), and
the SQLite index is a rowid table, so "the last row" is insertion order -- a
storage-engine artefact, not a fact about the thread.  Deriving a destructive
recommendation from it made the answer depend on the order rows happened to
be written in; see ``tests/unit/test_thread_retention_verdict.py``.

A destructive verdict is never bare: :attr:`ThreadRetention.reason` carries
the evidence, and ``reason is NONE`` is equivalent to ``deletable`` *by
construction* rather than by coincidence.

This module is one of **two** retention policies in the repository.  The other
is :data:`~nexus_ai_agent.storage.checkpoint_lifecycle.ELIGIBILITY_POLICY_NAME`
in :mod:`nexus_ai_agent.storage.checkpoint_lifecycle`; they are deliberately
distinct and their divergence is pinned by
``tests/unit/test_retention_policy_divergence.py``.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Final

from nexus_ai_agent.domain.retention import DEFAULT_RETENTION_DECISION


class JournalStatus(str, Enum):
    """String-valued journal status, spelled without 3.11+ syntax.

    Same recipe as ``EntityType`` in ``domain/glossary.py``: the
    ``(str, Enum)`` mixin plus ``__str__``/``__format__`` prints the value
    on every supported interpreter (3.10–3.12+).
    """

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    RETRYING = "retrying"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"

    def __str__(self) -> str:
        return str(self.value)

    def __format__(self, spec: str) -> str:
        return format(str(self.value), spec)


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


#: The resumability window and the expired-resume behaviour are PRODUCT
#: decisions, recorded once in :mod:`nexus_ai_agent.domain.retention`.  They
#: are re-exported here (never re-spelled) so a change to the product
#: decision cannot silently leave a second, divergent copy behind.
RESUMABILITY_WINDOW: Final[timedelta] = DEFAULT_RETENTION_DECISION.resumability_window
FORK_AFTER_RESUMABILITY: Final[str] = DEFAULT_RETENTION_DECISION.expired_resume_behavior
RETRY_BACKOFF: Final[str] = "exponential_backoff_on_failed_to_retrying"

#: Name of the policy implemented by :func:`deletable` / :func:`thread_retention`.
#:
#: There is a second, *different* retention policy in the repository:
#: ``storage.checkpoint_lifecycle.eligible_for_deletion``
#: (:data:`~nexus_ai_agent.storage.checkpoint_lifecycle.ELIGIBILITY_POLICY_NAME`).
#: The two are deliberately NOT one concept and must not be merged; they
#: diverge on two measured axes (post-pin grace, and which timestamp counts as
#: age evidence).  ``tests/unit/test_retention_policy_divergence.py`` pins the
#: whole truth table so the divergence stays a decision instead of drifting
#: into a bug.  The names exist so a caller can never pick one by accident:
#: two predicates called "deletable" and "eligible_for_deletion" are
#: indistinguishable at a call site, and only one of them is fail-closed on
#: missing access evidence.
POLICY_NAME: Final[str] = "resumability-evidence-v1"


class RetentionReason(str, Enum):
    """Why a thread is being retained — the evidence behind ``deletable``.

    A destructive verdict with no reason is not actionable: ``would_delete:
    false`` tells an operator nothing about whether the protection is an
    explicit pin, ordinary recency, or the absence of evidence.  These four
    values are the complete set of ways :func:`deletable` can return
    ``False``, and they are mutually distinguishable:

    * ``NONE`` -- nothing protects the thread; it *is* deletable.  This is
      the only value that co-occurs with ``deletable=True``.
    * ``PINNED`` -- at least one row's ``active_until`` is still in the
      future.  Explicit, intentional protection.
    * ``RECENT_ACCESS`` -- at least one row was accessed inside the
      resumability window.  Incidental protection.
    * ``NO_EVIDENCE`` -- the thread has no lifecycle rows, or a row carries no
      access stamp.  The thread is retained because its age is *unknown*, not
      because anything is known about it.  Never a claim of safety.

    Spelled with the ``(str, Enum)`` mixin so the value prints on every
    supported interpreter (same recipe as :class:`JournalStatus`).
    """

    NONE = "none"
    PINNED = "pinned"
    RECENT_ACCESS = "recent_access"
    NO_EVIDENCE = "no_evidence"

    def __str__(self) -> str:
        return str(self.value)

    def __format__(self, spec: str) -> str:
        return format(str(self.value), spec)


def _utc(value: datetime) -> datetime:
    return (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )


def pinned(record: RetentionRecord, *, now: datetime) -> bool:
    current = _utc(now)
    return record.active_until is not None and current < _utc(record.active_until)


def row_reason(record: RetentionRecord, *, now: datetime) -> RetentionReason:
    """The row-level retention decision, with its evidence.

    This is the *single* implementation of the row-level decision under
    :data:`POLICY_NAME`; :func:`deletable` is only its boolean projection.
    There is deliberately no second copy of these three conditions anywhere —
    an explanation that could disagree with the decision it explains is a
    shadow predicate, and a shadow predicate eventually wins.

    The three ways a row can be retained, in reporting precedence:

    * an ``active_until`` still in the future -> ``PINNED``;
    * no access stamp at all -> ``NO_EVIDENCE`` (age unknown, so retained —
      never inferred safe);
    * accessed inside the resumability window -> ``RECENT_ACCESS``.

    ``NONE`` means none of the three holds, i.e. the row is deletable.  The
    precedence order is the *reporting* order only: the decision is "does any
    condition hold", which is order-insensitive.
    """
    current = _utc(now)
    if pinned(record, now=current):
        return RetentionReason.PINNED
    if record.last_accessed_at is None:
        return RetentionReason.NO_EVIDENCE
    if current - _utc(record.last_accessed_at) < RESUMABILITY_WINDOW:
        return RetentionReason.RECENT_ACCESS
    return RetentionReason.NONE


def deletable(record: RetentionRecord, *, now: datetime) -> bool:
    """Pure fail-safe predicate; unknown access/active state is retained.

    The boolean projection of :func:`row_reason` — the same decision, not a
    parallel one, so ``deletable(r)`` is ``row_reason(r) is NONE`` *by
    construction* rather than by a test that happens to pass today.
    """
    return row_reason(record, now=now) is RetentionReason.NONE


@dataclass(frozen=True)
class ThreadRetention:
    """One thread's retention verdict, quantified over every row it owns.

    ``active`` is deliberately an alias of ``pinned``, not a second
    predicate: the ``inspect-v1`` output carries both field names for one
    concept (``active_until`` still in the future), and two spellings of one
    predicate are how they drift apart.  Callers may use either name; they
    cannot disagree.
    """

    #: How many lifecycle rows this verdict was measured over. ``0`` means
    #: the thread has no metadata, which is *unknown*, not *old*.
    rows: int
    #: EXISTENTIAL: at least one row is still inside its ``active_until``.
    pinned: bool
    #: UNIVERSAL: at least one row exists and every row is deletable.
    deletable: bool
    #: The evidence behind :attr:`deletable`.  ``NONE`` is the only value that
    #: co-occurs with ``deletable=True``; that biconditional is the invariant
    #: ``tests/unit/test_thread_retention_verdict.py`` pins.
    reason: RetentionReason = RetentionReason.NONE

    @property
    def known(self) -> bool:
        """False when the thread has no lifecycle rows (verdict is unknown)."""
        return self.rows > 0

    @property
    def active(self) -> bool:
        """Alias of :attr:`pinned` — one concept, one implementation."""
        return self.pinned


#: Precedence when several rows veto for different reasons.  An explicit pin
#: outranks incidental recency, which outranks "we have no evidence": report
#: the strongest *affirmative* protection available, and only fall back to
#: ``NO_EVIDENCE`` when absence of evidence is the only reason the thread
#: survived.  Order-independent — it is a max over a total order, not a scan.
_REASON_PRECEDENCE: Final[tuple[RetentionReason, ...]] = (
    RetentionReason.PINNED,
    RetentionReason.RECENT_ACCESS,
    RetentionReason.NO_EVIDENCE,
)


def thread_retention(records: Iterable[RetentionRecord], *, now: datetime) -> ThreadRetention:
    """The canonical thread-scoped retention verdict.

    This is the only correct way to answer "may this thread be deleted?".
    A per-row predicate answers a different, narrower question, and using
    one row's answer for the whole thread is a fail-open: the row happens to
    be picked by storage-engine row order, so the same logical thread can
    flip between "protected" and "delete it" when rows are rewritten in a
    different order.

    Fail-closed on every uncertain input:

    * no rows -> ``deletable=False`` (``all([])`` is ``True``, so the empty
      case is guarded explicitly; absence of metadata is never evidence
      of age) and ``reason=NO_EVIDENCE``;
    * a row that was never accessed -> :func:`deletable` already retains it,
      which vetoes the thread through the universal quantifier;
    * naive timestamps are normalised to UTC by the row predicates before
      any comparison, so a mixed-awareness index cannot mis-compare.

    Pure and order-independent: the result is a function of the multiset of
    rows, never of their sequence, and the input is not mutated.
    """
    measured: Sequence[RetentionRecord] = tuple(records)
    current = _utc(now)
    row_reasons = {row_reason(record, now=current) for record in measured}
    if not measured:
        reason = RetentionReason.NO_EVIDENCE
    else:
        reason = next(
            (candidate for candidate in _REASON_PRECEDENCE if candidate in row_reasons),
            RetentionReason.NONE,
        )
    return ThreadRetention(
        rows=len(measured),
        pinned=any(pinned(record, now=current) for record in measured),
        deletable=bool(measured) and all(deletable(record, now=current) for record in measured),
        reason=reason,
    )


__all__ = [
    "ALLOWED_TRANSITIONS",
    "FORK_AFTER_RESUMABILITY",
    "POLICY_NAME",
    "RESUMABILITY_WINDOW",
    "RETRY_BACKOFF",
    "JournalStatus",
    "RetentionReason",
    "RetentionRecord",
    "ThreadRetention",
    "deletable",
    "pinned",
    "row_reason",
    "thread_retention",
]
