"""Synthetic typed witnesses for oracle unit tests only; never live E2E evidence."""

from __future__ import annotations

import hashlib
from dataclasses import replace

from nexus_ai_agent.evaluation.domain import (
    AdversarialClass,
    ApprovalDecision,
    ArgumentObservation,
    ArtifactObservation,
    ArtifactViolation,
    AuthorizationObservation,
    CommandObservation,
    CreativeIRObservation,
    DeliveryObservation,
    DispatchBoundary,
    DispatchObservation,
    DispatchResult,
    EvidenceKind,
    EvidenceRecord,
    EvidenceSource,
    ExecutionObservation,
    FailureCode,
    IdempotencyObservation,
    IdentityNode,
    IdentityPair,
    IdentityRelation,
    IdentitySource,
    IntentObservation,
    ObservedBehavior,
    ObservedOutcome,
    PipelineStage,
    PlanObservation,
    PolicyDecision,
    ProvenanceLink,
    RecoveryObservation,
    ReplanAttempt,
    ReplanObservation,
    RequirementStageMark,
    RequirementState,
    RequirementTrace,
    SecurityBoundaryKind,
    SecurityBoundaryObservation,
    StrategyObservation,
    VerificationObservation,
    VerificationVerdict,
)
from nexus_ai_agent.evaluation.oracles import (
    canonical_plan_bytes,
    expected_command_id,
    expected_plan_id,
)

_STAGE_SOURCES = {
    PipelineStage.REQUEST: EvidenceSource.USER_INPUT,
    PipelineStage.INTENT: EvidenceSource.MODEL_OUTPUT,
    PipelineStage.STRATEGY: EvidenceSource.MODEL_OUTPUT,
    PipelineStage.CREATIVE_IR: EvidenceSource.MODEL_OUTPUT,
    PipelineStage.PLAN: EvidenceSource.MODEL_OUTPUT,
    PipelineStage.AUTHORIZATION: EvidenceSource.POLICY_ENGINE,
    PipelineStage.COMMAND: EvidenceSource.COMMAND_BUS,
    PipelineStage.TRANSACTION: EvidenceSource.COMMAND_BUS,
    PipelineStage.EXECUTION: EvidenceSource.COMMAND_BUS,
    PipelineStage.ARTIFACT: EvidenceSource.PERSISTENCE,
    PipelineStage.VERIFICATION: EvidenceSource.INDEPENDENT_VERIFIER,
    PipelineStage.RECEIPT: EvidenceSource.PERSISTENCE,
}


def _argument_value(case, argument_name: str, argument_type) -> str:
    for requirement in case.expected.requirements:
        name, separator, value = requirement.value.partition("=")
        if separator and name == argument_name:
            return value
    if argument_name == "at":
        return '{"anchor":"playhead"}'
    if argument_name == "label":
        return "Opening"
    if argument_type.value == "boolean":
        return "false"
    if argument_type.value == "integer":
        return "0"
    if argument_type.value == "decimal_string":
        return "0"
    if argument_type.value == "sha256":
        return "a" * 64
    if argument_type.value == "canonical_json":
        return "{}"
    return "test-value"


def _plan_for_case(case) -> PlanObservation:
    commands = []
    for contract in case.expected.capabilities:
        arguments = tuple(
            ArgumentObservation(
                name=argument.name,
                value_type=argument.value_type,
                canonical_value=_argument_value(case, argument.name, argument.value_type),
            )
            for argument in contract.arguments
            if argument.required
        )
        dependencies = (commands[-1].command_id,) if commands else ()
        command = CommandObservation(
            command_id="pending",
            capability_id=contract.capability_id,
            arguments=arguments,
            dependencies=dependencies,
        )
        command = replace(
            command,
            command_id=expected_command_id(
                case.evaluation_input.request_id, len(commands), command
            ),
        )
        commands.append(command)
    typed_commands = tuple(commands)
    return PlanObservation(
        plan_id=expected_plan_id(case.evaluation_input.request_id, typed_commands),
        commands=typed_commands,
        canonical_bytes=canonical_plan_bytes(case.evaluation_input.request_id, typed_commands),
    )


def _constraint_traces(case) -> tuple[RequirementTrace, ...]:
    traces = []
    for requirement in case.expected.requirements:
        marks = tuple(
            RequirementStageMark(
                stage=stage,
                state=(
                    RequirementState.LOST
                    if ArtifactViolation.DURATION_EXCEEDED
                    in case.expected.expected_artifact_violations
                    and stage is PipelineStage.VERIFICATION
                    else RequirementState.PRESERVED
                ),
                evidence_ref=f"{case.evaluation_input.request_id}/{requirement.requirement_id}/{stage.value}",
                evidence=EvidenceRecord(
                    kind=EvidenceKind.REQUIREMENT_STAGE,
                    source=EvidenceSource.TEST_HARNESS,
                    reference=f"{case.evaluation_input.request_id}/{requirement.requirement_id}/{stage.value}",
                    value=RequirementState.PRESERVED.value,
                ),
            )
            for stage in requirement.required_stages
        )
        traces.append(RequirementTrace(requirement_id=requirement.requirement_id, marks=marks))
    return tuple(traces)


def _identity_pair(relation: IdentityRelation) -> IdentityPair:
    before_nodes = (
        IdentityNode(path="scene-a", identity="node-a-v1"),
        IdentityNode(path="scene-b", identity="node-b-v1"),
        IdentityNode(path="scene-c", identity="node-c-v1"),
    )
    if relation is IdentityRelation.SAME_SEMANTIC_INPUT:
        return IdentityPair(
            relation=relation,
            before_root_id="root-stable",
            after_root_id="root-stable",
            before_nodes=before_nodes,
            after_nodes=before_nodes,
        )
    after_nodes = (
        before_nodes[0],
        IdentityNode(path="scene-b", identity="node-b-v2"),
        before_nodes[2],
    )
    return IdentityPair(
        relation=relation,
        before_root_id="root-before",
        after_root_id="root-after",
        before_nodes=before_nodes,
        after_nodes=after_nodes,
        allowed_changed_paths=("scene-b",),
    )


def make_observation(case) -> ObservedBehavior:
    """Build controlled data for oracle tests; it does not call a subject."""
    expected = case.expected
    outcome = (
        expected.expected_outcome
        or {
            "ACCEPT": ObservedOutcome.EXECUTED,
            "REFUSE": ObservedOutcome.REFUSED,
            "CLARIFY": ObservedOutcome.NEEDS_CLARIFICATION,
            "REQUIRE_CONFIRMATION": ObservedOutcome.AWAITING_CONFIRMATION,
        }[expected.decision.value]
    )
    expected_execution_count = expected.expected_execution_count
    will_execute = expected_execution_count is None or expected_execution_count > 0
    failed_attempt = outcome is ObservedOutcome.FAILED and expected.failure_code in {
        FailureCode.EXECUTION_FAILED,
        FailureCode.TIMEOUT,
    }
    will_dispatch = will_execute or failed_attempt
    requires_plan = will_dispatch or PipelineStage.PLAN in expected.required_provenance_stages
    plan = _plan_for_case(case) if requires_plan and expected.capabilities else None
    commands = plan.commands if plan else ()

    intent = None
    if expected.failure_code not in {None} and expected.failure_code.value in {
        "INVALID_INPUT",
        "MALFORMED_OUTPUT",
    }:
        # Those two malformed boundaries are allowed to lack a typed intent.
        intent = None
    elif (
        expected.decision.value == "ACCEPT"
        or PipelineStage.INTENT in expected.required_provenance_stages
    ):
        intent = IntentObservation(
            goal_key=expected.intent_key,
            ambiguous=expected.decision.value == "CLARIFY",
            conflicting_requirement_ids=expected.expected_conflict_ids,
            unsupported_requirement_ids=expected.unsupported_requirements,
        )

    strategy = None
    if (
        PipelineStage.STRATEGY in expected.required_provenance_stages
        and expected.decision.value == "ACCEPT"
    ):
        strategy = StrategyObservation(
            goal_key=expected.intent_key,
            structurally_valid=True,
            internally_coherent=True,
            capability_ids=tuple(item.capability_id for item in expected.capabilities),
            semantic_roles=expected.required_semantic_roles,
            provenance_ref=f"{case.evaluation_input.request_id}/strategy",
        )

    creative_ir = None
    if PipelineStage.CREATIVE_IR in expected.required_provenance_stages:
        order_requirement = next(
            (item for item in expected.requirements if item.kind.value == "preserve_order"),
            None,
        )
        ordered_scene_ids = (
            tuple(part.strip() for part in order_requirement.value.split(","))
            if order_requirement
            else ("scene-a", "scene-b", "scene-c")
        )
        duration_limit = expected.max_duration_us or next(
            (
                int(item.value)
                for item in expected.requirements
                if item.kind.value == "max_duration_us"
            ),
            15_000_000,
        )
        measured_duration = duration_limit - 1 if duration_limit > 1 else duration_limit
        creative_ir = CreativeIRObservation(
            schema_valid=True,
            semantic_digest="a" * 64,
            root_identity=f"ir-{case.case_id}",
            provenance_ref=f"{case.evaluation_input.request_id}/creative-ir",
            total_duration_us=measured_duration,
            ordered_scene_ids=ordered_scene_ids,
            strongest_emphasis_scene_id=ordered_scene_ids[-1],
            effects=(),
        )

    constraint_traces = _constraint_traces(case)
    repeated_plans = (plan,) if plan and expected.require_plan_determinism else ()

    authorization = []
    dispatches = []
    if expected.failure_code in {FailureCode.POLICY_DENIED, FailureCode.AUTHORIZATION_DENIED}:
        denial_evidence = EvidenceRecord(
            kind=EvidenceKind.AUTHORIZATION_DECISION,
            source=EvidenceSource.POLICY_ENGINE,
            reference=f"policy/{case.evaluation_input.request_id}/deny",
            value=PolicyDecision.DENY.value,
        )
        authorization.append(
            AuthorizationObservation(
                actor_id=case.evaluation_input.trusted_actor_id,
                actor_source=IdentitySource.TRUSTED_CONTEXT,
                policy_decision=PolicyDecision.DENY,
                policy_evidence=denial_evidence,
                approval_decision=ApprovalDecision.NOT_REQUIRED,
                command_ids=(),
            )
        )
    if will_dispatch and plan:
        for command in commands:
            policy_evidence = EvidenceRecord(
                kind=EvidenceKind.AUTHORIZATION_DECISION,
                source=EvidenceSource.POLICY_ENGINE,
                reference=f"policy/{command.command_id}",
                value=PolicyDecision.ALLOW.value,
            )
            authorization.append(
                AuthorizationObservation(
                    actor_id=case.evaluation_input.trusted_actor_id,
                    actor_source=IdentitySource.TRUSTED_CONTEXT,
                    policy_decision=PolicyDecision.ALLOW,
                    policy_evidence=policy_evidence,
                    approval_decision=ApprovalDecision.NOT_REQUIRED,
                    command_ids=(command.command_id,),
                )
            )
            dispatches.append(
                DispatchObservation(
                    command_id=command.command_id,
                    boundary=DispatchBoundary.COMMAND_BUS,
                    actor_id=case.evaluation_input.trusted_actor_id,
                    result=(DispatchResult.FAILED if failed_attempt else DispatchResult.SUCCEEDED),
                    policy_evidence_id=policy_evidence.evidence_id,
                    process_exit_code=(
                        1 if expected.failure_code is FailureCode.EXECUTION_FAILED else None
                    )
                    if failed_attempt
                    else 0,
                    side_effect_ids=() if failed_attempt else (f"effect-{command.command_id}",),
                )
            )

    execution = (
        ExecutionObservation(
            execution_id="execution-1",
            dispatches=tuple(dispatches),
            completed=will_execute,
            process_exit_code=(1 if expected.failure_code is FailureCode.EXECUTION_FAILED else None)
            if failed_attempt
            else 0,
            transaction_id="transaction-1",
        )
        if will_dispatch
        else None
    )

    artifact = None
    verification = None
    if expected.require_artifact or PipelineStage.ARTIFACT in expected.required_provenance_stages:
        violations = set(expected.expected_artifact_violations)
        measured_hash = "a" * 64
        declared_hash = "b" * 64 if ArtifactViolation.HASH_MISMATCH in violations else measured_hash
        if ArtifactViolation.MALFORMED_HASH in violations:
            measured_hash = "malformed"
            declared_hash = "malformed"
        byte_length = 0 if ArtifactViolation.ZERO_BYTES in violations else 128
        duration_us = 12_000_000
        if ArtifactViolation.DURATION_EXCEEDED in violations:
            duration_us = (expected.max_duration_us or 15_000_000) + 1
        artifact = ArtifactObservation(
            artifact_id="artifact-1",
            sha256=measured_hash,
            declared_sha256=declared_hash,
            byte_length=byte_length,
            project_id=case.evaluation_input.project_id,
            revision_id=(
                expected.expected_artifact_revision_id or case.evaluation_input.revision_id
            ),
            transaction_id="transaction-1",
            trace_id="trace-1",
            media_header_valid=ArtifactViolation.INVALID_MEDIA_HEADER not in violations,
            stale=ArtifactViolation.STALE_ARTIFACT in violations,
            duration_us=duration_us,
            exists=ArtifactViolation.MISSING_ARTIFACT not in violations,
        )
        if (
            expected.require_independent_verification
            or PipelineStage.VERIFICATION in expected.required_provenance_stages
        ) and expected.expected_verification_verdict is not VerificationVerdict.MISSING:
            verifier_evidence = [
                EvidenceRecord(
                    kind=EvidenceKind.ARTIFACT_SHA256,
                    source=EvidenceSource.INDEPENDENT_VERIFIER,
                    reference=artifact.artifact_id,
                    value=artifact.sha256,
                ),
                EvidenceRecord(
                    kind=EvidenceKind.ARTIFACT_SIZE,
                    source=EvidenceSource.INDEPENDENT_VERIFIER,
                    reference=artifact.artifact_id,
                    value=str(artifact.byte_length),
                ),
                EvidenceRecord(
                    kind=EvidenceKind.MEDIA_HEADER,
                    source=EvidenceSource.INDEPENDENT_VERIFIER,
                    reference=artifact.artifact_id,
                    value="valid" if artifact.media_header_valid else "invalid",
                ),
            ]
            if expected.max_duration_us is not None:
                verifier_evidence.append(
                    EvidenceRecord(
                        kind=EvidenceKind.MEDIA_DURATION_US,
                        source=EvidenceSource.INDEPENDENT_VERIFIER,
                        reference=artifact.artifact_id,
                        value=str(artifact.duration_us),
                    )
                )
            verifier_verdict = expected.expected_verification_verdict or (
                VerificationVerdict.FAIL
                if expected.expected_artifact_violations
                else VerificationVerdict.PASS
            )
            verification = VerificationObservation(
                verifier_id=expected.expected_verifier_id
                or "nexus.independent-artifact-verifier/v1",
                verifier_version="1.0.0",
                independent=True,
                verdict=verifier_verdict,
                artifact_sha256=artifact.sha256,
                duration_us=artifact.duration_us,
                evidence=tuple(verifier_evidence),
            )

    provenance = []
    stages = expected.required_provenance_stages
    previous_object_id = ""
    transaction_position = list(PipelineStage).index(PipelineStage.TRANSACTION)
    artifact_position = list(PipelineStage).index(PipelineStage.ARTIFACT)
    for stage in stages:
        object_id = (
            case.evaluation_input.request_id
            if stage is PipelineStage.REQUEST
            else f"{stage.value}-object"
        )
        stage_position = list(PipelineStage).index(stage)
        provenance.append(
            ProvenanceLink(
                stage=stage,
                object_id=object_id,
                parent_id=previous_object_id,
                project_id=case.evaluation_input.project_id,
                revision_id=(
                    expected.expected_artifact_revision_id
                    if expected.expected_artifact_revision_id is not None
                    and stage_position >= artifact_position
                    else case.evaluation_input.revision_id
                ),
                transaction_id="transaction-1" if stage_position >= transaction_position else "",
                artifact_id="artifact-1" if stage_position >= artifact_position else "",
                trace_id="trace-1",
                evidence_source=_STAGE_SOURCES[stage],
            )
        )
        previous_object_id = object_id

    idempotency = None
    if expected.idempotency_required or case.evaluation_input.delivery_count > 1:
        payload_digest = hashlib.sha256(
            case.evaluation_input.request_text.encode("utf-8")
        ).hexdigest()
        deliveries = tuple(
            DeliveryObservation(
                request_id=case.evaluation_input.request_id,
                payload_sha256=payload_digest,
                causal_id="causal-1",
                execution_id="execution-1",
            )
            for _ in range(case.evaluation_input.delivery_count)
        )
        idempotency = IdempotencyObservation(
            deliveries=deliveries,
            execution_ids=("execution-1",),
            side_effect_count=1,
        )

    recovery = None
    if expected.recovery_required:
        recovery = RecoveryObservation(
            original_causal_id="causal-1",
            recovered_causal_id="causal-1",
            restart_count=1,
            original_history_ids=("command-history-1",),
            recovered_history_ids=("command-history-1", "recovery-record-1"),
            repeated_effect_count=0,
            must_retry_stages=(PipelineStage.EXECUTION,),
            must_not_repeat_stages=(PipelineStage.COMMAND, PipelineStage.TRANSACTION),
            actual_retried_stages=(PipelineStage.EXECUTION,),
            actual_repeated_stages=(),
        )

    replan = None
    if expected.max_replans > 0:
        attempts = []
        previous_plan = ""
        previous_command_ids = tuple(command.command_id for command in commands)
        for attempt_number in range(1, expected.max_replans + 2):
            current_plan = f"replan-{attempt_number}"
            attempts.append(
                ReplanAttempt(
                    attempt_number=attempt_number,
                    plan_id=current_plan,
                    previous_plan_id=previous_plan,
                    previous_command_ids=previous_command_ids,
                    preserved_command_ids=previous_command_ids,
                    policy_reauthorized=True,
                )
            )
            previous_plan = current_plan
        replan = ReplanObservation(
            retry_budget=expected.max_replans,
            attempts=tuple(attempts),
            previous_result_preserved=True,
        )

    identity_pairs = (
        (_identity_pair(expected.identity_relation),)
        if expected.identity_relation is not None
        else ()
    )
    if identity_pairs and expected.identity_relation is IdentityRelation.LOCALIZED_SEMANTIC_EDIT:
        identity_pairs = (
            replace(
                identity_pairs[0],
                allowed_changed_paths=expected.identity_allowed_changed_paths,
            ),
        )

    security_boundaries = []
    if case.evaluation_input.external_reference_project_id:
        boundary_kind = (
            SecurityBoundaryKind.PROVENANCE_SPOOF
            if case.adversarial_class is AdversarialClass.PROVENANCE_SPOOF
            else SecurityBoundaryKind.CROSS_PROJECT_REFERENCE
        )
        attempted_reference = case.evaluation_input.external_reference_project_id
        source = EvidenceSource.POLICY_ENGINE
    elif case.evaluation_input.replayed_trace_id:
        boundary_kind = SecurityBoundaryKind.TRACE_REPLAY
        attempted_reference = case.evaluation_input.replayed_trace_id
        source = EvidenceSource.PERSISTENCE
    else:
        boundary_kind = None
        attempted_reference = ""
        source = EvidenceSource.TEST_HARNESS
    if boundary_kind is not None and expected.failure_code is not None:
        boundary_evidence = EvidenceRecord(
            kind=EvidenceKind.SECURITY_BOUNDARY,
            source=source,
            reference=case.evaluation_input.request_id,
            value=f"{boundary_kind.value};attempt={attempted_reference};rejected=true;"
            f"failure={expected.failure_code.value}",
        )
        security_boundaries.append(
            SecurityBoundaryObservation(
                kind=boundary_kind,
                attempted_reference=attempted_reference,
                rejected=True,
                failure_code=expected.failure_code,
                evidence=boundary_evidence,
            )
        )

    return ObservedBehavior(
        outcome=outcome,
        observed_request_id=case.evaluation_input.request_id,
        observed_project_id=case.evaluation_input.project_id,
        observed_revision_id=case.evaluation_input.revision_id,
        failure_code=expected.failure_code,
        intent=intent,
        strategy=strategy,
        creative_ir=creative_ir,
        constraint_traces=constraint_traces,
        plan=plan,
        repeated_plans=repeated_plans,
        identity_pairs=identity_pairs,
        authorization=tuple(authorization),
        execution=execution,
        artifact=artifact,
        verification=verification,
        provenance=tuple(provenance),
        idempotency=idempotency,
        recovery=recovery,
        replan=replan,
        tool_state=expected.expected_tool_state,
        security_boundaries=tuple(security_boundaries),
        tool_state_evidence=(
            EvidenceRecord(
                kind=EvidenceKind.TOOL_STATE,
                source=EvidenceSource.TEST_HARNESS,
                reference=f"{case.evaluation_input.request_id}/tool-state",
                value=expected.expected_tool_state.value,
            )
            if expected.expected_tool_state is not None
            else None
        ),
        malformed_output=expected.failure_code is FailureCode.MALFORMED_OUTPUT,
    )
