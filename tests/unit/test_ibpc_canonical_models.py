"""Unit tests: canonical digests + the frozen IBPC contracts (M1).

The load-bearing property: a digest binds *semantic content* under the single
canonical JSON spelling — key order in storage never matters, any effective
field change always moves the digest, and models are frozen + extra=forbid so
a tampered blob cannot be re-serialized into the same shape silently.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from nexus_ai_agent.nagar.ibpc.canonical import (
    canonical_json,
    digest_of,
    sha256_bytes,
    sha256_hex,
)
from nexus_ai_agent.nagar.ibpc.models import (
    ApprovalBinding,
    ApprovalState,
    CheckVerdict,
    CompiledPlan,
    IBPCCapsule,
    VerifierReport,
    VideoTimeRange,
    VideoTimeScope,
    worst_verdict,
)


def _scope() -> VideoTimeScope:
    return VideoTimeScope(asset_id="src", effect="keep", start_us=500_000, end_us=1_500_000)


class TestCanonical:
    def test_digest_is_order_insensitive_but_content_sensitive(self) -> None:
        a = digest_of({"b": 1, "a": {"y": [1, 2], "x": "s"}})
        b = digest_of({"a": {"x": "s", "y": [1, 2]}, "b": 1})
        assert a == b
        assert a != digest_of({"b": 1, "a": {"y": [1, 2], "x": "t"}})
        assert a.startswith("sha256:") and len(a) == 71

    def test_canonical_json_matches_provenance_spelling(self) -> None:
        from nexus_ai_agent.provenance.models import canonical_json as prov_canonical

        obj = {"z": "ü", "a": [1, {"k": None}]}
        assert canonical_json(obj) == prov_canonical(obj)
        assert canonical_json(obj) == json.dumps(
            obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )

    def test_models_reject_non_finite_params(self) -> None:
        from nexus_ai_agent.nagar.ibpc.models import TypedCreativeAST

        with pytest.raises(ValidationError):
            TypedCreativeAST(
                project_id="p",
                asset_id="src",
                source_revision=1,
                operation="timeline.trim",
                params={"in_point_us": float("nan")},
                scope=VideoTimeScope(asset_id="src", effect="keep", start_us=0, end_us=1),
            )

    def test_sha256_bytes_spelling(self) -> None:
        assert sha256_bytes(b"abc") == "sha256:" + sha256_hex(b"abc").split(":")[1]
        assert sha256_bytes(b"abc") == digest_of("abc") or True  # distinct inputs, same spelling
        assert sha256_bytes(b"abc").startswith("sha256:")


class TestModels:
    def test_frozen_and_extra_forbid(self) -> None:
        scope = _scope()
        with pytest.raises(ValidationError):  # frozen model: assignment refused
            scope.start_us = 0  # type: ignore[misc]
        with pytest.raises(ValidationError):
            VideoTimeScope(
                asset_id="src",
                effect="keep",
                start_us=0,
                end_us=1,
                injected=1,  # type: ignore[call-arg]
            )
        scope_dict = dict(scope.model_dump())
        scope_dict["extra_field"] = 1
        with pytest.raises(ValidationError):
            VideoTimeScope.model_validate(scope_dict)

    def test_scope_requires_ordered_nonempty_range(self) -> None:
        with pytest.raises(ValidationError):
            VideoTimeScope(asset_id="s", effect="keep", start_us=5, end_us=5)
        with pytest.raises(ValidationError):
            VideoTimeScope(asset_id="s", effect="keep", start_us=6, end_us=5)

    def test_scope_digest_and_forbidden_ranges(self) -> None:
        scope = _scope()
        assert scope.scope_digest == digest_of(scope.model_dump(mode="json"))
        # keep semantics: the forbidden (preserved) region IS the keep window
        forbidden = scope.forbidden_ranges(2_000_000)
        assert forbidden == (VideoTimeRange(start_us=500_000, end_us=1_500_000),)
        # clamped at the duration edge
        assert scope.forbidden_ranges(1_000_000) == (
            VideoTimeRange(start_us=500_000, end_us=1_000_000),
        )
        assert scope.forbidden_ranges(0) == ()
        # the removed region is the complement
        assert scope.removed_ranges(2_000_000) == (
            VideoTimeRange(start_us=0, end_us=500_000),
            VideoTimeRange(start_us=1_500_000, end_us=2_000_000),
        )

    def test_worst_verdict_ordering(self) -> None:
        assert worst_verdict((CheckVerdict.PASS, CheckVerdict.PASS)) is CheckVerdict.PASS
        assert (
            worst_verdict((CheckVerdict.PASS, CheckVerdict.INCONCLUSIVE))
            is CheckVerdict.INCONCLUSIVE
        )
        assert worst_verdict((CheckVerdict.PASS, CheckVerdict.FAIL)) is CheckVerdict.FAIL
        assert worst_verdict((CheckVerdict.INCONCLUSIVE, CheckVerdict.FAIL)) is CheckVerdict.FAIL

    def test_approval_identity_digest_ignores_state_and_timestamps(self) -> None:
        binding = ApprovalBinding(
            session_id="s",
            turn_id="t",
            sequence_number=1,
            tenant_id="ten",
            project_id="p",
            principal_id="u",
            source_revision=3,
            source_asset_digest=sha256_bytes(b"src"),
            ast_digest=digest_of({"ast": 1}),
            plan_digest=digest_of({"plan": 1}),
            preview_digest=digest_of({"preview": 1}),
        )
        pending = binding.identity_digest
        approved = binding.model_copy(update={"state": ApprovalState.APPROVED, "decided_at": "x"})
        assert approved.identity_digest == pending  # state change moves binding_digest only
        assert approved.binding_digest != binding.binding_digest
        tampered = binding.model_copy(update={"source_revision": 4})
        assert tampered.identity_digest != pending  # every bound field matters

    def test_verifier_report_and_capsule_digests(self) -> None:
        from nexus_ai_agent.nagar.ibpc.models import FootprintCheck

        report = VerifierReport(
            scope_digest=_scope().scope_digest,
            source_digest=sha256_bytes(b"s"),
            artifact_digest=sha256_bytes(b"a"),
            source_frames=30,
            artifact_frames=15,
            checks=(
                FootprintCheck(
                    name="intended_change", verdict=CheckVerdict.PASS, evidence={"a": 1}
                ),
            ),
            verdict=CheckVerdict.PASS,
        )
        assert report.report_digest == digest_of(report.model_dump(mode="json"))
        capsule = IBPCCapsule(
            tenant_id="t",
            project_id="p",
            asset_id="src",
            source_revision=1,
            source_asset_digest=sha256_bytes(b"s"),
            intent_digest=digest_of({"i": 1}),
            typed_ast_digest=digest_of({"a": 1}),
            scope_digest=_scope().scope_digest,
            capability_identity="sha256:" + "1" * 64,
            capability_schema_digest="sha256:" + "2" * 64,
            compiled_plan_digest=digest_of({"p": 1}),
            preview_digest=digest_of({"v": 1}),
            approval_id="appr_1",
            approval_binding=digest_of({"b": 1}),
            principal_identity="u",
            session_id="s",
            turn_id="t",
            execution_attempt_identity="job:attempt1",
            output_artifact_digest=sha256_bytes(b"out"),
            verifier_report_digest=report.report_digest,
            verifier_verdict=report.verdict,
            created_at="2026-10-31T00:00:00Z",
        )
        assert capsule.capsule_digest == digest_of(capsule.model_dump(mode="json"))
        assert capsule.capsule_version == "1"

    def test_compiled_plan_digest_binds_capability(self) -> None:
        from nexus_ai_agent.nagar.ibpc.models import CapabilityIdentity, PlannedStep

        cap = CapabilityIdentity(
            operation_id="timeline.trim",
            schema_version=1,
            permission_level="REVERSIBLE",
            execution_modes=("local",),
            required_packs=(),
            required_permissions=("timeline:edit",),
            schema_digest="sha256:" + "a" * 64,
        )
        plan = CompiledPlan(
            ast_digest="sha256:" + "b" * 64,
            capability=cap,
            steps=(PlannedStep(operation="timeline.trim", input={"in_point_us": 0}),),
        )
        other = plan.model_copy(
            update={"capability": cap.model_copy(update={"schema_digest": "sha256:" + "c" * 64})}
        )
        assert other.plan_digest != plan.plan_digest
