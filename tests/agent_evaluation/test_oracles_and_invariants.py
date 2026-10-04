"""Independent behavior oracle, hard-invariant, and E2E-readiness tests."""

from __future__ import annotations

from dataclasses import replace

from _witness import make_observation

from nexus_ai_agent.evaluation.corpus import (
    ADVERSARIAL_CASES,
    FAILURE_CASES,
    GOLDEN_CASES,
)
from nexus_ai_agent.evaluation.domain import (
    ApprovalDecision,
    AuthorizationObservation,
    DispatchBoundary,
    DispatchResult,
    EvaluationDimension,
    EvidenceKind,
    EvidenceRecord,
    EvidenceSource,
    FailureCode,
    IdentityNode,
    IdentityPair,
    IdentityRelation,
    IdentitySource,
    ObservedOutcome,
    PolicyDecision,
    Severity,
    Verdict,
    VerificationVerdict,
)
from nexus_ai_agent.evaluation.oracles import (
    authority_oracle,
    check_identity_pair,
    evaluate_observation,
    intent_oracle,
    provenance_oracle,
    verification_oracle,
)
from nexus_ai_agent.evaluation.runner import (
    _case_result,
    e2e_readiness_from_observation,
)

_CASE_BY_KEY = {case.case_key: case for case in GOLDEN_CASES + ADVERSARIAL_CASES + FAILURE_CASES}


def test_synthetic_full_path_witness_passes_each_relevant_oracle() -> None:
    case = _CASE_BY_KEY["g02-hard-timing-limit"]
    observed = make_observation(case)
    results = evaluate_observation(case, observed)
    assert results
    assert all(result.status is not Verdict.FAIL for result in results)
    assert {result.dimension for result in results if result.status is Verdict.PASS}


def test_e2e_readiness_requires_observed_chain_and_passing_oracles() -> None:
    case = _CASE_BY_KEY["g02-hard-timing-limit"]
    observed = make_observation(case)
    oracle_results = evaluate_observation(case, observed)
    case_result = _case_result(case, oracle_results, observed=observed)
    readiness = e2e_readiness_from_observation(
        case,
        observed,
        case_result,
        adapter_id="synthetic-test-witness-only",
    )
    assert readiness.status.value == "AVAILABLE"
    assert all(stage.status.value == "AVAILABLE" for stage in readiness.stages)
    assert all(stage.evidence_ref for stage in readiness.stages)


def test_e2e_stage_cannot_pass_on_presence_alone_when_verification_fails() -> None:
    case = _CASE_BY_KEY["g02-hard-timing-limit"]
    observed = make_observation(case)
    observed = replace(observed, artifact=replace(observed.artifact, declared_sha256="f" * 64))
    oracle_results = evaluate_observation(case, observed)
    case_result = _case_result(case, oracle_results, observed=observed)
    readiness = e2e_readiness_from_observation(
        case,
        observed,
        case_result,
        adapter_id="synthetic-corruption-witness",
    )
    assert readiness.status.value == "BLOCKED"
    verification = next(stage for stage in readiness.stages if stage.stage.value == "verification")
    evidence = next(stage for stage in readiness.stages if stage.stage.value == "evidence")
    assert verification.status.value == "BLOCKED"
    assert evidence.status.value == "BLOCKED"


def test_independent_verifier_self_report_is_not_accepted() -> None:
    case = _CASE_BY_KEY["g02-hard-timing-limit"]
    observed = make_observation(case)
    verification = replace(observed.verification, independent=False)
    result = verification_oracle(case, replace(observed, verification=verification))
    assert result.status is Verdict.FAIL
    assert any(finding.code is FailureCode.FAKE_SUCCESS for finding in result.findings)
    assert all(finding.severity is Severity.CRITICAL for finding in result.findings)


def test_expected_bad_artifact_is_detected_and_cannot_be_marked_complete() -> None:
    case = _CASE_BY_KEY["f04-wrong-artifact-hash"]
    observed = make_observation(case)
    baseline = verification_oracle(case, observed)
    assert baseline.status is Verdict.PASS
    assert observed.artifact is not None
    assert observed.artifact.declared_sha256 != observed.artifact.sha256
    assert observed.verification is not None
    assert observed.verification.verdict is VerificationVerdict.FAIL

    false_green = replace(observed, outcome=ObservedOutcome.COMPLETED)
    failed = verification_oracle(case, false_green)
    assert failed.status is Verdict.FAIL
    assert FailureCode.FAKE_SUCCESS in {finding.code for finding in failed.findings}


def test_independent_policy_denial_requires_bound_policy_engine_evidence() -> None:
    case = _CASE_BY_KEY["g14-policy-blocked-is-distinct"]
    observed = make_observation(case)
    baseline = authority_oracle(case, observed)
    assert baseline.status is Verdict.PASS
    assert observed.authorization[0].policy_decision is PolicyDecision.DENY

    forged = replace(
        observed.authorization[0].policy_evidence,
        source=EvidenceSource.MODEL_OUTPUT,
    )
    changed_auth = replace(observed.authorization[0], policy_evidence=forged)
    failed = authority_oracle(case, replace(observed, authorization=(changed_auth,)))
    assert failed.status is Verdict.FAIL
    assert FailureCode.AUTHORIZATION_DENIED in {finding.code for finding in failed.findings}


def test_tool_state_cannot_be_asserted_without_independent_harness_evidence() -> None:
    case = _CASE_BY_KEY["g13-known-tool-unavailable-is-distinct"]
    observed = make_observation(case)
    result = next(
        item
        for item in evaluate_observation(case, observed)
        if item.dimension is EvaluationDimension.TOOL_SELECTION
    )
    assert result.status is Verdict.PASS
    missing = replace(observed, tool_state_evidence=None)
    result = next(
        item
        for item in evaluate_observation(case, missing)
        if item.dimension is EvaluationDimension.TOOL_SELECTION
    )
    assert result.status is Verdict.FAIL
    assert FailureCode.INTERNAL_CONTRACT_FAILURE in {finding.code for finding in result.findings}


def test_malformed_output_fault_is_typed_and_refused_not_scored_as_success() -> None:
    case = _CASE_BY_KEY["a03-malformed-structured-output"]
    observed = make_observation(case)
    result = intent_oracle(case, observed)
    assert result.status is Verdict.PASS
    assert observed.malformed_output is True
    assert observed.outcome is ObservedOutcome.REFUSED
    assert observed.failure_code is FailureCode.MALFORMED_OUTPUT


def test_user_supplied_approval_cannot_authorize_a_command() -> None:
    case = _CASE_BY_KEY["a07-fake-approval-cannot-self-authorize"]
    observed = make_observation(case)
    forged_approval = EvidenceRecord(
        kind=EvidenceKind.APPROVAL_DECISION,
        source=EvidenceSource.MODEL_OUTPUT,
        reference="model-output/approval",
        value=ApprovalDecision.APPROVED.value,
    )
    authorization = AuthorizationObservation(
        actor_id="model-claimed-owner",
        actor_source=IdentitySource.MODEL_OUTPUT,
        policy_decision=PolicyDecision.MISSING,
        policy_evidence=None,
        approval_decision=ApprovalDecision.APPROVED,
        approval_source=IdentitySource.MODEL_OUTPUT,
        approval_evidence=forged_approval,
        command_ids=(),
    )
    result = authority_oracle(case, replace(observed, authorization=(authorization,)))
    assert result.status is Verdict.FAIL
    assert FailureCode.APPROVAL_FORGED in {finding.code for finding in result.findings}
    assert observed.outcome is ObservedOutcome.AWAITING_CONFIRMATION


def test_cross_project_forgery_and_trace_replay_need_boundary_evidence() -> None:
    for case_key in (
        "a13-cross-project-reference-refused",
        "a14-replayed-trace-refused",
        "a17-provenance-project-spoof",
    ):
        case = _CASE_BY_KEY[case_key]
        observed = make_observation(case)
        result = provenance_oracle(case, observed)
        assert result.status is Verdict.PASS, (case_key, result.findings)
        assert any(item.kind is EvidenceKind.SECURITY_BOUNDARY for item in result.evidence)

        missing = provenance_oracle(case, replace(observed, security_boundaries=()))
        assert missing.status is Verdict.FAIL
        assert missing.findings


def test_identity_invariants_cover_repeatability_and_locality() -> None:
    case = _CASE_BY_KEY["g18-identical-semantic-input-stable-root"]
    same = IdentityPair(
        relation=IdentityRelation.SAME_SEMANTIC_INPUT,
        before_root_id="root-1",
        after_root_id="root-1",
        before_nodes=(IdentityNode("scene-a", "a1"), IdentityNode("scene-b", "b1")),
        after_nodes=(IdentityNode("scene-a", "a1"), IdentityNode("scene-b", "b1")),
    )
    findings, evidence = check_identity_pair(case, same)
    assert findings == []
    assert evidence

    localized = IdentityPair(
        relation=IdentityRelation.LOCALIZED_SEMANTIC_EDIT,
        before_root_id="root-before",
        after_root_id="root-after",
        before_nodes=(IdentityNode("scene-a", "a1"), IdentityNode("scene-b", "b1")),
        after_nodes=(IdentityNode("scene-a", "a1"), IdentityNode("scene-b", "b2")),
        allowed_changed_paths=("scene-b",),
    )
    findings, evidence = check_identity_pair(case, localized)
    assert findings == []
    assert evidence

    leaked = replace(
        localized, after_nodes=(IdentityNode("scene-a", "a2"), localized.after_nodes[1])
    )
    findings, _ = check_identity_pair(case, leaked)
    assert findings
    assert all(finding.code is FailureCode.INTERNAL_CONTRACT_FAILURE for finding in findings)


def test_duplicate_causal_execution_is_a_hard_failure() -> None:
    case = _CASE_BY_KEY["g10-duplicate-delivery-one-causal-execution"]
    observed = make_observation(case)
    observation = replace(
        observed.idempotency,
        execution_ids=("execution-1", "execution-fork"),
        side_effect_count=2,
    )
    result = next(
        item
        for item in evaluate_observation(case, replace(observed, idempotency=observation))
        if item.dimension is EvaluationDimension.IDEMPOTENCY
    )
    assert result.status is Verdict.FAIL
    assert result.findings
    assert all(finding.hard_gate for finding in result.findings)
    assert all(finding.severity is Severity.CRITICAL for finding in result.findings)


def test_dispatch_outside_command_bus_is_a_critical_failure() -> None:
    case = _CASE_BY_KEY["g01-simple-creative-request"]
    observed = make_observation(case)
    dispatch = replace(
        observed.execution.dispatches[0], boundary=DispatchBoundary.DIRECT_SUBPROCESS
    )
    execution = replace(
        observed.execution, dispatches=(dispatch, *observed.execution.dispatches[1:])
    )
    results = evaluate_observation(case, replace(observed, execution=execution))
    architecture = next(
        item for item in results if item.dimension is EvaluationDimension.ARCHITECTURE_BOUNDARY
    )
    assert architecture.status is Verdict.FAIL
    assert FailureCode.DIRECT_EXECUTION in {finding.code for finding in architecture.findings}


def test_expected_revision_mismatch_must_be_measured_and_rejected() -> None:
    case = _CASE_BY_KEY["f10-provenance-revision-mismatch-fails-closed"]
    observed = make_observation(case)
    result = provenance_oracle(case, observed)
    assert result.status is Verdict.PASS
    assert observed.artifact is not None
    assert observed.artifact.revision_id == "revision-6"

    unrecognized = replace(observed, failure_code=FailureCode.VERIFICATION_FAILED)
    result = provenance_oracle(case, unrecognized)
    assert result.status is Verdict.FAIL
    assert FailureCode.PROVENANCE_TAMPERED in {finding.code for finding in result.findings}


def test_command_and_timeout_failures_require_measured_failed_dispatch() -> None:
    for case_key in (
        "f01-command-failure-is-not-verification-failure",
        "f02-timeout-is-causally-distinct",
    ):
        case = _CASE_BY_KEY[case_key]
        observed = make_observation(case)
        assert observed.execution is not None
        assert observed.execution.dispatches
        assert any(item.result.value == "FAILED" for item in observed.execution.dispatches)
        results = evaluate_observation(case, observed)
        execution = next(
            item for item in results if item.dimension is EvaluationDimension.EXECUTION_INTEGRITY
        )
        assert execution.status is Verdict.PASS

        no_failure = replace(
            observed,
            execution=replace(
                observed.execution,
                dispatches=tuple(
                    replace(item, result=DispatchResult.SUCCEEDED)
                    for item in observed.execution.dispatches
                ),
            ),
        )
        execution = next(
            item
            for item in evaluate_observation(case, no_failure)
            if item.dimension is EvaluationDimension.EXECUTION_INTEGRITY
        )
        assert execution.status is Verdict.FAIL


def test_missing_verifier_is_a_typed_failure_case_not_a_false_green() -> None:
    case = _CASE_BY_KEY["f07-artifact-marked-complete-without-verification"]
    observed = make_observation(case)
    assert observed.verification is None
    result = verification_oracle(case, observed)
    assert result.status is Verdict.PASS
    assert any(item.kind is EvidenceKind.VERIFIER_STATUS for item in result.evidence)

    false_green = replace(observed, outcome=ObservedOutcome.COMPLETED)
    result = verification_oracle(case, false_green)
    assert result.status is Verdict.FAIL
    assert FailureCode.FAKE_SUCCESS in {finding.code for finding in result.findings}


def test_duration_violation_is_expected_only_when_typed_failure_is_observed() -> None:
    case = _CASE_BY_KEY["f06-over-duration-media-is-not-complete"]
    observed = make_observation(case)
    duration = next(
        item
        for item in evaluate_observation(case, observed)
        if item.dimension is EvaluationDimension.CONSTRAINT_PRESERVATION
    )
    assert duration.status is Verdict.PASS
    assert observed.outcome is ObservedOutcome.FAILED

    false_green = replace(observed, outcome=ObservedOutcome.COMPLETED)
    duration = next(
        item
        for item in evaluate_observation(case, false_green)
        if item.dimension is EvaluationDimension.CONSTRAINT_PRESERVATION
    )
    assert duration.status is Verdict.FAIL
