"""Executable truth table for the TWO retention policies in this repository.

There are two row-level retention predicates, and they are **not** one
concept:

============================================  ==========================  =======================
predicate                                     policy name                 ages from
============================================  ==========================  =======================
``domain.policies.retention.deletable``       ``resumability-evidence-v1`` ``last_accessed_at``
``storage.checkpoint_lifecycle.``
``eligible_for_deletion``                     ``storage-reclaim-v1``      ``created_at``
============================================  ==========================  =======================

They disagree on exactly two independent axes, both deliberate:

1. **post-pin grace** — ``storage-reclaim-v1`` keeps protecting a record for
   ``active_grace`` after its pin lapses; ``resumability-evidence-v1`` has no
   such concept.
2. **age evidence** — ``storage-reclaim-v1`` reads an absent
   ``last_accessed_at`` as "never accessed, therefore not recent";
   ``resumability-evidence-v1`` reads it as *unknown* and retains.

This module exists so that the divergence is **measured, not asserted in
prose**.  Every row of the table below is produced by calling the real
functions.  If a future change makes the two agree, the "divergence" tests go
red — which is the point: unifying them would mean deleting one of the two
policies, and that must be a visible decision, not silent drift.

``eligible_for_deletion`` is currently dead in ``src/`` (no runtime caller),
which is precisely why nothing forces the two together and why the truth
table is the only thing keeping the boundary honest.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from nexus_ai_agent.domain.policies.retention import (
    POLICY_NAME,
    RESUMABILITY_WINDOW,
    RetentionRecord,
    deletable,
    pinned,
)
from nexus_ai_agent.storage.checkpoint_lifecycle import (
    DEFAULT_RETENTION_POLICY,
    ELIGIBILITY_POLICY_NAME,
    CheckpointRecord,
    eligible_for_deletion,
)

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
DAY = timedelta(days=1)


def row(
    *,
    created_days: int = 40,
    accessed_days: float | None = 40,
    pin_in_days: float | None = None,
) -> tuple[RetentionRecord, CheckpointRecord]:
    """The same logical record, in both predicates' input types."""
    created = NOW - created_days * DAY
    accessed = None if accessed_days is None else NOW - timedelta(days=accessed_days)
    pin = None if pin_in_days is None else NOW + timedelta(days=pin_in_days)
    return (
        RetentionRecord(created_at=created, last_accessed_at=accessed, active_until=pin),
        CheckpointRecord("t", "c", created, last_accessed_at=accessed, active_until=pin),
    )


def both(**kwargs: object) -> tuple[bool, bool]:
    """`(deletable, eligible_for_deletion)` for one record."""
    resumability, reclaim = row(**kwargs)  # type: ignore[arg-type]
    return (
        deletable(resumability, now=NOW),
        eligible_for_deletion(reclaim, now=NOW, policy=DEFAULT_RETENTION_POLICY),
    )


# ── the policies are named, and the names differ ──────────────────────────


def test_the_two_policies_are_distinctly_named() -> None:
    """Two policies with near-synonym function names must not be unnamed too.

    ``deletable`` and ``eligible_for_deletion`` are indistinguishable at a
    call site.  The policy names are what let a caller tell which decision
    they are about to make.
    """
    assert POLICY_NAME == "resumability-evidence-v1"
    assert ELIGIBILITY_POLICY_NAME == "storage-reclaim-v1"
    assert POLICY_NAME != ELIGIBILITY_POLICY_NAME


def test_both_policies_share_the_one_product_window() -> None:
    """One product number, two policies: the window is the same object."""
    assert RESUMABILITY_WINDOW is DEFAULT_RETENTION_POLICY.max_age


# ── the agreeing rows ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("state", "kwargs", "expected"),
    [
        ("1 recent access", {"accessed_days": 1}, (False, False)),
        ("2 old access", {"accessed_days": 40}, (True, True)),
        (
            "3 missing access, young creation",
            {"created_days": 1, "accessed_days": None},
            (False, False),
        ),
        ("4 active pin (+7d)", {"pin_in_days": 7}, (False, False)),
        ("7 old creation + old access", {"created_days": 40, "accessed_days": 40}, (True, True)),
        ("10 boundary: access exactly 30d ago", {"accessed_days": 30}, (True, True)),
    ],
)
def test_states_where_both_policies_agree(
    state: str, kwargs: dict[str, object], expected: tuple[bool, bool]
) -> None:
    """The two policies agree on the ordinary cases, including the boundary.

    Both use ``>=`` on the window, so a record accessed exactly 30 days ago is
    already outside it for both — no off-by-one between them.
    """
    assert both(**kwargs) == expected, state


def test_boundary_just_inside_the_window_is_retained_by_both() -> None:
    """One second inside the window protects the record under both policies."""
    one_second = 1 / 86400
    assert both(accessed_days=30 - one_second) == (False, False)


# ── the two deliberate divergences ────────────────────────────────────────


def test_axis1_post_pin_grace_exists_only_in_the_reclaim_policy() -> None:
    """A pin that lapsed inside ``active_grace`` still protects — in one policy only.

    This is not a bug to be reconciled: ``storage-reclaim-v1`` deliberately
    keeps a grace tail after a pin expires, and ``resumability-evidence-v1``
    deliberately does not model one.  Making them agree would delete a policy.
    """
    grace = DEFAULT_RETENTION_POLICY.active_grace
    assert grace == timedelta(days=7)

    # Pin lapsed 1 day ago: inside the 7-day tail.
    assert both(pin_in_days=-1) == (True, False)
    # Pin lapsed 10 days ago: outside the tail, so both now agree.
    assert both(pin_in_days=-10) == (True, True)


def test_axis2_missing_access_evidence_is_read_oppositely() -> None:
    """Absent ``last_accessed_at``: *unknown* in one policy, *not recent* in the other.

    The fail-closed direction matters: ``resumability-evidence-v1`` retains
    (no evidence of age), ``storage-reclaim-v1`` reclaims (age is provable
    from ``created_at``, which is NOT NULL in both stores).
    """
    assert both(created_days=40, accessed_days=None) == (False, True)


def _grace_axis(pin_in_days: float | None) -> bool:
    """Axis 1: the pin lapsed, but inside the reclaim policy's grace tail."""
    if pin_in_days is None or pin_in_days >= 0:
        return False
    return timedelta(days=-pin_in_days) < DEFAULT_RETENTION_POLICY.active_grace


def _age_basis_axis(created_days: int, accessed_days: float | None) -> bool:
    """Axis 2: the two policies age from different timestamps.

    They can only disagree when the two timestamps fall on *opposite* sides of
    the window, or when the access stamp is missing altogether (which the
    resumability policy reads as unknown and the reclaim policy skips).
    """
    window = RESUMABILITY_WINDOW.days
    if accessed_days is None:
        return created_days >= window
    return (created_days >= window) != (accessed_days >= window)


def test_the_divergence_set_is_exactly_the_two_documented_axes() -> None:
    """Sweep the state space; every disagreement must be one of the two axes.

    This is the guard against a *third* divergence appearing silently: a future
    edit that makes the policies disagree for a new reason fails here and names
    the state.  The per-group counts also pin the divergence set exactly, so a
    divergence cannot quietly disappear either.

    Group counts are measured, not assumed:
      * axis 1 (post-pin grace)                     9 cells
      * axis 2a (missing access, old creation)      6 cells
      * axis 2b (young creation, old access)       18 cells
    """
    window = RESUMABILITY_WINDOW.days
    unexplained: list[str] = []
    grace_cells = 0
    missing_access_cells = 0
    opposite_sides_cells = 0
    agreed = 0

    for created_days in (1, 29, 30, 31, 40):
        for accessed_days in (None, 1, 29, 30, 31, 40):
            for pin_in_days in (None, -10, -1, 7):
                d, e = both(
                    created_days=created_days,
                    accessed_days=accessed_days,
                    pin_in_days=pin_in_days,
                )
                if d == e:
                    agreed += 1
                    continue
                label = f"created={created_days}d accessed={accessed_days} pin={pin_in_days}"
                by_grace = _grace_axis(pin_in_days)
                by_age = _age_basis_axis(created_days, accessed_days)
                if not (by_grace or by_age):
                    unexplained.append(f"{label} -> deletable={d} eligible={e}")
                if by_grace and not by_age:
                    grace_cells += 1
                if accessed_days is None and created_days >= window:
                    missing_access_cells += 1
                elif accessed_days is not None and (
                    (created_days >= window) != (accessed_days >= window)
                ):
                    opposite_sides_cells += 1

    assert not unexplained, "divergence not explained by either axis:\n" + "\n".join(unexplained)
    assert grace_cells == 9, grace_cells
    assert missing_access_cells == 6, missing_access_cells
    assert opposite_sides_cells == 18, opposite_sides_cells
    assert agreed == 120 - 33, agreed


def test_axis2b_is_unreachable_in_production_and_is_documented_as_such() -> None:
    """The 18 "young creation + old access" cells cannot occur in a real index.

    A checkpoint cannot be accessed before it exists, so
    ``last_accessed_at < created_at`` is not a reachable row.  The divergence
    is real at *function* level and is pinned above, but the production-facing
    divergence is only axis 1 (grace) and axis 2a (missing evidence).  This
    test records that distinction so nobody "fixes" a case that cannot happen
    while missing the two that can.
    """
    # reachable: created 40d ago, never accessed
    assert both(created_days=40, accessed_days=None) == (False, True)
    # unreachable: accessed 30d before it was created
    assert both(created_days=1, accessed_days=30) == (True, False)


# ── timezone / naive spellings must not create a third divergence ─────────


@pytest.mark.parametrize(
    "label",
    ["utc aware", "+03:00 aware", "-08:00 aware", "naive utc"],
)
def test_the_same_instant_spelled_differently_gives_the_same_answer(label: str) -> None:
    """Neither policy may let a timestamp's *spelling* change the verdict."""
    base = NOW - 40 * DAY
    stamps = {
        "utc aware": base,
        "+03:00 aware": base.astimezone(timezone(timedelta(hours=3))),
        "-08:00 aware": base.astimezone(timezone(timedelta(hours=-8))),
        "naive utc": base.replace(tzinfo=None),
    }
    stamp = stamps[label]
    resumability = RetentionRecord(created_at=stamp, last_accessed_at=stamp)
    reclaim = CheckpointRecord("t", "c", stamp, last_accessed_at=stamp)

    assert deletable(resumability, now=NOW) is True
    assert eligible_for_deletion(reclaim, now=NOW, policy=DEFAULT_RETENTION_POLICY) is True


def test_a_naive_now_does_not_raise_or_flip_either_policy() -> None:
    """A naive ``now`` is normalised by both; neither may raise TypeError."""
    resumability, reclaim = row(accessed_days=40)
    naive_now = NOW.replace(tzinfo=None)

    assert deletable(resumability, now=naive_now) is True
    assert eligible_for_deletion(reclaim, now=naive_now, policy=DEFAULT_RETENTION_POLICY) is True


# ── the resumability policy's own fail-closed core ────────────────────────


def test_resumability_policy_is_fail_closed_on_missing_evidence() -> None:
    """The policy that drives the destructive CLI verdict never infers safety."""
    resumability, _ = row(created_days=400, accessed_days=None)
    # 400 days old and still not deletable: absence of access evidence is not
    # evidence of age.  No heuristic may stand in for the missing stamp.
    assert deletable(resumability, now=NOW) is False
    assert pinned(resumability, now=NOW) is False


def test_reclaim_policy_rejects_a_non_positive_window() -> None:
    """``RetentionPolicy`` refuses a window that would make everything reclaimable."""
    from nexus_ai_agent.storage.checkpoint_lifecycle import RetentionPolicy

    with pytest.raises(ValueError):
        RetentionPolicy(max_age=timedelta(0))
    with pytest.raises(ValueError):
        RetentionPolicy(active_grace=timedelta(seconds=-1))
