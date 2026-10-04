"""Evaluation runner and the blocked-ready E2E adapter boundary.

The runner never manufactures observations. It only calls an explicitly
injected test adapter and evaluates the returned typed observations. With no
adapter, every real-subject case is BLOCKED and the fail-closed machine gate
returns FAIL.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Protocol

from nexus_ai_agent.evaluation.domain import (
    BlockedCondition,
    CaseKind,
    DimensionResult,
    E2EReadiness,
    E2EStage,
    E2EStageReadiness,
    E2EStageStatus,
    EvaluationCase,
    EvaluationDimension,
    EvaluationInput,
    EvaluationResult,
    EvaluationRun,
    EvaluationSuite,
    EvidenceKind,
    EvidenceRecord,
    EvidenceSource,
    ExpectedDecision,
    FailureCode,
    FailureFinding,
    GateVerdict,
    ObservedBehavior,
    OracleResult,
    PipelineStage,
    Severity,
    Verdict,
    aggregate_dimensions,
    is_subject_sha,
)
from nexus_ai_agent.evaluation.oracles import ORACLE_VERSIONS, evaluate_observation


class SubjectAdapter(Protocol):
    """Approved test seam implemented outside the evaluator.

    A future adapter must collect facts from an isolated test subject boundary;
    it must not provide a verdict or substitute self-reported success for
    policy, CommandBus, verifier, persistence, or artifact evidence.
    """

    @property
    def adapter_id(self) -> str: ...

    def observe(self, evaluation_input: EvaluationInput) -> ObservedBehavior: ...


def blocked_e2e_readiness(
    reason: str,
    *,
    adapter_id: str = "unregistered",
) -> E2EReadiness:
    return E2EReadiness(
        adapter_id=adapter_id,
        stages=tuple(
            E2EStageReadiness(
                stage=stage,
                status=E2EStageStatus.BLOCKED,
                evidence_ref="",
                reason=reason,
            )
            for stage in E2EStage
        ),
    )


def _dedupe_evidence(
    records: tuple[EvidenceRecord, ...] | list[EvidenceRecord],
) -> tuple[EvidenceRecord, ...]:
    by_id = {record.evidence_id: record for record in records}
    return tuple(by_id[key] for key in sorted(by_id))


def _dimension_results(oracle_results: tuple[OracleResult, ...]) -> tuple[DimensionResult, ...]:
    by_dimension: dict[EvaluationDimension, list[OracleResult]] = defaultdict(list)
    for result in oracle_results:
        by_dimension[result.dimension].append(result)

    summaries: list[DimensionResult] = []
    for dimension in EvaluationDimension:
        results = by_dimension.get(dimension, [])
        counts = {
            Verdict.PASS: sum(item.status is Verdict.PASS for item in results),
            Verdict.FAIL: sum(item.status is Verdict.FAIL for item in results),
            Verdict.NOT_APPLICABLE: sum(item.status is Verdict.NOT_APPLICABLE for item in results),
            Verdict.BLOCKED: sum(item.status is Verdict.BLOCKED for item in results),
        }
        if counts[Verdict.FAIL]:
            status = Verdict.FAIL
        elif counts[Verdict.BLOCKED]:
            status = Verdict.BLOCKED
        elif counts[Verdict.PASS]:
            status = Verdict.PASS
        else:
            status = Verdict.NOT_APPLICABLE
        summaries.append(
            DimensionResult(
                dimension=dimension,
                status=status,
                passed=counts[Verdict.PASS],
                failed=counts[Verdict.FAIL],
                not_applicable=counts[Verdict.NOT_APPLICABLE],
                blocked=counts[Verdict.BLOCKED],
            )
        )
    return tuple(summaries)


def _case_result(
    case: EvaluationCase,
    oracle_results: tuple[OracleResult, ...],
    *,
    observed: ObservedBehavior | None,
    blocked_conditions: tuple[BlockedCondition, ...] = (),
) -> EvaluationResult:
    findings = tuple(finding for result in oracle_results for finding in result.findings)
    status = (
        Verdict.FAIL
        if any(result.status is Verdict.FAIL for result in oracle_results)
        else Verdict.BLOCKED
        if blocked_conditions or any(result.status is Verdict.BLOCKED for result in oracle_results)
        else Verdict.PASS
        if any(result.status is Verdict.PASS for result in oracle_results)
        else Verdict.NOT_APPLICABLE
    )
    evidence = _dedupe_evidence([record for result in oracle_results for record in result.evidence])
    return EvaluationResult(
        case_id=case.case_id,
        case_key=case.case_key,
        kind=case.kind,
        evaluation_input=case.evaluation_input,
        expected=case.expected,
        observed=observed,
        status=status,
        oracle_results=oracle_results,
        dimension_results=_dimension_results(oracle_results),
        findings=findings,
        evidence=evidence,
        blocked_conditions=blocked_conditions,
    )


def _blocked_case_result(case: EvaluationCase, reason: str) -> EvaluationResult:
    conditions = tuple(
        BlockedCondition(
            code="SUBJECT_ADAPTER_UNAVAILABLE",
            dimension=dimension,
            reason=reason,
        )
        for dimension in EvaluationDimension
        if dimension is not EvaluationDimension.E2E_READINESS
    )
    dimensions = tuple(
        DimensionResult(dimension=item.dimension, status=Verdict.BLOCKED, blocked=1)
        for item in conditions
    )
    return EvaluationResult(
        case_id=case.case_id,
        case_key=case.case_key,
        kind=case.kind,
        evaluation_input=case.evaluation_input,
        expected=case.expected,
        observed=None,
        status=Verdict.BLOCKED,
        oracle_results=(),
        dimension_results=dimensions,
        findings=(),
        evidence=(),
        blocked_conditions=conditions,
    )


def _adapter_error_result(
    case: EvaluationCase,
    error: Exception,
    *,
    phase: str = "adapter-observation",
) -> EvaluationResult:
    code = (
        FailureCode.TIMEOUT
        if phase == "adapter-observation" and isinstance(error, TimeoutError)
        else FailureCode.INTERNAL_CONTRACT_FAILURE
    )
    evidence = EvidenceRecord(
        kind=EvidenceKind.EXECUTION_RESULT,
        source=EvidenceSource.TEST_HARNESS,
        reference=f"{case.case_id}/subject-adapter/{phase}",
        value=f"{phase}_exception={type(error).__name__}",
    )
    finding = FailureFinding(
        case_id=case.case_id,
        oracle_id="subject-adapter-boundary-v1",
        dimension=EvaluationDimension.EXECUTION_INTEGRITY,
        code=code,
        severity=Severity.CRITICAL,
        hard_gate=True,
        detail=(f"The {phase} boundary raised; no subject PASS was accepted."),
        evidence_ids=(evidence.evidence_id,),
    )
    oracle_result = OracleResult(
        case_id=case.case_id,
        oracle_id="subject-adapter-boundary-v1",
        oracle_version="1.0.0",
        dimension=EvaluationDimension.EXECUTION_INTEGRITY,
        status=Verdict.FAIL,
        findings=(finding,),
        evidence=(evidence,),
    )
    return _case_result(case, (oracle_result,), observed=None)


def _stage(
    stage: E2EStage,
    available: bool,
    reason: str,
    evidence_ref: str = "",
) -> E2EStageReadiness:
    return E2EStageReadiness(
        stage=stage,
        status=E2EStageStatus.AVAILABLE if available else E2EStageStatus.BLOCKED,
        evidence_ref=evidence_ref,
        reason="observed typed evidence" if available else reason,
    )


def e2e_readiness_from_observation(
    case: EvaluationCase,
    observed: ObservedBehavior,
    result: EvaluationResult,
    *,
    adapter_id: str,
) -> E2EReadiness:
    """Derive stage readiness from observations, never from adapter verdict flags."""
    provenance = {link.stage: link for link in observed.provenance}
    policy_evidence = [
        auth.policy_evidence
        for auth in observed.authorization
        if auth.policy_evidence is not None
        and auth.policy_evidence.source is EvidenceSource.POLICY_ENGINE
    ]
    bus_dispatches = [
        dispatch
        for dispatch in (observed.execution.dispatches if observed.execution else ())
        if dispatch.boundary.value == "command_bus"
    ]
    verification_passed = bool(
        observed.verification
        and observed.verification.independent
        and observed.verification.verdict.value == "PASS"
        and any(
            record.source is EvidenceSource.INDEPENDENT_VERIFIER
            for record in observed.verification.evidence
        )
    )
    dimension_status = {item.dimension: item.status for item in result.dimension_results}
    provenance_passed = (
        dimension_status.get(EvaluationDimension.PROVENANCE_INTEGRITY) is Verdict.PASS
    )
    stages = (
        _stage(
            E2EStage.REQUEST,
            observed.observed_request_id == case.evaluation_input.request_id
            and PipelineStage.REQUEST in provenance
            and provenance_passed,
            "accepted request identity was not observed at ingress",
            provenance[PipelineStage.REQUEST].object_id
            if PipelineStage.REQUEST in provenance
            else "",
        ),
        _stage(
            E2EStage.TYPED_INTENT,
            observed.intent is not None
            and PipelineStage.INTENT in provenance
            and provenance_passed
            and dimension_status.get(EvaluationDimension.INTENT_CORRECTNESS) is Verdict.PASS,
            "typed intent and its causal provenance were not observed",
            provenance[PipelineStage.INTENT].object_id
            if PipelineStage.INTENT in provenance
            else "",
        ),
        _stage(
            E2EStage.STRATEGY,
            observed.strategy is not None
            and PipelineStage.STRATEGY in provenance
            and provenance_passed
            and dimension_status.get(EvaluationDimension.STRATEGY_VALIDITY) is Verdict.PASS,
            "typed strategy and its causal provenance were not observed",
            provenance[PipelineStage.STRATEGY].object_id
            if PipelineStage.STRATEGY in provenance
            else "",
        ),
        _stage(
            E2EStage.CREATIVE_IR,
            observed.creative_ir is not None
            and PipelineStage.CREATIVE_IR in provenance
            and provenance_passed
            and dimension_status.get(EvaluationDimension.IR_VALIDITY) is Verdict.PASS,
            "Creative IR and its sealed identity/provenance were not observed",
            provenance[PipelineStage.CREATIVE_IR].object_id
            if PipelineStage.CREATIVE_IR in provenance
            else "",
        ),
        _stage(
            E2EStage.PLAN,
            observed.plan is not None
            and PipelineStage.PLAN in provenance
            and provenance_passed
            and dimension_status.get(EvaluationDimension.COMPILER_DETERMINISM) is Verdict.PASS,
            "deterministic executable plan and its provenance were not observed",
            observed.plan.plan_id if observed.plan else "",
        ),
        _stage(
            E2EStage.AUTHORIZATION,
            bool(policy_evidence)
            and PipelineStage.AUTHORIZATION in provenance
            and provenance_passed
            and dimension_status.get(EvaluationDimension.AUTHORIZATION_INTEGRITY) is Verdict.PASS,
            "independent policy decision evidence was not observed",
            policy_evidence[0].evidence_id if policy_evidence else "",
        ),
        _stage(
            E2EStage.COMMAND_BUS,
            bool(bus_dispatches)
            and PipelineStage.COMMAND in provenance
            and provenance_passed
            and dimension_status.get(EvaluationDimension.ARCHITECTURE_BOUNDARY) is Verdict.PASS
            and dimension_status.get(EvaluationDimension.AUTHORIZATION_INTEGRITY) is Verdict.PASS,
            "canonical CommandBus dispatch was not observed",
            bus_dispatches[0].command_id if bus_dispatches else "",
        ),
        _stage(
            E2EStage.EXECUTION,
            observed.execution is not None
            and observed.execution.completed
            and PipelineStage.EXECUTION in provenance
            and provenance_passed
            and dimension_status.get(EvaluationDimension.EXECUTION_INTEGRITY) is Verdict.PASS,
            "completed runtime execution evidence was not observed",
            observed.execution.execution_id if observed.execution else "",
        ),
        _stage(
            E2EStage.VERIFICATION,
            verification_passed
            and PipelineStage.VERIFICATION in provenance
            and provenance_passed
            and dimension_status.get(EvaluationDimension.VERIFICATION_INTEGRITY) is Verdict.PASS,
            "independent verifier evidence was not observed",
            observed.verification.verifier_id if observed.verification else "",
        ),
        _stage(
            E2EStage.EVIDENCE,
            bool(result.evidence)
            and PipelineStage.RECEIPT in provenance
            and provenance_passed
            and result.status is Verdict.PASS
            and not any(item.hard_gate for item in result.findings),
            "reproducible evidence and durable receipt were not observed",
            provenance[PipelineStage.RECEIPT].object_id
            if PipelineStage.RECEIPT in provenance
            else "",
        ),
    )
    return E2EReadiness(adapter_id=adapter_id, stages=stages)


def _select_e2e_case(suite: EvaluationSuite) -> EvaluationCase:
    full_path = {
        PipelineStage.REQUEST,
        PipelineStage.INTENT,
        PipelineStage.STRATEGY,
        PipelineStage.CREATIVE_IR,
        PipelineStage.PLAN,
        PipelineStage.AUTHORIZATION,
        PipelineStage.COMMAND,
        PipelineStage.EXECUTION,
        PipelineStage.VERIFICATION,
        PipelineStage.RECEIPT,
    }
    candidates = [
        case
        for case in suite.cases
        if case.kind is CaseKind.GOLDEN
        and case.expected.decision is ExpectedDecision.ACCEPT
        and case.expected.require_independent_verification
        and full_path.issubset(set(case.expected.required_provenance_stages))
    ]
    if not candidates:
        raise ValueError("suite has no canonical full-vertical-slice E2E case")
    return candidates[0]


def run_suite(
    suite: EvaluationSuite,
    *,
    subject_sha: str,
    adapter: SubjectAdapter | None = None,
) -> EvaluationRun:
    """Run the oracle suite against an explicitly injected test adapter.

    No adapter means BLOCKED case observations and a FAIL gate. This prevents a
    local fixture, model self-report, or placeholder from masquerading as an
    end-to-end Agent result.
    """
    if not is_subject_sha(subject_sha):
        raise ValueError("subject_sha must be a 40- or 64-character lowercase commit digest")

    adapter_id = adapter.adapter_id if adapter is not None else "unregistered"
    if adapter is not None and (not isinstance(adapter_id, str) or not adapter_id.strip()):
        raise ValueError("subject adapter must expose a stable, non-empty adapter_id")
    results: list[EvaluationResult] = []
    blocked_conditions: list[BlockedCondition] = []
    observations: dict[str, ObservedBehavior] = {}

    if adapter is None:
        reason = (
            "No approved Arena A runtime adapter is registered; "
            "runtime behavior was not fabricated."
        )
        results = [_blocked_case_result(case, reason) for case in suite.cases]
        blocked_conditions.append(
            BlockedCondition(
                code="E2E_ADAPTER_UNAVAILABLE",
                dimension=EvaluationDimension.E2E_READINESS,
                reason=reason,
            )
        )
        readiness = blocked_e2e_readiness(reason)
    else:
        for case in suite.cases:
            try:
                observation = adapter.observe(case.evaluation_input)
                if not isinstance(observation, ObservedBehavior):
                    raise TypeError("subject adapter must return ObservedBehavior")
            except Exception as error:  # noqa: BLE001 - converted to typed adapter-boundary evidence
                results.append(_adapter_error_result(case, error))
                continue
            try:
                oracle_results = evaluate_observation(case, observation)
            except Exception as error:  # noqa: BLE001 - malformed observations fail closed
                results.append(_adapter_error_result(case, error, phase="oracle-evaluation"))
                continue
            observations[case.case_id] = observation
            results.append(_case_result(case, oracle_results, observed=observation))

        e2e_case = _select_e2e_case(suite)
        e2e_observation = observations.get(e2e_case.case_id)
        e2e_result = next(item for item in results if item.case_id == e2e_case.case_id)
        if e2e_observation is None:
            reason = "The full-path case produced no typed observation from the adapter."
            readiness = blocked_e2e_readiness(reason, adapter_id=adapter_id)
            blocked_conditions.append(
                BlockedCondition(
                    code="E2E_OBSERVATION_MISSING",
                    dimension=EvaluationDimension.E2E_READINESS,
                    reason=reason,
                )
            )
        else:
            readiness = e2e_readiness_from_observation(
                e2e_case,
                e2e_observation,
                e2e_result,
                adapter_id=adapter_id,
            )
            if readiness.status is not E2EStageStatus.AVAILABLE:
                missing = [
                    item.stage.value
                    for item in readiness.stages
                    if item.status is not E2EStageStatus.AVAILABLE
                ]
                blocked_conditions.append(
                    BlockedCondition(
                        code="E2E_CHAIN_INCOMPLETE",
                        dimension=EvaluationDimension.E2E_READINESS,
                        reason="Required vertical-slice observations missing: "
                        + ", ".join(missing),
                    )
                )

    all_results = tuple(results)
    hard = tuple(
        finding for result in all_results for finding in result.findings if finding.hard_gate
    )
    hard_by_id = {item.finding_id: item for item in hard}
    hard_failures_tuple = tuple(hard_by_id[key] for key in sorted(hard_by_id))
    dimensions = list(aggregate_dimensions(all_results))
    e2e_dimension_status = (
        Verdict.PASS if readiness.status is E2EStageStatus.AVAILABLE else Verdict.BLOCKED
    )
    dimensions = [
        DimensionResult(
            dimension=item.dimension,
            status=e2e_dimension_status,
            passed=int(e2e_dimension_status is Verdict.PASS),
            blocked=int(e2e_dimension_status is Verdict.BLOCKED),
        )
        if item.dimension is EvaluationDimension.E2E_READINESS
        else item
        for item in dimensions
    ]
    dimension_results = tuple(dimensions)
    dimensions_by_key = {item.dimension: item for item in dimension_results}
    required_blocked = [
        dimension
        for dimension in suite.required_dimensions
        if dimensions_by_key.get(
            dimension, DimensionResult(dimension, Verdict.NOT_APPLICABLE)
        ).status
        is not Verdict.PASS
    ]
    gate = GateVerdict.PASS
    if hard_failures_tuple or blocked_conditions or required_blocked:
        gate = GateVerdict.FAIL
    if any(result.status is Verdict.FAIL for result in all_results):
        gate = GateVerdict.FAIL

    return EvaluationRun(
        suite_id=suite.suite_id,
        suite_version=suite.version,
        subject_sha=subject_sha,
        adapter_id=adapter_id,
        results=all_results,
        e2e_readiness=readiness,
        gate_verdict=gate,
        hard_failures=hard_failures_tuple,
        blocked_conditions=tuple(blocked_conditions),
        oracle_versions=ORACLE_VERSIONS,
    )
