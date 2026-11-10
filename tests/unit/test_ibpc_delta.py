"""Unit tests: Delta Dialogue — deterministic relative-scope intent parser.

Covers the mandatory matrix from the approved spec
(``docs/superpowers/specs/2026-11-10-nagar-triad-design.md``):
English, Persian, Persian numerals, negation, contradiction, multiple
operations, missing scope, asset mismatch, negative revision, zero delta,
excessive delta, reversed scope, duration overflow, digest integrity, typed
results and the public API.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError
import typing as _typing

from nexus_ai_agent.nagar.ibpc.delta import (
    DeltaAnchor,
    DeltaIntentAST,
    _normalize_word_numbers,
    parse_delta_intent,
)
from nexus_ai_agent.nagar.ibpc.intent import Clarification, Refusal
from nexus_ai_agent.nagar.ibpc.models import VideoTimeScope

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

DURATION_US = 6_000_000  # 6 seconds
CBN = "cbn_" + "a" * 64


def _scope(start_s: float, end_s: float, asset_id: str = "a1"):
    from nexus_ai_agent.nagar.ibpc.models import VideoTimeScope

    return VideoTimeScope(
        asset_id=asset_id,
        effect="keep",
        start_us=int(start_s * 1_000_000),
        end_us=int(end_s * 1_000_000),
    )


def _delta(
    text: str,
    scope=None,
    duration_us: int = DURATION_US,
    asset_id: str = "a1",
    source_revision: int = 1,
):
    from nexus_ai_agent.nagar.ibpc.delta import parse_delta_intent

    if scope is None:
        scope = _scope(1.0, 4.0)
    return parse_delta_intent(
        text,
        current_scope=scope,
        project_id="p1",
        asset_id=asset_id,
        source_revision=source_revision,
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
        with pytest.raises(ValidationError):
            op.delta_us = 999  # type: ignore[misc]

    def test_delta_ast_requires_at_least_one_op(self) -> None:
        from nexus_ai_agent.nagar.ibpc.delta import DeltaIntentAST

        with pytest.raises(ValidationError):
            DeltaIntentAST(  # type: ignore[call-arg]
                base_scope_digest="sha256:" + "0" * 64,
                delta_ops=(),
                resolved_ast=None,
                intent_digest="sha256:" + "0" * 64,
            )

    def test_delta_op_property_returns_first(self) -> None:
        from nexus_ai_agent.nagar.ibpc.delta import DeltaAnchor, DeltaIntentAST, DeltaOp
        from nexus_ai_agent.nagar.ibpc.models import TypedCreativeAST, VideoTimeScope

        scope = _scope(1.0, 4.0)
        ast = TypedCreativeAST(
            project_id="p1",
            asset_id="a1",
            channel_binding_id=CBN,
            source_revision=1,
            operation="timeline.trim",
            params={"clip_asset_id": "a1", "in_point_us": 1_000_000, "out_point_us": 4_000_000},
            scope=scope,
            forbidden_scopes=tuple(
                VideoTimeScope(asset_id="a1", effect="modify", start_us=r.start_us, end_us=r.end_us)
                for r in scope.forbidden_ranges(DURATION_US)
            ),
            ambiguities=(),
        )
        ops = (
            DeltaOp(anchor=DeltaAnchor.IN_POINT, delta_us=500_000),
            DeltaOp(anchor=DeltaAnchor.OUT_POINT, delta_us=-500_000),
        )
        obj = DeltaIntentAST(
            base_scope_digest=scope.scope_digest,
            delta_ops=ops,
            resolved_ast=ast,
            intent_digest="sha256:" + "0" * 64,
        )
        assert obj.delta_op == ops[0]
        assert len(obj.delta_ops) == 2


# ---------------------------------------------------------------------------
# Core parse behaviour
# ---------------------------------------------------------------------------


class TestParseDeltaIntent:
    def test_english_trim_end(self) -> None:
        from nexus_ai_agent.nagar.ibpc.delta import DeltaAnchor, DeltaIntentAST

        r = _delta("trim 1 more second from the end")
        assert isinstance(r, DeltaIntentAST)
        assert r.delta_op.anchor == DeltaAnchor.OUT_POINT
        assert r.delta_op.delta_us == -1_000_000
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
        assert r.delta_op.delta_us == 500_000
        assert r.resolved_ast.scope.start_us == 1_500_000
        assert r.resolved_ast.scope.end_us == 4_000_000

    def test_add_to_end(self) -> None:
        from nexus_ai_agent.nagar.ibpc.delta import DeltaAnchor, DeltaIntentAST

        r = _delta("add 1 second to the end")
        assert isinstance(r, DeltaIntentAST)
        assert r.delta_op.anchor == DeltaAnchor.OUT_POINT
        assert r.delta_op.delta_us == +1_000_000
        assert r.resolved_ast.scope.end_us == 5_000_000

    def test_move_out_point_earlier(self) -> None:
        from nexus_ai_agent.nagar.ibpc.delta import DeltaAnchor, DeltaIntentAST

        r = _delta("move out point earlier by 1 second")
        assert isinstance(r, DeltaIntentAST)
        assert r.delta_op.anchor == DeltaAnchor.OUT_POINT
        assert r.delta_op.delta_us == -1_000_000

    def test_move_in_point_later(self) -> None:
        from nexus_ai_agent.nagar.ibpc.delta import DeltaAnchor, DeltaIntentAST

        r = _delta("move in point later by 1 second")
        assert isinstance(r, DeltaIntentAST)
        assert r.delta_op.anchor == DeltaAnchor.IN_POINT
        assert r.delta_op.delta_us == +1_000_000

    def test_persian_digit_normalisation(self) -> None:
        from nexus_ai_agent.nagar.ibpc.delta import DeltaIntentAST

        r = _delta("۲ ثانیه از تهش ببر")
        assert isinstance(r, DeltaIntentAST)
        assert r.delta_op.delta_us == -2_000_000

    def test_persian_word_number(self) -> None:
        from nexus_ai_agent.nagar.ibpc.delta import DeltaIntentAST

        r = _delta("دو ثانیه از تهش ببر")
        assert isinstance(r, DeltaIntentAST)
        assert r.delta_op.delta_us == -2_000_000


# ---------------------------------------------------------------------------
# Missing scope / typed results
# ---------------------------------------------------------------------------


class TestTypedResults:
    def test_no_prior_scope_returns_clarification(self) -> None:
        from nexus_ai_agent.nagar.ibpc.intent import Clarification

        r = _delta("trim 1 more second from the end", scope=None)
        # _delta substitutes a default scope, so call the parser directly
        from nexus_ai_agent.nagar.ibpc.delta import parse_delta_intent

        r = parse_delta_intent(
            "trim 1 more second from the end",
            current_scope=None,
            project_id="p1",
            asset_id="a1",
            source_revision=1,
            duration_us=DURATION_US,
            channel_binding_id=CBN,
        )
        assert isinstance(r, Clarification)
        assert r.reason == "no_prior_scope"

    def test_ambiguous_text_returns_clarification(self) -> None:
        from nexus_ai_agent.nagar.ibpc.intent import Clarification

        r = _delta("make it shorter somehow")
        assert isinstance(r, Clarification)

    def test_return_type_is_typed_not_object(self) -> None:
        from nexus_ai_agent.nagar.ibpc import delta as delta_mod

        ann = delta_mod.parse_delta_intent.__annotations__["return"]
        text = ann if isinstance(ann, str) else str(ann)
        assert "DeltaIntentAST" in text
        assert "Clarification" in text
        assert "Refusal" in text
        assert text.strip() != "object"


# ---------------------------------------------------------------------------
# Negation (defect: negative command executed as positive)
# ---------------------------------------------------------------------------


class TestNegation:
    def test_english_negation_refused(self) -> None:
        from nexus_ai_agent.nagar.ibpc.intent import Refusal

        r = _delta("don't trim the end")
        assert isinstance(r, Refusal)
        assert r.reason == "negated_delta"

    def test_english_do_not_remove_refused(self) -> None:
        from nexus_ai_agent.nagar.ibpc.intent import Refusal

        r = _delta("do not remove one second from the end")
        assert isinstance(r, Refusal)
        assert r.reason == "negated_delta"

    def test_persian_negation_refused(self) -> None:
        from nexus_ai_agent.nagar.ibpc.intent import Refusal

        r = _delta("یک ثانیه از تهش نبر")
        assert isinstance(r, Refusal)
        assert r.reason == "negated_delta"

    def test_persian_nemikhaham_refused(self) -> None:
        from nexus_ai_agent.nagar.ibpc.intent import Refusal

        r = _delta("نمیخواهم از اولش کم شود")
        assert isinstance(r, Refusal)
        assert r.reason == "negated_delta"

    def test_persian_bare_no_is_negation_not_nine(self) -> None:
        # defect #6: "نه" must never be read as the number 9
        from nexus_ai_agent.nagar.ibpc.intent import Refusal

        r = _delta("نه ثانیه از تهش ببر")
        # The negation guard runs before word-number normalisation, so "نه" is
        # always refused as a negation — it can never be observed as a 9s delta.
        assert isinstance(r, Refusal)
        assert r.reason == "negated_delta"


# ---------------------------------------------------------------------------
# Contradiction / multiple operations (defect: silent partial execution)
# ---------------------------------------------------------------------------


class TestContradictionAndMultipleOps:
    def test_contradictory_same_anchor_refused(self) -> None:
        from nexus_ai_agent.nagar.ibpc.intent import Refusal

        r = _delta("trim 1 second from the end and add 2 seconds to the end")
        assert isinstance(r, Refusal)
        assert r.reason == "contradictory_delta"

    def test_multiple_ops_all_retained(self) -> None:
        from nexus_ai_agent.nagar.ibpc.delta import DeltaAnchor, DeltaIntentAST

        r = _delta("trim 1 second from the start and add 2 seconds to the end")
        assert isinstance(r, DeltaIntentAST)
        assert len(r.delta_ops) == 2
        assert {op.anchor for op in r.delta_ops} == {DeltaAnchor.IN_POINT, DeltaAnchor.OUT_POINT}

    def test_partially_ambiguous_request_fails_closed(self) -> None:
        # defect #8: no clause may be silently dropped
        from nexus_ai_agent.nagar.ibpc.intent import Clarification

        r = _delta("trim 1 second from the end and make it nice")
        assert isinstance(r, Clarification)


# ---------------------------------------------------------------------------
# Identity / revision guards
# ---------------------------------------------------------------------------


class TestIdentityGuards:
    def test_asset_mismatch_refused(self) -> None:
        from nexus_ai_agent.nagar.ibpc.intent import Refusal

        r = _delta("trim 1 second from the end", asset_id="a2")
        assert isinstance(r, Refusal)
        assert r.reason == "asset_mismatch"

    def test_negative_source_revision_refused_not_raised(self) -> None:
        from nexus_ai_agent.nagar.ibpc.intent import Refusal

        r = _delta("trim 1 second from the end", source_revision=-1)
        assert isinstance(r, Refusal)
        assert r.reason == "invalid_source_revision"

    def test_bool_source_revision_refused(self) -> None:
        from nexus_ai_agent.nagar.ibpc.intent import Refusal

        r = _delta("trim 1 second from the end", source_revision=True)  # type: ignore[arg-type]
        assert isinstance(r, Refusal)
        assert r.reason == "invalid_source_revision"


# ---------------------------------------------------------------------------
# Numeric bounds
# ---------------------------------------------------------------------------


class TestNumericBounds:
    def test_zero_delta_returns_refusal(self) -> None:
        from nexus_ai_agent.nagar.ibpc.intent import Refusal

        r = _delta("trim 0 seconds from the end")
        assert isinstance(r, Refusal)
        assert r.reason == "zero_delta"

    def test_excessive_delta_refused(self) -> None:
        from nexus_ai_agent.nagar.ibpc.intent import Refusal

        # > half duration (6s/2 = 3s)
        r = _delta("trim 5 seconds from the end")
        assert isinstance(r, Refusal)
        assert r.reason == "delta_out_of_range"

    def test_duration_overflow_refused(self) -> None:
        from nexus_ai_agent.nagar.ibpc.intent import Refusal

        # scope end 4s + 2s = 6s > duration 5.5s
        r = _delta("add 2 seconds to the end", duration_us=5_500_000)
        assert isinstance(r, Refusal)
        assert r.reason == "delta_out_of_range"

    def test_inverted_scope_from_end_refused(self) -> None:
        from nexus_ai_agent.nagar.ibpc.intent import Refusal

        small = _scope(1.0, 1.3)
        r = _delta("trim 1 second from the end", scope=small)
        assert isinstance(r, Refusal)
        assert r.reason == "delta_out_of_range"

    def test_inverted_scope_from_start_refused(self) -> None:
        from nexus_ai_agent.nagar.ibpc.intent import Refusal

        small = _scope(1.0, 1.3)
        r = _delta("trim 1 second from the start", scope=small)
        assert isinstance(r, Refusal)
        assert r.reason == "delta_out_of_range"

    def test_negative_resolved_start_is_a_typed_refusal_not_a_raw_error(self) -> None:
        """Growing the start beyond zero must fail closed with a typed Refusal.

        "add 2 seconds at the start" moves the in-point 2s earlier; against a
        scope starting at 1s that resolves to -1s.  ``VideoTimeScope.start_us``
        is ``ge=0``, so the parser must reject the *resolved* value before the
        model is constructed — otherwise a raw ``ValidationError`` would escape
        this public boundary (the exact defect this guard exists to prevent).
        """
        from nexus_ai_agent.nagar.ibpc.intent import Refusal

        r = _delta("add 2 seconds at the start")  # 1s - 2s = -1s
        assert isinstance(r, Refusal)
        assert r.reason == "delta_out_of_range"

    def test_unknown_duration_refused(self) -> None:
        from nexus_ai_agent.nagar.ibpc.intent import Refusal

        r = _delta("trim 1 second from the end", duration_us=0)
        assert isinstance(r, Refusal)
        assert r.reason == "unknown_duration"


# ---------------------------------------------------------------------------
# Digest / audit integrity
# ---------------------------------------------------------------------------


class TestDigestIntegrity:
    def test_base_scope_digest_matches(self) -> None:
        from nexus_ai_agent.nagar.ibpc.delta import DeltaIntentAST

        scope = _scope(1.0, 4.0)
        r = _delta("trim 1 more second from the end", scope=scope)
        assert isinstance(r, DeltaIntentAST)
        assert r.base_scope_digest == scope.scope_digest

    def test_intent_digest_stable_across_calls(self) -> None:
        from nexus_ai_agent.nagar.ibpc.delta import DeltaIntentAST

        a = _delta("trim 1 more second from the end")
        b = _delta("trim 1 more second from the end")
        assert isinstance(a, DeltaIntentAST) and isinstance(b, DeltaIntentAST)
        assert a.intent_digest == b.intent_digest

    def test_resolved_ast_is_executable_and_consistent(self) -> None:
        from nexus_ai_agent.nagar.ibpc.delta import DeltaIntentAST

        r = _delta("trim 1 more second from the end")
        assert isinstance(r, DeltaIntentAST)
        assert r.resolved_ast.executable
        assert r.resolved_ast.operation == "timeline.trim"
        p = r.resolved_ast.params
        assert p["in_point_us"] == r.resolved_ast.scope.start_us
        assert p["out_point_us"] == r.resolved_ast.scope.end_us


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


class TestPublicAPI:
    def test_all_exports(self) -> None:
        from nexus_ai_agent.nagar.ibpc import delta as delta_mod

        for name in ("DeltaAnchor", "DeltaIntentAST", "DeltaOp", "parse_delta_intent"):
            assert name in delta_mod.__all__

    def test_intent_reexports_parse_delta_intent(self) -> None:
        from nexus_ai_agent.nagar.ibpc import intent as intent_mod

        assert "parse_delta_intent" in intent_mod.__all__
        assert callable(intent_mod.parse_delta_intent)

# ---------------------------------------------------------------------------
# Stage 2 hardening (NAGAR-IBPC-TRIAD-CLOSURE-01): fail-closed contract.
# Each test encodes a defect reproduced against the implementation under review.
# ---------------------------------------------------------------------------


def _fc_resolve(text, **overrides):
    kwargs = dict(
        current_scope=_scope(1.0, 4.0),
        project_id="p1",
        asset_id="a1",
        source_revision=1,
        duration_us=DURATION_US,
        channel_binding_id=CBN,
    )
    kwargs.update(overrides)
    return parse_delta_intent(text, **kwargs)


class TestNegationNeverExecutes:
    def test_negated_english_never_executes(self) -> None:
        r = _fc_resolve("don't trim 1 second from the end")
        assert isinstance(r, Refusal) and r.reason == "negated_delta"

    def test_negated_persian_never_executes(self) -> None:
        r = _fc_resolve("یک ثانیه از تهش نبر")
        assert isinstance(r, Refusal) and r.reason == "negated_delta"

    def test_persian_no_is_not_the_number_nine(self) -> None:
        # 'نه' means "no"; the shared numeric policy must leave it untouched,
        # while a real English number word still maps to a digit.
        assert _normalize_word_numbers("نه") == "نه"
        assert _normalize_word_numbers("نه 2 ثانیه") == "نه 2 ثانیه"
        assert _normalize_word_numbers("nine seconds") == "9 seconds"
        r = _fc_resolve("نه، از تهش ببر")
        assert isinstance(r, Refusal) and r.reason == "negated_delta"


class TestContradictionAndMixedAnchors:
    def test_contradictory_trim_and_add_refused(self) -> None:
        r = _fc_resolve("trim 1 second from the end and add 1 second to the end")
        assert isinstance(r, Refusal) and r.reason == "contradictory_delta"

    def test_mixed_start_and_end_refused_not_partially_executed(self) -> None:
        # the second clause names no direction, so the whole request is refused
        r = _fc_resolve("trim 1 second from the start and 2 seconds from the end")
        assert not isinstance(r, DeltaIntentAST)
        assert isinstance(r, Clarification) and r.reason == "ambiguous_delta"

    def test_single_clause_with_both_anchors_refused(self) -> None:
        r = _fc_resolve("trim 1 second from the start or the end")
        assert not isinstance(r, DeltaIntentAST)
        assert isinstance(r, Clarification) and r.reason == "mixed_anchor"

    def test_two_explicit_clauses_resolve_both_ends(self) -> None:
        r = _fc_resolve("trim 1 second from the start and trim 1 second from the end")
        assert isinstance(r, DeltaIntentAST)
        assert [(op.anchor, op.delta_us) for op in r.delta_ops] == [
            (DeltaAnchor.IN_POINT, 1_000_000),
            (DeltaAnchor.OUT_POINT, -1_000_000),
        ]
        assert r.resolved_ast.scope.start_us == 2_000_000
        assert r.resolved_ast.scope.end_us == 3_000_000

    def test_extend_does_not_match_end_anchor(self) -> None:
        # 'extend' contains the substring 'end'; the anchor must be a whole word.
        r = _fc_resolve("extend 1 second at the start")
        assert isinstance(r, DeltaIntentAST)
        assert r.delta_op.anchor == DeltaAnchor.IN_POINT
        assert r.delta_op.delta_us == -1_000_000

    def test_persian_end_anchor_is_not_a_substring_match(self) -> None:
        # 'ته' inside 'گذشته' ("passed") must not be read as the end anchor
        r = _fc_resolve("یک ثانیه از اول گذشته ببر")
        assert not (isinstance(r, DeltaIntentAST) and r.delta_op.anchor == DeltaAnchor.OUT_POINT)


class TestBoundsAndIdentity:
    def test_asset_mismatch_refused(self) -> None:
        r = _fc_resolve(
            "trim 1 more second from the end",
            current_scope=VideoTimeScope(
                asset_id="a2", effect="keep", start_us=1_000_000, end_us=4_000_000
            ),
        )
        assert isinstance(r, Refusal) and r.reason == "asset_mismatch"

    def test_zero_duration_refused_as_unknown(self) -> None:
        r = _fc_resolve("trim 1 more second from the end", duration_us=0)
        assert isinstance(r, Refusal) and r.reason == "unknown_duration"

    def test_scope_beyond_duration_refused(self) -> None:
        r = _fc_resolve(
            "trim 1 more second from the end",
            current_scope=_scope(1.0, 7.0),
            duration_us=6_000_000,
        )
        assert isinstance(r, Refusal) and r.reason == "scope_out_of_bounds"

    def test_negative_source_revision_refused_not_raised(self) -> None:
        r = _fc_resolve("trim 1 more second from the end", source_revision=-5)
        assert isinstance(r, Refusal) and r.reason == "invalid_source_revision"

    def test_invalid_channel_binding_refused_not_raised(self) -> None:
        r = _fc_resolve("trim 1 more second from the end", channel_binding_id="bad")
        assert isinstance(r, Refusal) and r.reason == "invalid_request"

    def test_non_string_text_refused_not_raised(self) -> None:
        r = _fc_resolve(None)  # type: ignore[arg-type]
        assert isinstance(r, Refusal) and r.reason == "invalid_text_type"


class TestPublicTypes:
    def test_return_annotation_is_exact_union(self) -> None:
        hints = _typing.get_type_hints(parse_delta_intent)
        assert hints["return"] == _typing.Union[DeltaIntentAST, Clarification, Refusal]
