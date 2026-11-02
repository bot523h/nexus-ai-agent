"""Unit tests: the single-use, fully identity-bound approval ledger (Phase C/D).

Adversarial cases: replay after consume, every identity field mismatch
(session/turn/tenant/project/principal/sequence/revision/asset/ast/plan/
preview), stale revision, tampered digests, revoked bindings, wrong principal,
and the atomic single-use consume under concurrency.
"""

from __future__ import annotations

import threading

import pytest

from nexus_ai_agent.nagar.ibpc.approval import (
    ApprovalError,
    ApprovalIdentity,
    ApprovalLedger,
)
from nexus_ai_agent.nagar.ibpc.canonical import digest_of, sha256_bytes
from nexus_ai_agent.nagar.ibpc.models import ApprovalBinding, ApprovalState

NOW = lambda: "2026-10-31T00:00:00Z"  # noqa: E731


def _binding(**overrides) -> ApprovalBinding:
    base = dict(
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
    return ApprovalBinding(**base)


def _identity(**overrides) -> ApprovalIdentity:
    base = dict(
        principal_id="user-1",
        tenant_id="ten",
        project_id="p1",
        session_id="s1",
        turn_id="t1",
        sequence_number=1,
        source_revision=7,
        source_asset_digest=sha256_bytes(b"asset"),
        ast_digest=digest_of({"ast": 1}),
        plan_digest=digest_of({"plan": 1}),
        preview_digest=digest_of({"preview": 1}),
    )
    base.update(overrides)
    return ApprovalIdentity(**base)


IDENT = _identity()


class TestApprovalLedger:
    def test_happy_path_pending_approved_consumed(self) -> None:
        ledger = ApprovalLedger(now=NOW)
        binding = ledger.create(_binding())
        assert binding.state is ApprovalState.PENDING
        approved = ledger.approve(binding.approval_id, identity=IDENT, turn_is_active=lambda: True)
        assert approved.state is ApprovalState.APPROVED
        consumed = ledger.consume(binding.approval_id, identity=IDENT)
        assert consumed.state is ApprovalState.CONSUMED

    def test_replay_after_consume_is_refused(self) -> None:
        ledger = ApprovalLedger(now=NOW)
        binding = ledger.create(_binding())
        ledger.approve(binding.approval_id, identity=IDENT, turn_is_active=lambda: True)
        ledger.consume(binding.approval_id, identity=IDENT)
        with pytest.raises(ApprovalError) as exc:
            ledger.consume(binding.approval_id, identity=IDENT)
        assert exc.value.reason == "replay"
        with pytest.raises(ApprovalError) as exc2:
            ledger.approve(binding.approval_id, identity=IDENT, turn_is_active=lambda: True)
        assert exc2.value.reason == "replay"

    def test_consume_before_approve_refused(self) -> None:
        ledger = ApprovalLedger(now=NOW)
        binding = ledger.create(_binding())
        with pytest.raises(ApprovalError) as exc:
            ledger.consume(binding.approval_id, identity=IDENT)
        assert exc.value.reason == "not_approved"

    @pytest.mark.parametrize(
        "field,bad",
        [
            ("principal_id", "mallory"),
            ("tenant_id", "other-tenant"),
            ("project_id", "other-project"),
            ("session_id", "s2"),
            ("turn_id", "t2"),
            ("sequence_number", 2),
            ("source_revision", 8),
            ("source_asset_digest", sha256_bytes(b"tampered")),
            ("ast_digest", digest_of({"ast": 2})),
            ("plan_digest", digest_of({"plan": 2})),
            ("preview_digest", digest_of({"preview": 2})),
        ],
    )
    def test_every_identity_field_mismatch_is_refused(self, field: str, bad: object) -> None:
        """Cross-session/turn/tenant/project/principal/sequence/revision/asset/
        ast/plan/preview use of a binding is refused with a typed error naming
        the field."""
        ledger = ApprovalLedger(now=NOW)
        binding = ledger.create(_binding())
        wrong = _identity(**{field: bad})  # type: ignore[arg-type]
        with pytest.raises(ApprovalError) as exc:
            ledger.approve(binding.approval_id, identity=wrong, turn_is_active=lambda: True)
        assert exc.value.reason == f"identity_mismatch_{field}"
        # and the same refusal at consume time
        ledger.approve(binding.approval_id, identity=IDENT, turn_is_active=lambda: True)
        with pytest.raises(ApprovalError) as exc2:
            ledger.consume(binding.approval_id, identity=wrong)
        assert exc2.value.reason == f"identity_mismatch_{field}"

    def test_inactive_turn_cannot_approve(self) -> None:
        ledger = ApprovalLedger(now=NOW)
        binding = ledger.create(_binding())
        with pytest.raises(ApprovalError) as exc:
            ledger.approve(binding.approval_id, identity=IDENT, turn_is_active=lambda: False)
        assert exc.value.reason == "turn_inactive"

    def test_revoke_is_terminal(self) -> None:
        ledger = ApprovalLedger(now=NOW)
        binding = ledger.create(_binding())
        ledger.approve(binding.approval_id, identity=IDENT, turn_is_active=lambda: True)
        ledger.revoke(binding.approval_id)
        with pytest.raises(ApprovalError) as exc:
            ledger.consume(binding.approval_id, identity=IDENT)
        assert exc.value.reason == "revoked"
        with pytest.raises(ApprovalError) as exc2:
            ledger.approve(binding.approval_id, identity=IDENT, turn_is_active=lambda: True)
        assert exc2.value.reason == "revoked"

    def test_revoke_turn_revokes_all_open_bindings_of_the_turn(self) -> None:
        ledger = ApprovalLedger(now=NOW)
        a = ledger.create(_binding(turn_id="t1", approval_id="appr_a"))
        b = ledger.create(_binding(turn_id="t1", approval_id="appr_b"))
        ledger.approve(a.approval_id, identity=IDENT, turn_is_active=lambda: True)
        ledger.approve(b.approval_id, identity=IDENT, turn_is_active=lambda: True)
        ledger.create(_binding(turn_id="t2", approval_id="appr_c"))
        revoked = ledger.revoke_turn("s1", "t1")
        assert {r.approval_id for r in revoked} == {"appr_a", "appr_b"}
        assert ledger.get("appr_c").state is ApprovalState.PENDING  # other turn untouched
        assert ledger.get("appr_a").state is ApprovalState.REVOKED

    def test_duplicate_registration_refused(self) -> None:
        ledger = ApprovalLedger(now=NOW)
        binding = _binding(approval_id="appr_x")
        ledger.create(binding)
        with pytest.raises(ApprovalError) as exc:
            ledger.create(binding)
        assert exc.value.reason == "duplicate_approval"

    def test_unknown_approval(self) -> None:
        ledger = ApprovalLedger(now=NOW)
        with pytest.raises(ApprovalError) as exc:
            ledger.get("appr_nope")
        assert exc.value.reason == "unknown_approval"

    def test_concurrent_consume_has_exactly_one_winner(self) -> None:
        """The consume is an atomic compare-and-set under the ledger lock:
        N threads racing the same approval produce exactly one CONSUMED and
        N-1 typed replay refusals."""
        ledger = ApprovalLedger(now=NOW)
        binding = ledger.create(_binding())
        ledger.approve(binding.approval_id, identity=IDENT, turn_is_active=lambda: True)
        results: list[str] = []
        errors: list[str] = []

        def worker() -> None:
            try:
                ledger.consume(binding.approval_id, identity=IDENT)
                results.append("consumed")
            except ApprovalError as exc:
                errors.append(exc.reason)

        threads = [threading.Thread(target=worker) for _ in range(16)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        assert results == ["consumed"]
        assert errors == ["replay"] * 15
        assert ledger.get(binding.approval_id).state is ApprovalState.CONSUMED
