"""Outbox state machine (P3): transitions, retry, ownership, lease, recovery.

Every verdict in the pure policy is a function of explicit ``now`` / row
snapshot arguments — no sleeps, no wall clock, deterministic. The tests pin:

* allowed and invalid transitions;
* retry semantics (bounded, jitter-capped) and the permanent classification;
* ownership/lease (stale claim → recover, live claim → wait);
* the reconciliation sweep ordering.
"""

from __future__ import annotations

from nexus_ai_agent.domain.policies import outbox_policy as op
from nexus_ai_agent.domain.policies.outbox_policy import EffectStatus


def _snap(**overrides: object) -> op.Snapshot:
    base = dict(
        sequence=1,
        effect_key="telegram.send.abc",
        status=EffectStatus.PENDING,
        attempt=0,
        claimed_by=None,
        lease_until=0,
        not_before=100,
        next_retry_at=0,
        created_at=100,
    )
    base.update(overrides)
    return op.Snapshot(**base)  # type: ignore[arg-type]


# ── transitions ───────────────────────────────────────────────────────────


def test_allowed_and_forbidden_transitions_are_exhaustive() -> None:
    mapping = op.ALLOWED_TRANSITIONS
    for status in EffectStatus:
        assert status in mapping
    # PENDING may only become CLAIMED.
    assert mapping[EffectStatus.PENDING] == frozenset({EffectStatus.CLAIMED})
    # CLAIMED reaches the three recorded outcomes.
    assert mapping[EffectStatus.CLAIMED] == frozenset(
        {EffectStatus.SUCCEEDED, EffectStatus.FAILED_RETRYABLE, EffectStatus.FAILED_PERMANENT}
    )
    # FAILED_RETRYABLE may only be re-queued (as PENDING intent again).
    assert mapping[EffectStatus.FAILED_RETRYABLE] == frozenset({EffectStatus.PENDING})
    # Terminal states have no outgoing edges.
    assert mapping[EffectStatus.SUCCEEDED] == frozenset()
    assert mapping[EffectStatus.FAILED_PERMANENT] == frozenset()


def test_terminal_states_are_recognised() -> None:
    assert op.is_terminal(EffectStatus.SUCCEEDED)
    assert op.is_terminal(EffectStatus.FAILED_PERMANENT)
    assert not op.is_terminal(EffectStatus.PENDING)
    assert not op.is_terminal(EffectStatus.CLAIMED)
    assert not op.is_terminal(EffectStatus.FAILED_RETRYABLE)


# ── retry semantics ─────────────────────────────────────────────────────────


def test_retryable_failure_uses_bounded_exponential_backoff() -> None:
    state, decision = op.classify_failure(retryable=True, attempt=1)
    assert state is EffectStatus.FAILED_RETRYABLE
    assert decision.backoff_seconds == op.BASE_BACKOFF_SECONDS
    state, decision = op.classify_failure(retryable=True, attempt=2)
    assert decision.backoff_seconds == op.BASE_BACKOFF_SECONDS * 2


def test_retry_budget_exhausts_into_permanent() -> None:
    state, decision = op.classify_failure(retryable=True, attempt=op.MAX_ATTEMPTS)
    assert state is EffectStatus.FAILED_PERMANENT
    assert decision.reason == "max_attempts_exhausted"
    assert not decision.retryable


def test_non_retryable_failure_is_permanent_on_first_sight() -> None:
    state, decision = op.classify_failure(retryable=False, attempt=1)
    assert state is EffectStatus.FAILED_PERMANENT
    assert decision.reason == "non_retryable_failure"


def test_retry_delay_is_bounded_and_jitter_capped() -> None:
    now = 1_000_000
    # jitter above the cap is clamped (policy never produces an unbounded delay).
    base = op.next_retry_at(attempt=1, now=now, jitter=0)
    capped = op.next_retry_at(attempt=1, now=now, jitter=op.BUILD_MAX_JITTER_SECONDS + 1000)
    assert capped - base <= op.BUILD_MAX_JITTER_SECONDS
    # negative jitter clamps to zero.
    assert op.next_retry_at(attempt=1, now=now, jitter=-5) == base


# ── ownership & lease ───────────────────────────────────────────────────────


def test_claim_a_due_pending_row() -> None:
    row = _snap(status=EffectStatus.PENDING, not_before=100)
    assert op.classify_row(row, now=100, dispatcher_id="a") == "claim"


def test_wait_a_pending_row_not_yet_due() -> None:
    row = _snap(status=EffectStatus.PENDING, not_before=100)
    assert op.classify_row(row, now=99, dispatcher_id="a") == "wait"


def test_live_lease_owned_by_another_waits() -> None:
    row = _snap(status=EffectStatus.CLAIMED, claimed_by="other", lease_until=200, attempt=1)
    assert op.classify_row(row, now=150, dispatcher_id="a") == "wait"


def test_stale_lease_is_recoverable_by_anyone() -> None:
    # C5: lease expired at t=199; a new worker may take over at t=200.
    row = _snap(status=EffectStatus.CLAIMED, claimed_by="other", lease_until=200, attempt=1)
    assert op.classify_row(row, now=200, dispatcher_id="a") == "claim"


def test_own_live_claim_is_kept() -> None:
    row = _snap(status=EffectStatus.CLAIMED, claimed_by="a", lease_until=500, attempt=1)
    assert op.classify_row(row, now=10, dispatcher_id="a") == "claim"


def test_failed_retryable_waits_for_backoff_then_retries() -> None:
    row = _snap(status=EffectStatus.FAILED_RETRYABLE, next_retry_at=300, attempt=1)
    assert op.classify_row(row, now=299, dispatcher_id="a") == "wait"
    assert op.classify_row(row, now=300, dispatcher_id="a") == "claim"


def test_terminal_rows_dedupe_not_repeat() -> None:
    for status in (EffectStatus.SUCCEEDED, EffectStatus.FAILED_PERMANENT):
        row = _snap(status=status, claimed_by=None)
        assert op.classify_row(row, now=1_000_000, dispatcher_id="a") == "dedupe"


def test_terminal_row_with_claim_owner_is_flagged_unknown() -> None:
    # C4 residue: the delivery record disagrees with the recorded outcome.
    row = _snap(status=EffectStatus.SUCCEEDED, claimed_by="ghost")
    assert op.classify_row(row, now=1_000_000, dispatcher_id="a") == "unknown"


# ── reconciliation sweep ─────────────────────────────────────────────────────


def test_recovered_rows_are_oldest_first_and_claim_only() -> None:
    rows = [
        _snap(sequence=3, status=EffectStatus.CLAIMED, claimed_by="b", lease_until=1),
        _snap(sequence=1, status=EffectStatus.PENDING, not_before=0),
        _snap(sequence=2, status=EffectStatus.SUCCEEDED, claimed_by=None),
    ]
    recovered = op.recovered_rows(rows, now=500, dispatcher_id="a")
    assert [r.sequence for r in recovered] == [1, 3]


# ── dead-letter is opt-in, evidence-gated ────────────────────────────────────


def test_dead_letter_is_off_by_default_even_for_permanent() -> None:
    assert op.would_dead_letter(EffectStatus.FAILED_PERMANENT, dead_letter_enabled=False) is False
    assert op.would_dead_letter(EffectStatus.FAILED_PERMANENT, dead_letter_enabled=True) is True
    assert op.would_dead_letter(EffectStatus.FAILED_RETRYABLE, dead_letter_enabled=True) is False
