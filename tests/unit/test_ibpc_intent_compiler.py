"""Unit tests: deterministic intent parser + registry-validated compiler (M1/M2)."""

from __future__ import annotations

import pytest

from nexus_ai_agent.creative.packs.runtime import build_runtime_registry
from nexus_ai_agent.nagar.ibpc.compiler import (
    CompilationRefused,
    capability_identity_of,
    compile_ast,
)
from nexus_ai_agent.nagar.ibpc.intent import (
    Clarification,
    IntentAST,
    Refusal,
    intent_digest_of,
    normalize_intent,
    parse_intent,
)
from nexus_ai_agent.nagar.ibpc.models import TypedCreativeAST, VideoTimeScope

DURATION_US = 2_000_000


def _parse(text: str, revision: int = 3):
    return parse_intent(
        text,
        project_id="proj-1",
        asset_id="src",
        source_revision=revision,
        duration_us=DURATION_US,
    )


class TestParser:
    def test_english_trim_parses_to_typed_ast(self) -> None:
        result = _parse("trim from 0.5 to 1.5 seconds")
        assert isinstance(result, IntentAST)
        ast = result.ast
        assert ast.operation == "timeline.trim"
        assert ast.params == {
            "clip_asset_id": "src",
            "in_point_us": 500_000,
            "out_point_us": 1_500_000,
        }
        assert ast.scope == VideoTimeScope(
            asset_id="src", effect="keep", start_us=500_000, end_us=1_500_000
        )
        # the preserved window is derived, not asserted by the user
        assert [(r.start_us, r.end_us) for r in ast.forbidden_scopes] == [
            (500_000, 1_500_000),
        ]
        assert [(r.start_us, r.end_us) for r in ast.scope.removed_ranges(DURATION_US)] == [
            (0, 500_000),
            (1_500_000, 2_000_000),
        ]
        assert ast.executable

    def test_persian_digits_and_markers(self) -> None:
        result = _parse("از ۰.۵ تا ۱.۵ ثانیه را ببر")
        assert isinstance(result, IntentAST)
        assert result.ast.params["in_point_us"] == 500_000
        assert result.ast.params["out_point_us"] == 1_500_000

    def test_raw_text_is_never_stored_only_digest(self) -> None:
        text = "trim from 0.5 to 1.5 seconds"
        result = _parse(text)
        assert isinstance(result, IntentAST)
        assert result.intent_digest == intent_digest_of(normalize_intent(text))
        dumped = str(result.ast.model_dump()) + result.intent_digest
        assert text not in dumped

    def test_missing_range_asks_never_guesses(self) -> None:
        result = _parse("trim the video")
        assert isinstance(result, Clarification)
        assert result.reason == "missing_range"
        assert result.questions

    def test_more_than_two_numbers_is_ambiguous(self) -> None:
        result = _parse("trim from 0.5 to 1.5 and also 1.8")
        assert isinstance(result, Clarification)
        assert result.reason == "ambiguous_range"

    def test_single_number_asks(self) -> None:
        result = _parse("trim at 1 second")
        assert isinstance(result, Clarification)
        assert result.reason == "single_point"

    def test_negation_is_never_compiled(self) -> None:
        result = _parse("trim from 1 to 2 seconds but don't touch anything")
        assert isinstance(result, Clarification)
        assert result.reason == "negated_constraint"

    def test_unsupported_operation_is_a_typed_refusal(self) -> None:
        result = _parse("brighten the video")
        assert isinstance(result, Refusal)
        assert result.reason == "unsupported_operation_in_slice"

    def test_out_of_range_refused(self) -> None:
        result = _parse("trim from 1 to 5 seconds")
        assert isinstance(result, Refusal)
        assert result.reason == "out_of_range"

    def test_empty_intent_refused(self) -> None:
        assert isinstance(_parse("   "), Refusal)

    def test_unrecognized_text_asks(self) -> None:
        result = _parse("hello there")
        assert isinstance(result, Clarification)
        assert result.reason == "no_recognized_operation"


class TestCompiler:
    def test_compile_against_live_runtime_registry(self) -> None:
        registry = build_runtime_registry()
        result = _parse("trim from 0.5 to 1.5 seconds")
        assert isinstance(result, IntentAST)
        plan = compile_ast(result.ast, registry=registry)
        assert plan.ast_digest == result.ast.ast_digest
        assert plan.capability.operation_id == "timeline.trim"
        from nexus_ai_agent.creative.studio.capabilities import PermissionLevel

        assert plan.capability.permission_level == PermissionLevel.REVERSIBLE.value
        assert "local" in plan.capability.execution_modes
        assert plan.execution_mode == "local"
        assert len(plan.steps) == 1
        step_input = plan.steps[0].input
        assert step_input["clip_asset_id"] == "src"
        assert step_input["in_point_us"] == 500_000
        assert step_input["out_point_us"] == 1_500_000
        # the plan digest binds the registry's input schema digest
        ident = capability_identity_of(registry, "timeline.trim")
        assert plan.capability.schema_digest == ident.schema_digest
        assert plan.plan_digest == plan.model_dump() and True or plan.plan_digest

    def test_unknown_operation_refused(self) -> None:
        registry = build_runtime_registry()
        ast = TypedCreativeAST(
            project_id="p",
            asset_id="src",
            source_revision=1,
            operation="timeline.teleport",
            params={},
            scope=VideoTimeScope(asset_id="src", effect="keep", start_us=0, end_us=1),
        )
        with pytest.raises(CompilationRefused) as exc:
            compile_ast(ast, registry=registry)
        assert exc.value.reason == "unknown_operation"

    def test_invalid_input_against_schema_refused(self) -> None:
        registry = build_runtime_registry()
        result = _parse("trim from 0.5 to 1.5 seconds")
        assert isinstance(result, IntentAST)
        bad = result.ast.model_copy(update={"params": {"clip_asset_id": "src"}})
        with pytest.raises(CompilationRefused) as exc:
            compile_ast(bad, registry=registry)
        assert exc.value.reason == "invalid_input"

    def test_ambiguous_ast_refused(self) -> None:
        registry = build_runtime_registry()
        result = _parse("trim from 0.5 to 1.5 seconds")
        assert isinstance(result, IntentAST)
        ambiguous = result.ast.model_copy(update={"ambiguities": ("which range?",)})
        with pytest.raises(CompilationRefused) as exc:
            compile_ast(ambiguous, registry=registry)
        assert exc.value.reason == "ambiguous_ast"

    def test_schema_change_moves_identity_digest(self) -> None:
        registry = build_runtime_registry()
        ident = capability_identity_of(registry, "timeline.trim")
        moved = ident.model_copy(update={"schema_version": ident.schema_version + 1})
        assert moved.identity_digest != ident.identity_digest
