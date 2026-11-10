"""Unit tests: Delta Dialogue — relative-scope intent parser."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

DURATION_US = 6_000_000  # 6 seconds
CBN = "cbn_" + "a" * 64

def _scope(start_s: float, end_s: float):
    from nexus_ai_agent.nagar.ibpc.models import VideoTimeScope
    return VideoTimeScope(
        asset_id="a1",
        effect="keep",
        start_us=int(start_s * 1_000_000),
        end_us=int(end_s * 1_000_000),
    )

def _delta(text: str, scope=None, duration_us: int = DURATION_US):
    from nexus_ai_agent.nagar.ibpc.delta import parse_delta_intent
    if scope is None:
        scope = _scope(1.0, 4.0)
    return parse_delta_intent(
        text,
        current_scope=scope,
        project_id="p1",
        asset_id="a1",
        source_revision=1,
        duration_us=duration_us,
        channel_binding_id=CBN,
    )


class TestDeltaModels:
    def test_delta_anchor_values(self) -> None:
        from nexus_ai_agent.nagar.ibpc.delta import DeltaAnchor
        assert DeltaAnchor.IN_POINT == "in_point"
        assert DeltaAnchor.OUT_POINT == "out_point"

    def test_delta_op_rejects_extra_fields(self) -> None:
        from nexus_ai_agent.nagar.ibpc.delta import DeltaAnchor, DeltaOp
        with pytest.raises(ValidationError):
            DeltaOp(anchor=DeltaAnchor.OUT_POINT, delta_us=-1_000_000, extra_field="bad")  # type: ignore[call-arg]

    def test_delta_op_stores_zero(self) -> None:
        # model allows 0; parse_delta_intent refuses it
        from nexus_ai_agent.nagar.ibpc.delta import DeltaAnchor, DeltaOp
        op = DeltaOp(anchor=DeltaAnchor.OUT_POINT, delta_us=0)
        assert op.delta_us == 0

    def test_delta_op_is_frozen(self) -> None:
        from nexus_ai_agent.nagar.ibpc.delta import DeltaAnchor, DeltaOp
        op = DeltaOp(anchor=DeltaAnchor.OUT_POINT, delta_us=-500_000)
        with pytest.raises(Exception):
            op.delta_us = 999  # type: ignore[misc]

    def test_delta_intent_ast_fields(self) -> None:
        from nexus_ai_agent.nagar.ibpc.delta import DeltaAnchor, DeltaIntentAST, DeltaOp
        from nexus_ai_agent.nagar.ibpc.intent import parse_intent
        from nexus_ai_agent.nagar.ibpc.models import VideoTimeScope
        result = parse_intent(
            "trim from 1 to 3 seconds",
            project_id="p1",
            asset_id="a1",
            source_revision=1,
            duration_us=5_000_000,
            channel_binding_id="cbn_" + "a" * 64,
        )
        from nexus_ai_agent.nagar.ibpc.intent import IntentAST
        assert isinstance(result, IntentAST)
        op = DeltaOp(anchor=DeltaAnchor.OUT_POINT, delta_us=-500_000)
        ast_obj = DeltaIntentAST(
            base_scope_digest=result.ast.scope.scope_digest,
            delta_op=op,
            resolved_ast=result.ast,
            intent_digest=result.intent_digest,
        )
        assert ast_obj.base_scope_digest == result.ast.scope.scope_digest
        assert ast_obj.resolved_ast.executable


# ---------------------------------------------------------------------------
# parse_delta_intent behaviour tests
# ---------------------------------------------------------------------------

class TestParseDeltaIntent:
    def test_english_trim_end(self) -> None:
        from nexus_ai_agent.nagar.ibpc.delta import DeltaAnchor, DeltaIntentAST
        r = _delta("trim 1 more second from the end")
        assert isinstance(r, DeltaIntentAST)
        assert r.delta_op.anchor == DeltaAnchor.OUT_POINT
        assert r.delta_op.delta_us == -1_000_000
        # scope [1s, 4s]; out_point moves from 4s to 3s
        assert r.resolved_ast.scope.end_us == 3_000_000
        assert r.resolved_ast.scope.start_us == 1_000_000

    def test_persian_trim_end(self) -> None:
        from nexus_ai_agent.nagar.ibpc.delta import DeltaAnchor, DeltaIntentAST
        r = _delta("یک ثانیه از تهش ببر")
        assert isinstance(r, DeltaIntentAST)
        assert r.delta_op.anchor == DeltaAnchor.OUT_POINT
        assert r.delta_op.delta_us == -1_000_000

    def test_english_trim_start(self) -> None:
        from nexus_ai_agent.nagar.ibpc.delta import DeltaAnchor, DeltaIntentAST
        r = _delta("trim 0.5 seconds from the start")
        assert isinstance(r, DeltaIntentAST)
        assert r.delta_op.anchor == DeltaAnchor.IN_POINT
        # start moves right: +500_000
        assert r.delta_op.delta_us == 500_000
        assert r.resolved_ast.scope.start_us == 1_500_000
        assert r.resolved_ast.scope.end_us == 4_000_000

    def test_no_prior_scope_returns_clarification(self) -> None:
        from nexus_ai_agent.nagar.ibpc.delta import parse_delta_intent
        from nexus_ai_agent.nagar.ibpc.intent import Clarification
        r = parse_delta_intent(
            "trim 1 more second from the end",
            current_scope=None,
            project_id="p1", asset_id="a1",
            source_revision=1, duration_us=DURATION_US, channel_binding_id=CBN,
        )
        assert isinstance(r, Clarification)
        assert r.reason == "no_prior_scope"

    def test_zero_delta_returns_refusal(self) -> None:
        from nexus_ai_agent.nagar.ibpc.intent import Refusal
        r = _delta("trim 0 seconds from the end")
        assert isinstance(r, Refusal)
        assert r.reason == "zero_delta"

    def test_inverted_scope_returns_refusal(self) -> None:
        from nexus_ai_agent.nagar.ibpc.intent import Refusal
        # scope [1s, 1.3s]; trim 1s from end → end would be 0.3s < start 1s
        small = _scope(1.0, 1.3)
        r = _delta("trim 1 second from the end", scope=small)
        assert isinstance(r, Refusal)
        assert r.reason == "delta_out_of_range"

    def test_base_scope_digest_matches(self) -> None:
        from nexus_ai_agent.nagar.ibpc.delta import DeltaIntentAST
        scope = _scope(1.0, 4.0)
        r = _delta("trim 1 more second from the end", scope=scope)
        assert isinstance(r, DeltaIntentAST)
        assert r.base_scope_digest == scope.scope_digest

    def test_resolved_ast_is_executable(self) -> None:
        from nexus_ai_agent.nagar.ibpc.delta import DeltaIntentAST
        r = _delta("trim 1 more second from the end")
        assert isinstance(r, DeltaIntentAST)
        assert r.resolved_ast.executable
        assert r.resolved_ast.operation == "timeline.trim"

    def test_resolved_ast_params_match_scope(self) -> None:
        from nexus_ai_agent.nagar.ibpc.delta import DeltaIntentAST
        r = _delta("trim 1 more second from the end")
        assert isinstance(r, DeltaIntentAST)
        p = r.resolved_ast.params
        assert p["in_point_us"] == r.resolved_ast.scope.start_us
        assert p["out_point_us"] == r.resolved_ast.scope.end_us

    def test_ambiguous_text_returns_clarification(self) -> None:
        from nexus_ai_agent.nagar.ibpc.intent import Clarification
        r = _delta("make it shorter somehow")
        assert isinstance(r, Clarification)

    def test_persian_digit_normalisation(self) -> None:
        from nexus_ai_agent.nagar.ibpc.delta import DeltaIntentAST
        # ۲ = 2 in Persian
        r = _delta("۲ ثانیه از تهش ببر")
        assert isinstance(r, DeltaIntentAST)
        assert r.delta_op.delta_us == -2_000_000

    def test_add_to_end(self) -> None:
        from nexus_ai_agent.nagar.ibpc.delta import DeltaAnchor, DeltaIntentAST
        # scope [1s, 4s]; add 1s to end → end becomes 5s
        r = _delta("add 1 second to the end")
        assert isinstance(r, DeltaIntentAST)
        assert r.delta_op.anchor == DeltaAnchor.OUT_POINT
        assert r.delta_op.delta_us == +1_000_000
        assert r.resolved_ast.scope.end_us == 5_000_000

    def test_exceeds_duration_returns_refusal(self) -> None:
        from nexus_ai_agent.nagar.ibpc.intent import Refusal
        # add 10s to end exceeds 6s duration
        r = _delta("add 10 seconds to the end")
        assert isinstance(r, Refusal)
        assert r.reason == "delta_out_of_range"
