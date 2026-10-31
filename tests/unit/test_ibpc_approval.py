"""Unit tests: the single-use, digest-bound approval ledger (M3).

Adversarial cases from the mission: replay after consume, stale revision,
tampered preview/plan/AST/asset, wrong principal, revoked on interrupt/cancel.
"""

from __future__ import annotations

import pytest

from nexus_ai_agent.nagar.ibpc.approval import ApprovalError, ApprovalLedger
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


MATCH = dict(
    principal_id="user-1",
    source_revision=7,
    ast_digest=digest_of({"ast": 1}),
    plan_digest=digest_of({"plan": 1}),
    preview_digest=digest_of({"preview": 1}),
    source_asset_digest=sha256_bytes(b"asset"),
)


class TestApprovalLedger:
    def test_happy_path_pending_approved_consumed(self) -> None:
        ledger = ApprovalLedger(now=NOW)
        binding = ledger.create(_binding())
        assert binding.state is ApprovalState.PENDING
        approved = ledger.approve(binding.approval_id, turn_is_active=lambda: True, **MATCH)
        assert approved.state is ApprovalState.APPROVED
        consumed = ledger.consume(binding.approval_id, **MATCH)
        assert consumed.state is ApprovalState.CONSUMED

    def test_replay_after_consume_is_refused(self) -> None:
        ledger = ApprovalLedger(now=NOW)
        binding = ledger.create(_binding())
        ledger.approve(binding.approval_id, turn_is_active=lambda: True, **MATCH)
        ledger.consume(binding.approval_id, **MATCH)
        with pytest.raises(ApprovalError) as exc:
            ledger.consume(binding.approval_id, **MATCH)
        assert exc.value.reason == "replay"
        with pytest.raises(ApprovalError) as exc2:
            ledger.approve(binding.approval_id, turn_is_active=lambda: True, **MATCH)
        assert exc2.value.reason == "replay"

    def test_consume_before_approve_refused(self) -> None:
        ledger = ApprovalLedger(now=NOW)
        binding = ledger.create(_binding())
        with pytest.raises(ApprovalError) as exc:
            ledger.consume(binding.approval_id, **MATCH)
        assert exc.value.reason == "not_approved"

    def test_wrong_principal_refused(self) -> None:
        ledger = ApprovalLedger(now=NOW)
        binding = ledger.create(_binding())
        with pytest.raises(ApprovalError) as exc:
            ledger.approve(
                binding.approval_id,
                turn_is_active=lambda: True,
                **{**MATCH, "principal_id": "mallory"},
            )
        assert exc.value.reason == "wrong_principal"

    def test_stale_revision_refused(self) -> None:
        ledger = ApprovalLedger(now=NOW)
        binding = ledger.create(_binding())
        with pytest.raises(ApprovalError) as exc:
            ledger.approve(
                binding.approval_id,
                turn_is_active=lambda: True,
                **{**MATCH, "source_revision": 8},
            )
        assert exc.value.reason == "stale_revision"

    @pytest.mark.parametrize(
        "field,key",
        [
            ("ast_digest", "ast_digest"),
            ("plan_digest", "plan_digest"),
            ("preview_digest", "preview_digest"),
            ("source_asset_digest", "source_asset_digest"),
        ],
    )
    def test_tampered_digest_refused(self, field: str, key: str) -> None:
        ledger = ApprovalLedger(now=NOW)
        binding = ledger.create(_binding())
        tampered = {
            **MATCH,
            key: digest_of({"tampered": True})
            if "digest" in key and "asset" not in key
            else sha256_bytes(b"tampered"),
        }
        with pytest.raises(ApprovalError):
            ledger.approve(binding.approval_id, turn_is_active=lambda: True, **tampered)

    def test_inactive_turn_cannot_approve(self) -> None:
        ledger = ApprovalLedger(now=NOW)
        binding = ledger.create(_binding())
        with pytest.raises(ApprovalError) as exc:
            ledger.approve(binding.approval_id, turn_is_active=lambda: False, **MATCH)
        assert exc.value.reason == "turn_inactive"

    def test_revoke_is_terminal(self) -> None:
        ledger = ApprovalLedger(now=NOW)
        binding = ledger.create(_binding())
        ledger.approve(binding.approval_id, turn_is_active=lambda: True, **MATCH)
        ledger.revoke(binding.approval_id)
        with pytest.raises(ApprovalError) as exc:
            ledger.consume(binding.approval_id, **MATCH)
        assert exc.value.reason == "revoked"
        with pytest.raises(ApprovalError) as exc2:
            ledger.approve(binding.approval_id, turn_is_active=lambda: True, **MATCH)
        assert exc2.value.reason == "revoked"

    def test_revoke_turn_revokes_all_open_bindings_of_the_turn(self) -> None:
        ledger = ApprovalLedger(now=NOW)
        a = ledger.create(_binding(turn_id="t1", approval_id="appr_a"))
        b = ledger.create(_binding(turn_id="t1", approval_id="appr_b"))
        ledger.approve(a.approval_id, turn_is_active=lambda: True, **MATCH)
        ledger.approve(b.approval_id, turn_is_active=lambda: True, **MATCH)
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
