"""Coverage closure for the approval state machine: every refusal is typed.

Covers the transitions the adversarial suite does not reach: duplicate and
non-PENDING registration, a tampered immutable digest, approval from a revoked
or inactive turn, revoke-after-consume, nested turn revocation, and the durable
ledger's lifecycle (path, close, duplicate insert).
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from nexus_ai_agent.nagar.ibpc.approval import (
    ApprovalError,
    ApprovalLedger,
    DurableApprovalLedger,
    durable_approval_ledger_path,
)
from nexus_ai_agent.nagar.ibpc.canonical import digest_of, sha256_bytes
from nexus_ai_agent.nagar.ibpc.models import ApprovalBinding, ApprovalState

NOW = lambda: "2026-11-19T00:00:00Z"  # noqa: E731
ACTIVE = lambda: True  # noqa: E731


def _binding(**overrides: object) -> ApprovalBinding:
    base: dict[str, object] = dict(
        session_id="s1",
        turn_id="t1",
        sequence_number=1,
        tenant_id="ten",
        project_id="p1",
        principal_id="user-1",
        source_revision=7,
        source_asset_digest=sha256_bytes(b"asset"),
        ast_digest=digest_of({"ast": 1}),
        plan_digest=digest_of({"plan": 1}),
        preview_digest=digest_of({"preview": 1}),
    )
    base.update(overrides)
    return ApprovalBinding(**base)  # type: ignore[arg-type]


def _identity_of(binding: ApprovalBinding):  # type: ignore[no-untyped-def]
    from nexus_ai_agent.nagar.ibpc.approval import ApprovalIdentity

    return ApprovalIdentity(
        principal_id=binding.principal_id,
        tenant_id=binding.tenant_id,
        project_id=binding.project_id,
        session_id=binding.session_id,
        turn_id=binding.turn_id,
        sequence_number=binding.sequence_number,
        source_revision=binding.source_revision,
        source_asset_digest=binding.source_asset_digest,
        ast_digest=binding.ast_digest,
        plan_digest=binding.plan_digest,
        preview_digest=binding.preview_digest,
    )


@pytest.fixture()
def ledger() -> ApprovalLedger:
    return ApprovalLedger(now=NOW)


def _reason(exc: pytest.ExceptionInfo[ApprovalError]) -> str:
    return exc.value.reason


def test_a_binding_must_be_pending_to_be_registered(ledger: ApprovalLedger) -> None:
    approved = _binding().model_copy(update={"state": ApprovalState.APPROVED})
    with pytest.raises(ApprovalError) as info:
        ledger.create(approved)
    assert _reason(info) == "invalid_state"


def test_registering_the_same_approval_twice_is_refused(ledger: ApprovalLedger) -> None:
    binding = _binding()
    ledger.create(binding)
    with pytest.raises(ApprovalError) as info:
        ledger.create(binding)
    assert _reason(info) == "duplicate_approval"


def test_the_whole_identity_digest_is_a_second_line_behind_the_field_check(
    ledger: ApprovalLedger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the field-by-field comparison were ever bypassed, the digest still refuses."""
    from nexus_ai_agent.nagar.ibpc import approval as ap

    binding = ledger.create(_binding())
    monkeypatch.setattr(ap.ApprovalIdentity, "mismatches", lambda self, b: ())
    forged_identity = dataclasses.replace(_identity_of(binding), principal_id="intruder")
    with pytest.raises(ApprovalError) as info:
        ledger.approve(binding.approval_id, identity=forged_identity, turn_is_active=ACTIVE)
    assert _reason(info) == "identity_digest_mismatch"


def test_approval_from_a_revoked_binding_is_refused(ledger: ApprovalLedger) -> None:
    binding = ledger.create(_binding())
    ledger.revoke(binding.approval_id)
    with pytest.raises(ApprovalError) as info:
        ledger.approve(binding.approval_id, identity=_identity_of(binding), turn_is_active=ACTIVE)
    assert _reason(info) == "revoked"


def test_approval_is_refused_when_the_turn_is_no_longer_active(ledger: ApprovalLedger) -> None:
    binding = ledger.create(_binding())
    with pytest.raises(ApprovalError) as info:
        ledger.approve(
            binding.approval_id, identity=_identity_of(binding), turn_is_active=lambda: False
        )
    assert _reason(info) == "turn_inactive"


def test_approving_twice_is_an_invalid_transition(ledger: ApprovalLedger) -> None:
    binding = ledger.create(_binding())
    ledger.approve(binding.approval_id, identity=_identity_of(binding), turn_is_active=ACTIVE)
    with pytest.raises(ApprovalError) as info:
        ledger.approve(binding.approval_id, identity=_identity_of(binding), turn_is_active=ACTIVE)
    assert _reason(info) == "invalid_state"


def test_a_consumed_approval_cannot_be_revoked(ledger: ApprovalLedger) -> None:
    binding = ledger.create(_binding())
    ledger.approve(binding.approval_id, identity=_identity_of(binding), turn_is_active=ACTIVE)
    ledger.consume(binding.approval_id, identity=_identity_of(binding))
    with pytest.raises(ApprovalError) as info:
        ledger.revoke(binding.approval_id)
    assert _reason(info) == "replay"


def test_revoke_turn_revokes_only_the_open_bindings_of_that_turn(ledger: ApprovalLedger) -> None:
    open_one = ledger.create(_binding(sequence_number=1))
    open_two = ledger.create(_binding(sequence_number=2))
    done = ledger.create(_binding(sequence_number=3))
    ledger.approve(done.approval_id, identity=_identity_of(done), turn_is_active=ACTIVE)
    ledger.consume(done.approval_id, identity=_identity_of(done))
    other_turn = ledger.create(_binding(sequence_number=4, turn_id="t2"))

    revoked = ledger.revoke_turn("s1", "t1")

    assert {b.approval_id for b in revoked} == {open_one.approval_id, open_two.approval_id}
    assert ledger.get(other_turn.approval_id).state is ApprovalState.PENDING
    assert ledger.get(done.approval_id).state is ApprovalState.CONSUMED


def test_identity_mismatch_names_the_first_differing_field(ledger: ApprovalLedger) -> None:
    binding = ledger.create(_binding())
    identity = dataclasses.replace(_identity_of(binding), principal_id="intruder")
    with pytest.raises(ApprovalError) as info:
        ledger.approve(binding.approval_id, identity=identity, turn_is_active=ACTIVE)
    assert _reason(info).startswith("identity_mismatch_")


# ----------------------------- durable ledger lifecycle -----------------------------


def test_durable_ledger_lives_under_the_workspace_and_is_closed_cleanly(tmp_path: Path) -> None:
    path = durable_approval_ledger_path(tmp_path)
    store = DurableApprovalLedger(path, now=NOW)
    assert store.path == path
    store.create(_binding())
    store.close()


def test_durable_ledger_refuses_a_duplicate_insert(tmp_path: Path) -> None:
    store = DurableApprovalLedger(tmp_path / "a.sqlite3", now=NOW)
    try:
        binding = _binding()
        store.create(binding)
        with pytest.raises(ApprovalError) as info:
            store.create(binding)
        assert _reason(info) == "duplicate_approval"
        assert store._insert(binding) is False  # the storage seam reports the collision
    finally:
        store.close()


def test_durable_nested_transactions_join_the_outer_write_lock(tmp_path: Path) -> None:
    store = DurableApprovalLedger(tmp_path / "nest.sqlite3", now=NOW)
    try:
        binding = store.create(_binding(sequence_number=1))
        store.create(_binding(sequence_number=2))
        with store._transaction():
            with store._transaction():  # nested: joins the open BEGIN IMMEDIATE
                pass
        assert store.get(binding.approval_id).state is ApprovalState.PENDING
        revoked = store.revoke_turn("s1", "t1")
        assert len(revoked) == 2
    finally:
        store.close()
