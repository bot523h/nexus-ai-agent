"""Unit tests: Delta Dialogue — relative-scope intent parser."""
from __future__ import annotations

import pytest
from pydantic import ValidationError


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
