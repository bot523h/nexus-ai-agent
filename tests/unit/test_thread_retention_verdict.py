"""The thread-scoped retention verdict: one thread, one decision.

Why this module exists
----------------------
A checkpoint thread owns *many* rows in the lifecycle index, but the
``checkpoints inspect`` retention verdict (``pinned`` / ``active`` /
``resumable_within_window`` / ``would_delete``) used to be computed from
**one** of them -- ``metadata[-1]``, the last row of a ``SELECT`` that has
no ``ORDER BY``.

That made a destructive recommendation depend on a storage-engine artefact
(rowid / insertion order) instead of on the thread.  Measured before the
fix, with logically identical data (one row pinned until +7d and accessed
1 day ago, one row 40 days stale) and only the physical write order
changed::

    stale row written SECOND -> SELECT order ['cp_live', 'cp_old']
        pinned=False  active=False  would_delete=True     <-- a live thread
    stale row written FIRST  -> SELECT order ['cp_old', 'cp_live']
        pinned=True   active=True   would_delete=False

The invariant that removes the whole failure class is a quantifier
asymmetry, and it is the point of this suite:

    protection is EXISTENTIAL   pinned(thread)   = ANY row is pinned
    destruction is UNIVERSAL    deletable(thread) = EVERY row is deletable
    zero rows is UNKNOWN        unknown is retained (fail-closed)

Every test here is a negative-space test: it asserts that the *wrong*
answer is refused, not merely that a happy path works.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from itertools import permutations

import pytest

from nexus_ai_agent.domain.policies.retention import (
    RetentionReason,
    RetentionRecord,
    deletable,
    pinned,
    row_reason,
    thread_retention,
)

#: Anchored on the real clock: the verdict compares against ``now`` supplied
#: by the caller, so the suite stays green whenever it runs.
NOW = datetime.now(timezone.utc)


def live_row() -> RetentionRecord:
    """Accessed 1 day ago and pinned until +7d: protected on both counts."""
    return RetentionRecord(
        created_at=NOW - timedelta(days=1),
        last_accessed_at=NOW - timedelta(days=1),
        active_until=NOW + timedelta(days=7),
    )


def stale_row() -> RetentionRecord:
    """40 days old, untouched since, not pinned: individually deletable."""
    return RetentionRecord(
        created_at=NOW - timedelta(days=40),
        last_accessed_at=NOW - timedelta(days=40),
    )


def unknown_row() -> RetentionRecord:
    """Age unknown (never accessed): the row cannot be aged out safely."""
    return RetentionRecord(created_at=NOW - timedelta(days=40))


# ── the two quantifiers ───────────────────────────────────────────────────


def test_protection_is_existential_one_pinned_row_protects_the_thread() -> None:
    """One pinned row out of many is enough: ``pinned`` is ANY, not LAST."""
    verdict = thread_retention([stale_row(), live_row()], now=NOW)

    assert verdict.rows == 2
    assert verdict.pinned is True
    assert verdict.active is True
    # The single live row must veto destruction of the whole thread.
    assert verdict.deletable is False


def test_destruction_is_universal_every_row_must_be_deletable() -> None:
    """``deletable`` is ALL: a thread of only-stale rows, and nothing else."""
    assert thread_retention([stale_row()], now=NOW).deletable is True
    assert thread_retention([stale_row(), stale_row()], now=NOW).deletable is True

    # One non-deletable sibling vetoes the thread, whichever end it sits at.
    assert thread_retention([stale_row(), live_row()], now=NOW).deletable is False
    assert thread_retention([live_row(), stale_row()], now=NOW).deletable is False


def test_an_all_stale_thread_is_still_not_pinned() -> None:
    """Existential protection must not fire when nothing is actually pinned."""
    verdict = thread_retention([stale_row(), stale_row()], now=NOW)

    assert verdict.pinned is False
    assert verdict.active is False
    assert verdict.deletable is True


# ── fail-closed on unknown ────────────────────────────────────────────────


def test_zero_rows_is_unknown_and_never_deletable() -> None:
    """No metadata is not evidence of age; it must never read as deletable."""
    verdict = thread_retention([], now=NOW)

    assert verdict.rows == 0
    assert verdict.deletable is False
    assert verdict.pinned is False
    assert verdict.active is False
    assert verdict.known is False


def test_unknown_access_state_blocks_deletion_of_the_whole_thread() -> None:
    """A row that was never accessed cannot be aged out, so the thread can't."""
    assert deletable(unknown_row(), now=NOW) is False
    assert thread_retention([stale_row(), unknown_row()], now=NOW).deletable is False


def test_a_pin_that_already_expired_does_not_protect() -> None:
    """``active_until`` in the past is not a pin (fail-safe in both directions)."""
    expired = RetentionRecord(
        created_at=NOW - timedelta(days=40),
        last_accessed_at=NOW - timedelta(days=40),
        active_until=NOW - timedelta(days=1),
    )
    assert pinned(expired, now=NOW) is False

    verdict = thread_retention([expired], now=NOW)
    assert verdict.pinned is False
    assert verdict.deletable is True


# ── the property that the defect violated ─────────────────────────────────


def test_the_verdict_is_a_function_of_the_rows_not_of_their_order() -> None:
    """Every permutation of the same rows yields the identical verdict.

    This is the regression test for the ``metadata[-1]`` defect: the
    lifecycle index is read without ``ORDER BY``, so any verdict that
    depends on row position is non-deterministic by construction.
    """
    rows = [live_row(), stale_row(), unknown_row(), stale_row()]
    verdicts = {thread_retention(list(order), now=NOW) for order in permutations(rows)}

    assert len(verdicts) == 1, f"verdict depends on row order: {verdicts}"
    only = verdicts.pop()
    assert only.rows == 4
    assert only.pinned is True  # the live row protects
    assert only.deletable is False  # the live and the unknown row veto


def test_verdict_is_deterministic_under_repeated_evaluation() -> None:
    """Same rows, same clock -> same verdict, every time."""
    rows = [stale_row(), live_row()]
    first = thread_retention(rows, now=NOW)
    for _ in range(25):
        assert thread_retention(rows, now=NOW) == first


def test_the_input_sequence_is_not_mutated() -> None:
    """The verdict is a pure read; the caller's list must be untouched."""
    rows = [live_row(), stale_row()]
    snapshot = list(rows)

    thread_retention(rows, now=NOW)

    assert rows == snapshot


def test_naive_timestamps_are_normalised_before_comparison() -> None:
    """A naive ``active_until`` must not raise or silently mis-compare."""
    naive_pin = RetentionRecord(
        created_at=NOW - timedelta(days=1),
        last_accessed_at=NOW - timedelta(days=1),
        active_until=(NOW + timedelta(days=7)).replace(tzinfo=None),
    )

    verdict = thread_retention([naive_pin, stale_row()], now=NOW)

    assert verdict.pinned is True
    assert verdict.deletable is False


def test_rows_count_is_the_number_of_rows_examined() -> None:
    """``rows`` is the evidence size, so a caller can tell 0 from "unknown"."""
    for size in (0, 1, 2, 5):
        assert thread_retention([stale_row()] * size, now=NOW).rows == size


def test_thread_retention_is_exported() -> None:
    """The primitive is part of the policy module's public surface."""
    from nexus_ai_agent.domain.policies import retention as module

    for name in (
        "thread_retention",
        "ThreadRetention",
        "RetentionReason",
        "row_reason",
        "POLICY_NAME",
        "pinned",
        "deletable",
    ):
        assert name in module.__all__, name


# ── one product number, one place ─────────────────────────────────────────


def test_the_resumability_window_has_exactly_one_source() -> None:
    """The 30-day window is re-exported, never re-spelled.

    Three modules used to carry their own ``timedelta(days=30)``.  Identity
    (not equality) is asserted on purpose: a second literal with the same
    value is exactly the drift this guard exists to catch, and it must fail
    even while the numbers still happen to agree.
    """
    from nexus_ai_agent.domain.policies.retention import (
        FORK_AFTER_RESUMABILITY,
        RESUMABILITY_WINDOW,
    )
    from nexus_ai_agent.domain.retention import DEFAULT_RETENTION_DECISION
    from nexus_ai_agent.storage.checkpoint_lifecycle import RetentionPolicy

    product = DEFAULT_RETENTION_DECISION.resumability_window
    assert RESUMABILITY_WINDOW is product
    assert RetentionPolicy().max_age is product
    assert FORK_AFTER_RESUMABILITY == DEFAULT_RETENTION_DECISION.expired_resume_behavior


# ── the reason behind a destructive verdict ───────────────────────────────
#
# ``would_delete: false`` on its own is not actionable: an operator cannot tell
# an explicit pin from ordinary recency, or either of those from "we simply
# have no evidence".  ``RetentionReason`` names the evidence, and the invariant
# that binds it to the verdict is a strict biconditional:
#
#     reason is NONE  <=>  deletable is True
#
# ``no_evidence`` in particular is never a claim of safety.


def test_reason_is_none_exactly_when_the_thread_is_deletable() -> None:
    """The biconditional: no reason iff deletable, in both directions."""
    deletable_cases = ([stale_row()], [stale_row(), stale_row()])
    retained_cases = (
        [live_row()],  # pinned + recent
        [stale_row(), live_row()],  # one vetoing row
        [unknown_row()],  # no evidence at all
        [stale_row(), unknown_row()],
        [],  # no rows at all
    )

    for rows in deletable_cases:
        verdict = thread_retention(rows, now=NOW)
        assert verdict.deletable is True
        assert verdict.reason is RetentionReason.NONE

    for rows in retained_cases:
        verdict = thread_retention(rows, now=NOW)
        assert verdict.deletable is False
        assert verdict.reason is not RetentionReason.NONE


def test_reason_distinguishes_pinned_from_recent_from_unknown() -> None:
    """The three protection states that used to share one boolean are now named."""
    pinned_only = RetentionRecord(
        created_at=NOW - timedelta(days=40),
        last_accessed_at=NOW - timedelta(days=40),
        active_until=NOW + timedelta(days=7),
    )
    assert thread_retention([pinned_only], now=NOW).reason is RetentionReason.PINNED

    recent_only = RetentionRecord(
        created_at=NOW - timedelta(days=2),
        last_accessed_at=NOW - timedelta(days=1),
    )
    assert thread_retention([recent_only], now=NOW).reason is RetentionReason.RECENT_ACCESS

    assert thread_retention([unknown_row()], now=NOW).reason is RetentionReason.NO_EVIDENCE
    assert thread_retention([], now=NOW).reason is RetentionReason.NO_EVIDENCE


def test_an_explicit_pin_outranks_incidental_recency() -> None:
    """Precedence reports the strongest affirmative protection, not the first row."""
    recent = RetentionRecord(
        created_at=NOW - timedelta(days=2), last_accessed_at=NOW - timedelta(days=1)
    )
    pin = RetentionRecord(
        created_at=NOW - timedelta(days=40),
        last_accessed_at=NOW - timedelta(days=40),
        active_until=NOW + timedelta(days=7),
    )

    # Both orders: the pin wins, and the answer does not depend on position.
    assert thread_retention([recent, pin], now=NOW).reason is RetentionReason.PINNED
    assert thread_retention([pin, recent], now=NOW).reason is RetentionReason.PINNED


def test_affirmative_evidence_outranks_absence_of_evidence() -> None:
    """``no_evidence`` is the fallback, reported only when it is the whole story."""
    recent = RetentionRecord(
        created_at=NOW - timedelta(days=2), last_accessed_at=NOW - timedelta(days=1)
    )

    verdict = thread_retention([recent, unknown_row()], now=NOW)

    assert verdict.deletable is False  # the unknown row still vetoes
    assert verdict.reason is RetentionReason.RECENT_ACCESS


def test_row_reason_can_explain_but_never_contradict_deletable() -> None:
    """The reason is derived from the same predicates, over a full input sweep.

    If ``row_reason`` ever drifts away from ``deletable`` — the "shadow
    predicate" failure — this fails.  It is a property test, not a happy path.
    """
    ages: list[float | None] = [None, 0, 1, 29, 30, 31, 60]
    pins: list[float | None] = [None, -10, -1, 0, 7]
    checked = 0
    for accessed in ages:
        for pin in pins:
            record = RetentionRecord(
                created_at=NOW - timedelta(days=60),
                last_accessed_at=None if accessed is None else NOW - timedelta(days=accessed),
                active_until=None if pin is None else NOW + timedelta(days=pin),
            )
            explained = row_reason(record, now=NOW)
            assert (explained is RetentionReason.NONE) is deletable(record, now=NOW), (
                f"row_reason contradicts deletable: accessed={accessed} pin={pin}"
            )
            checked += 1
    assert checked == len(ages) * len(pins)


def test_reason_is_order_independent() -> None:
    """The reason is a max over a total order, so permutations cannot change it."""
    rows = [live_row(), stale_row(), unknown_row()]
    reasons = {thread_retention(list(order), now=NOW).reason for order in permutations(rows)}

    assert len(reasons) == 1, f"reason depends on row order: {reasons}"
    assert reasons.pop() is RetentionReason.PINNED


def test_reason_serialises_as_a_plain_string() -> None:
    """It crosses a JSON boundary in the CLI, so its value must be the spelling."""
    assert str(RetentionReason.NO_EVIDENCE) == "no_evidence"
    assert f"{RetentionReason.PINNED}" == "pinned"
    assert RetentionReason.RECENT_ACCESS.value == "recent_access"
    assert RetentionReason.NONE.value == "none"


if __name__ == "__main__":  # pragma: no cover - convenience entry point
    raise SystemExit(pytest.main([__file__, "-v"]))
