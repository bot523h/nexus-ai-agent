"""Pure outbox state machine + retry/recovery policy (P3) — framework-free zone.

The module is the *decision layer* for effect delivery. It deliberately holds
no I/O: no database, no clock reads, no network. Time is an explicit argument,
so every crash-window and lease decision is deterministic and testable without
sleeping (mutation and concurrency tests depend on that).

Four states, no dead-letter (D+P4-6): a dead-letter state without an evidence
budget invites a permanently-undiscoverable quarantine. The two failure shapes
are told apart by *classification* instead:

* ``FAILED_RETRYABLE``  — transient (timeouts, 5xx, dependency blips): the
  policy schedules a bounded retry with jitter.
* ``FAILED_PERMANENT``  — a ``retryable=False`` outcome: handed to the
  reconciliation contract immediately, and to an operator in production.

The full rule set (allowed transitions, invalid transitions, retry semantics,
ownership and lease) is pinned by ``tests/unit/test_outbox_policy.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

# ---------------------------------------------------------------------------
# Bounded knobs (deterministic defaults; a caller may tighten, never loosen).
# ---------------------------------------------------------------------------
#: A claimed effect whose lease has not been heartbeated for this long is stale.
CLAIM_LEASE_SECONDS: Final[int] = 60

#: An old PENDING record is a reconciliation candidate after this long.
PENDING_STALE_AGE_SECONDS: Final[int] = 300

#: Retry cap (evidence-based; no infinite retry storm).
MAX_ATTEMPTS: Final[int] = 5

#: Retry delay bounds — ``min(2**attempt, MAX_BACKOFF) # + BUILD_JITTER_SECONDS``.
BASE_BACKOFF_SECONDS: Final[int] = 1
MAX_BACKOFF_SECONDS: Final[int] = 60
BUILD_MAX_JITTER_SECONDS: Final[int] = 5

#: Dead-letter budget decision surface (D+P4-6): a mandatory gate if a
#: FAILED_PERMANENT effect (or an exhausted retry budget) needs a quarantine state.
DEAD_LETTER_TOGGLE_DEFAULT: Final[bool] = False


class EffectStatus(StrEnum):
    PENDING = "pending"
    CLAIMED = "claimed"
    SUCCEEDED = "succeeded"
    FAILED_RETRYABLE = "failed_retryable"
    FAILED_PERMANENT = "failed_permanent"


#: Allowed transitions from each state (deterministic, exhaustive).
ALLOWED_TRANSITIONS: Final[dict[EffectStatus, frozenset[EffectStatus]]] = {
    EffectStatus.PENDING: frozenset({EffectStatus.CLAIMED}),
    EffectStatus.CLAIMED: frozenset(
        {EffectStatus.SUCCEEDED, EffectStatus.FAILED_RETRYABLE, EffectStatus.FAILED_PERMANENT}
    ),
    EffectStatus.FAILED_RETRYABLE: frozenset({EffectStatus.PENDING}),
    EffectStatus.SUCCEEDED: frozenset(),
    EffectStatus.FAILED_PERMANENT: frozenset(),
}

#: Terminal states.
TERMINAL_STATUSES: Final[frozenset[EffectStatus]] = frozenset(
    {EffectStatus.SUCCEEDED, EffectStatus.FAILED_PERMANENT}
)


def is_terminal(status: EffectStatus) -> bool:
    return status in TERMINAL_STATUSES


@dataclass(frozen=True)
class RetryDecision:
    retryable: bool
    backoff_seconds: int
    reason: str


def classify_failure(*, retryable: bool, attempt: int) -> tuple[EffectStatus, RetryDecision]:
    """Classify a delivery failure into retryable or permanent.

    The *retryability* of an underlying exception is decided at the adapter
    boundary (where exception types live — see :class:`attempt.error`), never
    in this pure policy: error *messages* must not leak into policy decisions.
    The policy only enforces the two budgets:

    * ``retryable=False`` — a definitive rejection (e.g. Telegram "chat not
      found"): permanent on first sight, no noisy retries.
    * ``retryable=True`` — transient stay retryable while ``attempt <
      MAX_ATTEMPTS``; past the budget they become permanent (evidence-based
      cap, no infinite retry storm).
    """
    if not retryable:
        return EffectStatus.FAILED_PERMANENT, RetryDecision(False, 0, "non_retryable_failure")
    if attempt >= MAX_ATTEMPTS:
        return EffectStatus.FAILED_PERMANENT, RetryDecision(False, 0, "max_attempts_exhausted")
    backoff = min(BASE_BACKOFF_SECONDS * (2 ** max(attempt - 1, 0)), MAX_BACKOFF_SECONDS)
    return EffectStatus.FAILED_RETRYABLE, RetryDecision(True, backoff, "transient_failure")


def next_retry_at(*, attempt: int, now: int, jitter: int = 0) -> int:
    """Monotonic-seconds timestamp for the next retry (bounded exponential + jitter).

    ``now`` and the result are epoch-seconds; ``jitter`` must already be a
    deterministic value in ``[0, BUILD_MAX_JITTER_SECONDS]`` (the *dispatcher*
    derives it from the effect key lazily; the policy stays pure).
    """
    clamped = max(0, min(jitter, BUILD_MAX_JITTER_SECONDS))
    backoff = min(BASE_BACKOFF_SECONDS * (2 ** max(attempt - 1, 0)), MAX_BACKOFF_SECONDS)
    return now + backoff + clamped


@dataclass(frozen=True)
class Snapshot:
    """The full decision surface the dispatcher needs for one row (C1-C5)."""

    sequence: int
    effect_key: str
    status: EffectStatus
    attempt: int
    claimed_by: str | None
    lease_until: int
    not_before: int
    next_retry_at: int
    created_at: int


def _stale(lease_until: int, now: int) -> bool:
    return now >= lease_until


def classify_row(
    snapshot: Snapshot,
    *,
    now: int,
    dispatcher_id: str,
) -> str:
    """Deterministic next-action decision for one outbox row (reconciliation core).

    This one function embodies the crash matrix (C1-C5) in pure form:

    * ``claim``    — PENDING and due, or CLAIMED whose lease is stale (C1/C5)
                     or FAILED_RETRYABLE past its backoff: take it over.
    * ``dedupe``   — SUCCEEDED/FAILED_PERMANENT: terminal, never re-dispatched
                     (the adapter still returns ``dedupe`` so re-submitting the
                     same intent is *recognised*, not silently re-run).
    * ``wait``     — PENDING not yet due, CLAIMED with a live lease owned by
                     someone else, or FAILED_RETRYABLE still backing off.
    * ``unknown``  — anything the machine cannot classify: a terminal row that
                     carriers a non-empty lease owner (C4 ambiguity leaking
                     into state) — surface it, never guess.
    """
    if is_terminal(snapshot.status):
        # ``claimed_by`` on a terminal row is a C4 residue: the delivery outcome
        # and the delivery record disagree — that must be visible, not laundered.
        return "unknown" if snapshot.claimed_by else "dedupe"
    if snapshot.status is EffectStatus.CLAIMED:
        if snapshot.claimed_by == dispatcher_id or _stale(snapshot.lease_until, now):
            return "claim"
        return "wait"
    if snapshot.status is EffectStatus.PENDING:
        return "claim" if now >= snapshot.not_before else "wait"
    if snapshot.status is EffectStatus.FAILED_RETRYABLE:
        return "claim" if now >= snapshot.next_retry_at else "wait"
    return "unknown"


def recovered_rows(rows: list[Snapshot], *, now: int, dispatcher_id: str) -> list[Snapshot]:
    """Return the rows the reconciler must take over right now, oldest first.

    Order is deterministic (ascending ``sequence``) so two reconcilers sweep
    the same backlog in the same order — the *claim* itself still arbitrates.
    """
    candidates = [
        row for row in rows if classify_row(row, now=now, dispatcher_id=dispatcher_id) == "claim"
    ]
    return sorted(candidates, key=lambda row: row.sequence)


def would_dead_letter(status: EffectStatus, *, dead_letter_enabled: bool) -> bool:
    """The only place a DEAD_LETTER state may ever be justified (D+P4-6).

    Returns ``True`` only when the operator has explicitly enabled the toggle
    AND the effect is permanently failed. Default policy: no dead-letter —
    FAILED_PERMANENT rows stay in the outbox, visible, awaiting the
    reconciliation contract or an operator command.
    """
    return bool(dead_letter_enabled) and status is EffectStatus.FAILED_PERMANENT
