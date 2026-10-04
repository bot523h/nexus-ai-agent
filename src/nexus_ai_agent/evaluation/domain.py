"""Typed, immutable contracts for independent Agent assurance.

This package defines observations and verdicts only. It does not import the
Agent runtime, authorize operations, dispatch commands, execute tools, persist
state, or verify production artifacts.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass
from enum import Enum
from typing import Any


def _require_tuple_items(value: object, item_type: type, label: str) -> None:
    if not isinstance(value, tuple):
        raise TypeError(f"{label} must be a tuple")
    if any(not isinstance(item, item_type) for item in value):
        raise TypeError(f"{label} contains a value that is not {item_type.__name__}")


class Verdict(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    BLOCKED = "BLOCKED"


class GateVerdict(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"


class EvaluationDimension(str, Enum):
    INTENT_CORRECTNESS = "intent_correctness"
    CONSTRAINT_PRESERVATION = "constraint_preservation"
    STRATEGY_VALIDITY = "strategy_validity"
    IR_VALIDITY = "ir_validity"
    COMPILER_DETERMINISM = "compiler_determinism"
    CAPABILITY_VALIDITY = "capability_validity"
    TOOL_SELECTION = "tool_selection"
    AUTHORIZATION_INTEGRITY = "authorization_integrity"
    EXECUTION_INTEGRITY = "execution_integrity"
    VERIFICATION_INTEGRITY = "verification_integrity"
    PROVENANCE_INTEGRITY = "provenance_integrity"
    IDEMPOTENCY = "idempotency"
    RECOVERY = "recovery"
    REPLAN_SAFETY = "replan_safety"
    ARCHITECTURE_BOUNDARY = "architecture_boundary"
    E2E_READINESS = "e2e_readiness"


class FailureCode(str, Enum):
    INVALID_INPUT = "INVALID_INPUT"
    AMBIGUOUS_INTENT = "AMBIGUOUS_INTENT"
    CONFLICTING_REQUIREMENTS = "CONFLICTING_REQUIREMENTS"
    UNSUPPORTED_REQUEST = "UNSUPPORTED_REQUEST"
    UNSUPPORTED_CAPABILITY = "UNSUPPORTED_CAPABILITY"
    POLICY_DENIED = "POLICY_DENIED"
    AUTHORIZATION_DENIED = "AUTHORIZATION_DENIED"
    ACTOR_SPOOFED = "ACTOR_SPOOFED"
    APPROVAL_FORGED = "APPROVAL_FORGED"
    DIRECT_EXECUTION = "DIRECT_EXECUTION"
    TOOL_UNAVAILABLE = "TOOL_UNAVAILABLE"
    CONFIRMATION_REQUIRED = "CONFIRMATION_REQUIRED"
    EXECUTION_FAILED = "EXECUTION_FAILED"
    VERIFICATION_FAILED = "VERIFICATION_FAILED"
    RECOVERY_FAILED = "RECOVERY_FAILED"
    TIMEOUT = "TIMEOUT"
    DUPLICATE_REQUEST = "DUPLICATE_REQUEST"
    CONSTRAINT_DROPPED = "CONSTRAINT_DROPPED"
    PROVENANCE_TAMPERED = "PROVENANCE_TAMPERED"
    CROSS_PROJECT_LINEAGE = "CROSS_PROJECT_LINEAGE"
    FAKE_SUCCESS = "FAKE_SUCCESS"
    REPLAN_UNBOUNDED = "REPLAN_UNBOUNDED"
    MALFORMED_OUTPUT = "MALFORMED_OUTPUT"
    INTERNAL_CONTRACT_FAILURE = "INTERNAL_CONTRACT_FAILURE"


class Severity(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class CaseKind(str, Enum):
    GOLDEN = "GOLDEN"
    ADVERSARIAL = "ADVERSARIAL"
    FAILURE = "FAILURE"
    PROPERTY = "PROPERTY"


class AdversarialClass(str, Enum):
    EMPTY = "empty"
    VERY_LONG = "very_long"
    CONTRADICTORY = "contradictory"
    AMBIGUOUS = "ambiguous"
    UNSUPPORTED = "unsupported"
    MALFORMED_OUTPUT = "malformed_output"
    UNKNOWN_CAPABILITY = "unknown_capability"
    FAKE_CAPABILITY = "fake_capability"
    FAKE_ACTOR = "fake_actor"
    FAKE_APPROVAL = "fake_approval"
    USER_AUTHORITY = "user_authority"
    PATH_TRAVERSAL = "path_traversal"
    SHELL_LIKE = "shell_like"
    PROMPT_INJECTION = "prompt_injection"
    TOOL_RESULT_INJECTION = "tool_result_injection"
    PROVENANCE_SPOOF = "provenance_spoof"
    REPLAY = "replay"
    DUPLICATE_DELIVERY = "duplicate_delivery"
    STALE_EXECUTION = "stale_execution"
    CROSS_PROJECT = "cross_project"


class ExpectedDecision(str, Enum):
    ACCEPT = "ACCEPT"
    REFUSE = "REFUSE"
    CLARIFY = "CLARIFY"
    REQUIRE_CONFIRMATION = "REQUIRE_CONFIRMATION"


class ObservedOutcome(str, Enum):
    NOT_STARTED = "NOT_STARTED"
    ACCEPTED = "ACCEPTED"
    REFUSED = "REFUSED"
    NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"
    AWAITING_CONFIRMATION = "AWAITING_CONFIRMATION"
    EXECUTED = "EXECUTED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    PENDING = "PENDING"


class PipelineStage(str, Enum):
    REQUEST = "request"
    INTENT = "intent"
    STRATEGY = "strategy"
    CREATIVE_IR = "creative_ir"
    PLAN = "plan"
    AUTHORIZATION = "authorization"
    COMMAND = "command"
    TRANSACTION = "transaction"
    EXECUTION = "execution"
    ARTIFACT = "artifact"
    VERIFICATION = "verification"
    RECEIPT = "receipt"


class RequirementKind(str, Enum):
    MAX_DURATION_US = "max_duration_us"
    PRESERVE_ORDER = "preserve_order"
    SEMANTIC_ROLE = "semantic_role"
    FORBIDDEN_EFFECT = "forbidden_effect"
    ARGUMENT_VALUE = "argument_value"
    STYLE = "style"
    LANGUAGE = "language"
    OUTPUT_FORMAT = "output_format"
    OTHER = "other"


class RequirementState(str, Enum):
    PRESERVED = "PRESERVED"
    LOST = "LOST"
    NOT_REACHED = "NOT_REACHED"


class ArgumentType(str, Enum):
    STRING = "string"
    IDENTIFIER = "identifier"
    INTEGER = "integer"
    BOOLEAN = "boolean"
    DECIMAL_STRING = "decimal_string"
    SHA256 = "sha256"
    CANONICAL_JSON = "canonical_json"


class IdentitySource(str, Enum):
    TRUSTED_CONTEXT = "trusted_context"
    USER_PAYLOAD = "user_payload"
    MODEL_OUTPUT = "model_output"
    TOOL_OUTPUT = "tool_output"
    UNKNOWN = "unknown"


class PolicyDecision(str, Enum):
    ALLOW = "ALLOW"
    DENY = "DENY"
    MISSING = "MISSING"


class ApprovalDecision(str, Enum):
    NOT_REQUIRED = "NOT_REQUIRED"
    APPROVED = "APPROVED"
    DENIED = "DENIED"
    MISSING = "MISSING"


class EvidenceSource(str, Enum):
    USER_INPUT = "user_input"
    MODEL_OUTPUT = "model_output"
    TOOL_OUTPUT = "tool_output"
    COMMAND_BUS = "command_bus"
    POLICY_ENGINE = "policy_engine"
    APPROVAL_STORE = "approval_store"
    INDEPENDENT_VERIFIER = "independent_verifier"
    PERSISTENCE = "persistence"
    TEST_HARNESS = "test_harness"
    SUBJECT_SELF_REPORT = "subject_self_report"


class EvidenceKind(str, Enum):
    REQUEST_DIGEST = "request_digest"
    INTENT_CONTRACT = "intent_contract"
    REQUIREMENT_STAGE = "requirement_stage"
    STRATEGY_CONTRACT = "strategy_contract"
    TOOL_STATE = "tool_state"
    IR_SCHEMA = "ir_schema"
    PLAN_BYTES = "plan_bytes"
    CAPABILITY_ARGUMENT = "capability_argument"
    AUTHORIZATION_DECISION = "authorization_decision"
    APPROVAL_DECISION = "approval_decision"
    DISPATCH_EVENT = "dispatch_event"
    EXECUTION_RESULT = "execution_result"
    ARTIFACT_SHA256 = "artifact_sha256"
    ARTIFACT_SIZE = "artifact_size"
    MEDIA_HEADER = "media_header"
    MEDIA_DURATION_US = "media_duration_us"
    PROVENANCE_LINK = "provenance_link"
    IDEMPOTENCY_RECORD = "idempotency_record"
    RECOVERY_RECORD = "recovery_record"
    REPLAN_ATTEMPT = "replan_attempt"
    ARCHITECTURE_SCAN = "architecture_scan"
    VERIFIER_STATUS = "verifier_status"
    SECURITY_BOUNDARY = "security_boundary"


class ToolSelectionState(str, Enum):
    CORRECTLY_SELECTED = "CORRECTLY_SELECTED"
    INCORRECTLY_SELECTED = "INCORRECTLY_SELECTED"
    UNAVAILABLE = "UNAVAILABLE"
    POLICY_BLOCKED = "POLICY_BLOCKED"
    REQUIRES_CONFIRMATION = "REQUIRES_CONFIRMATION"
    EXECUTION_FAILED = "EXECUTION_FAILED"
    SUCCEEDED_UNVERIFIED = "SUCCEEDED_UNVERIFIED"
    VERIFIED = "VERIFIED"
    NOT_SELECTED = "NOT_SELECTED"


class DispatchBoundary(str, Enum):
    COMMAND_BUS = "command_bus"
    DIRECT_SUBPROCESS = "direct_subprocess"
    DIRECT_FILESYSTEM = "direct_filesystem"
    DIRECT_HTTP = "direct_http"
    DIRECT_DATABASE = "direct_database"
    DIRECT_SHELL = "direct_shell"
    UNKNOWN = "unknown"


class DispatchResult(str, Enum):
    NOT_STARTED = "NOT_STARTED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class VerificationVerdict(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    MISSING = "MISSING"


class ArtifactViolation(str, Enum):
    MISSING_ARTIFACT = "MISSING_ARTIFACT"
    ZERO_BYTES = "ZERO_BYTES"
    MALFORMED_HASH = "MALFORMED_HASH"
    HASH_MISMATCH = "HASH_MISMATCH"
    INVALID_MEDIA_HEADER = "INVALID_MEDIA_HEADER"
    STALE_ARTIFACT = "STALE_ARTIFACT"
    DURATION_EXCEEDED = "DURATION_EXCEEDED"


class E2EStage(str, Enum):
    REQUEST = "request"
    TYPED_INTENT = "typed_intent"
    STRATEGY = "strategy"
    CREATIVE_IR = "creative_ir"
    PLAN = "plan"
    AUTHORIZATION = "authorization"
    COMMAND_BUS = "command_bus"
    EXECUTION = "execution"
    VERIFICATION = "verification"
    EVIDENCE = "evidence"


class E2EStageStatus(str, Enum):
    AVAILABLE = "AVAILABLE"
    BLOCKED = "BLOCKED"
    DEFERRED = "DEFERRED"


class IdentityRelation(str, Enum):
    SAME_SEMANTIC_INPUT = "SAME_SEMANTIC_INPUT"
    LOCALIZED_SEMANTIC_EDIT = "LOCALIZED_SEMANTIC_EDIT"


class SecurityBoundaryKind(str, Enum):
    CROSS_PROJECT_REFERENCE = "CROSS_PROJECT_REFERENCE"
    PROVENANCE_SPOOF = "PROVENANCE_SPOOF"
    TRACE_REPLAY = "TRACE_REPLAY"


@dataclass(frozen=True, slots=True)
class EvaluationInput:
    """User-side input plus test-harness identities; never an authority grant."""

    request_text: str
    request_id: str
    project_id: str
    revision_id: str
    locale: str = "en"
    delivery_count: int = 1
    trusted_actor_id: str = "test-user-1"
    untrusted_actor_claim: str = ""
    untrusted_tool_output: str = ""
    external_reference_project_id: str = ""
    replayed_trace_id: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.request_text, str):
            raise TypeError("request_text must be a string")
        for name in ("request_id", "project_id", "revision_id", "locale", "trusted_actor_id"):
            _require_text(getattr(self, name), name)
        if not isinstance(self.delivery_count, int) or isinstance(self.delivery_count, bool):
            raise TypeError("delivery_count must be an integer")
        if self.delivery_count < 1:
            raise ValueError("delivery_count must be at least one")


@dataclass(frozen=True, slots=True)
class ConstraintRequirement:
    requirement_id: str
    text: str
    kind: RequirementKind
    value: str
    hard: bool = True
    required_stages: tuple[PipelineStage, ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.requirement_id, "requirement_id")
        _require_text(self.text, "text")
        _require_text(self.value, "constraint value")
        if not isinstance(self.kind, RequirementKind):
            raise TypeError("kind must be RequirementKind")
        if self.kind is RequirementKind.MAX_DURATION_US:
            try:
                duration = int(self.value)
            except ValueError as error:
                raise ValueError("MAX_DURATION_US value must be an integer") from error
            if duration <= 0:
                raise ValueError("MAX_DURATION_US value must be positive")
        if len(set(self.required_stages)) != len(self.required_stages):
            raise ValueError("required_stages must not contain duplicates")
        if tuple(
            sorted(self.required_stages, key=lambda stage: list(PipelineStage).index(stage))
        ) != (self.required_stages):
            raise ValueError("required_stages must be in pipeline order")


@dataclass(frozen=True, slots=True)
class ArgumentContract:
    name: str
    value_type: ArgumentType
    required: bool = True

    def __post_init__(self) -> None:
        _require_text(self.name, "argument name")
        if not isinstance(self.value_type, ArgumentType):
            raise TypeError("value_type must be ArgumentType")


@dataclass(frozen=True, slots=True)
class CapabilityContract:
    capability_id: str
    arguments: tuple[ArgumentContract, ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.capability_id, "capability_id")
        names = tuple(argument.name for argument in self.arguments)
        if len(names) != len(set(names)):
            raise ValueError("capability argument names must be unique")


@dataclass(frozen=True, slots=True)
class ExpectedBehavior:
    decision: ExpectedDecision
    intent_key: str
    expected_outcome: ObservedOutcome | None = None
    failure_code: FailureCode | None = None
    requirements: tuple[ConstraintRequirement, ...] = ()
    capabilities: tuple[CapabilityContract, ...] = ()
    required_provenance_stages: tuple[PipelineStage, ...] = ()
    expected_execution_count: int | None = None
    idempotency_required: bool = False
    recovery_required: bool = False
    require_artifact: bool = False
    require_independent_verification: bool = False
    expected_verifier_id: str | None = None
    expected_verification_verdict: VerificationVerdict | None = None
    expected_artifact_violations: tuple[ArtifactViolation, ...] = ()
    expected_artifact_revision_id: str | None = None
    max_duration_us: int | None = None
    max_replans: int = 0
    required_semantic_roles: tuple[str, ...] = ()
    expected_tool_state: ToolSelectionState | None = None
    unsupported_capability: str | None = None
    unsupported_requirements: tuple[str, ...] = ()
    expected_conflict_ids: tuple[str, ...] = ()
    requires_confirmation: bool = False
    require_plan_determinism: bool = False
    identity_relation: IdentityRelation | None = None
    identity_allowed_changed_paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.decision, ExpectedDecision):
            raise TypeError("decision must be ExpectedDecision")
        if self.expected_outcome is not None and not isinstance(
            self.expected_outcome, ObservedOutcome
        ):
            raise TypeError("expected_outcome must be ObservedOutcome")
        if self.failure_code is not None and not isinstance(self.failure_code, FailureCode):
            raise TypeError("failure_code must be FailureCode")
        if self.expected_verification_verdict is not None and not isinstance(
            self.expected_verification_verdict, VerificationVerdict
        ):
            raise TypeError("expected_verification_verdict must be VerificationVerdict")
        if self.identity_relation is not None and not isinstance(
            self.identity_relation, IdentityRelation
        ):
            raise TypeError("identity_relation must be IdentityRelation")
        _require_text(self.intent_key, "intent_key")
        _require_tuple_items(self.requirements, ConstraintRequirement, "requirements")
        _require_tuple_items(self.capabilities, CapabilityContract, "capabilities")
        _require_tuple_items(self.required_provenance_stages, PipelineStage, "provenance stages")
        _require_tuple_items(
            self.expected_artifact_violations,
            ArtifactViolation,
            "expected artifact violations",
        )
        _require_tuple_items(self.required_semantic_roles, str, "required semantic roles")
        _require_tuple_items(self.unsupported_requirements, str, "unsupported requirements")
        _require_tuple_items(self.expected_conflict_ids, str, "expected conflict ids")
        _require_tuple_items(
            self.identity_allowed_changed_paths,
            str,
            "identity allowed changed paths",
        )
        requirement_ids = tuple(item.requirement_id for item in self.requirements)
        if len(requirement_ids) != len(set(requirement_ids)):
            raise ValueError("requirement ids must be unique within an expected contract")
        capability_ids = tuple(item.capability_id for item in self.capabilities)
        if len(capability_ids) != len(set(capability_ids)):
            raise ValueError("capability ids must be unique within an expected contract")
        if len(set(self.required_provenance_stages)) != len(self.required_provenance_stages):
            raise ValueError("required provenance stages must not contain duplicates")
        if self.expected_execution_count is not None and self.expected_execution_count < 0:
            raise ValueError("expected_execution_count cannot be negative")
        if self.max_replans < 0:
            raise ValueError("max_replans cannot be negative")
        if self.max_duration_us is not None and self.max_duration_us < 0:
            raise ValueError("max_duration_us cannot be negative")
        if self.require_independent_verification and not self.expected_verifier_id:
            raise ValueError("an expected verifier id is required for independent verification")
        if self.expected_verifier_id is not None:
            _require_text(self.expected_verifier_id, "expected_verifier_id")
        if len(set(self.expected_artifact_violations)) != len(self.expected_artifact_violations):
            raise ValueError("expected artifact violations must be unique")
        if self.expected_artifact_violations and not self.require_independent_verification:
            raise ValueError("artifact violation cases require independent verification")
        if self.expected_artifact_revision_id is not None:
            _require_text(self.expected_artifact_revision_id, "expected_artifact_revision_id")
        if self.unsupported_capability is not None:
            _require_text(self.unsupported_capability, "unsupported_capability")
        if len(self.required_semantic_roles) != len(set(self.required_semantic_roles)):
            raise ValueError("required semantic roles must be unique")
        if len(self.unsupported_requirements) != len(set(self.unsupported_requirements)):
            raise ValueError("unsupported requirement ids must be unique")
        if len(self.expected_conflict_ids) != len(set(self.expected_conflict_ids)):
            raise ValueError("expected conflict ids must be unique")
        if len(self.identity_allowed_changed_paths) != len(
            set(self.identity_allowed_changed_paths)
        ):
            raise ValueError("identity change paths must be unique")
        for path in self.identity_allowed_changed_paths:
            _require_text(path, "identity change path")
        if self.identity_relation is None and self.identity_allowed_changed_paths:
            raise ValueError("identity change paths require an identity relation")
        if (
            self.identity_relation is IdentityRelation.LOCALIZED_SEMANTIC_EDIT
            and not self.identity_allowed_changed_paths
        ):
            raise ValueError("localized semantic edits require declared changed paths")
        if (
            self.identity_relation is IdentityRelation.SAME_SEMANTIC_INPUT
            and self.identity_allowed_changed_paths
        ):
            raise ValueError("same-input identity cases cannot allow changed paths")


@dataclass(frozen=True, slots=True)
class EvaluationCase:
    case_key: str
    kind: CaseKind
    evaluation_input: EvaluationInput
    expected: ExpectedBehavior
    adversarial_class: AdversarialClass | None = None
    tags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.case_key, "case_key")
        if not isinstance(self.evaluation_input, EvaluationInput):
            raise TypeError("evaluation_input must be EvaluationInput")
        if not isinstance(self.expected, ExpectedBehavior):
            raise TypeError("expected must be ExpectedBehavior")
        if not isinstance(self.kind, CaseKind):
            raise TypeError("kind must be CaseKind")
        if self.kind is CaseKind.ADVERSARIAL and self.adversarial_class is None:
            raise ValueError("adversarial cases must declare an adversarial class")
        if self.kind is not CaseKind.ADVERSARIAL and self.adversarial_class is not None:
            raise ValueError("only adversarial cases may declare an adversarial class")
        if self.expected.expected_artifact_revision_id == self.evaluation_input.revision_id:
            raise ValueError(
                "expected artifact revision fault must differ from the request revision"
            )
        if self.expected.expected_artifact_revision_id is not None and (
            self.expected.expected_outcome is not ObservedOutcome.FAILED
            or self.expected.failure_code is not FailureCode.PROVENANCE_TAMPERED
        ):
            raise ValueError("artifact revision fault must have a typed failed outcome")
        if len(set(self.tags)) != len(self.tags):
            raise ValueError("case tags must be unique")

    @property
    def case_id(self) -> str:
        return content_id("case", self)


@dataclass(frozen=True, slots=True)
class EvaluationSuite:
    suite_id: str
    version: str
    cases: tuple[EvaluationCase, ...]
    required_dimensions: tuple[EvaluationDimension, ...]

    def __post_init__(self) -> None:
        _require_text(self.suite_id, "suite_id")
        _require_text(self.version, "suite version")
        _require_tuple_items(self.cases, EvaluationCase, "suite cases")
        _require_tuple_items(self.required_dimensions, EvaluationDimension, "required dimensions")
        if not self.cases:
            raise ValueError("an evaluation suite must contain at least one case")
        ids = tuple(case.case_id for case in self.cases)
        if len(ids) != len(set(ids)):
            raise ValueError("suite case ids must be unique")
        if len(set(self.required_dimensions)) != len(self.required_dimensions):
            raise ValueError("required dimensions must be unique")

    @property
    def suite_digest(self) -> str:
        return content_digest(
            {
                "suite_id": self.suite_id,
                "version": self.version,
                "case_ids": tuple(case.case_id for case in self.cases),
                "required_dimensions": self.required_dimensions,
            }
        )


@dataclass(frozen=True, slots=True)
class RequirementStageMark:
    stage: PipelineStage
    state: RequirementState
    evidence_ref: str = ""
    evidence: EvidenceRecord | None = None


@dataclass(frozen=True, slots=True)
class RequirementTrace:
    requirement_id: str
    marks: tuple[RequirementStageMark, ...]

    def __post_init__(self) -> None:
        _require_text(self.requirement_id, "requirement_id")
        stages = tuple(mark.stage for mark in self.marks)
        if len(stages) != len(set(stages)):
            raise ValueError("a requirement trace cannot contain duplicate stages")


@dataclass(frozen=True, slots=True)
class IntentObservation:
    goal_key: str
    ambiguous: bool = False
    conflicting_requirement_ids: tuple[str, ...] = ()
    unsupported_requirement_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class StrategyObservation:
    goal_key: str
    structurally_valid: bool
    internally_coherent: bool
    capability_ids: tuple[str, ...]
    semantic_roles: tuple[str, ...] = ()
    provenance_ref: str = ""
    invented_authority: bool = False


@dataclass(frozen=True, slots=True)
class CreativeIRObservation:
    schema_valid: bool
    invalid_references: tuple[str, ...] = ()
    impossible_timing: bool = False
    unresolved_intents: tuple[str, ...] = ()
    semantic_digest: str = ""
    root_identity: str = ""
    provenance_ref: str = ""
    total_duration_us: int | None = None
    ordered_scene_ids: tuple[str, ...] = ()
    strongest_emphasis_scene_id: str = ""
    effects: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.total_duration_us is not None and self.total_duration_us < 0:
            raise ValueError("total_duration_us cannot be negative")


@dataclass(frozen=True, slots=True)
class ArgumentObservation:
    name: str
    value_type: ArgumentType
    canonical_value: str

    def __post_init__(self) -> None:
        _require_text(self.name, "argument name")
        if not isinstance(self.value_type, ArgumentType):
            raise TypeError("value_type must be ArgumentType")
        if not isinstance(self.canonical_value, str):
            raise TypeError("canonical_value must be a string")


@dataclass(frozen=True, slots=True)
class CommandObservation:
    command_id: str
    capability_id: str
    arguments: tuple[ArgumentObservation, ...] = ()
    dependencies: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.command_id, "command_id")
        _require_text(self.capability_id, "capability_id")
        names = tuple(argument.name for argument in self.arguments)
        if len(names) != len(set(names)):
            raise ValueError("command argument names must be unique")
        if len(set(self.dependencies)) != len(self.dependencies):
            raise ValueError("command dependencies must be unique")


@dataclass(frozen=True, slots=True)
class PlanObservation:
    plan_id: str
    commands: tuple[CommandObservation, ...]
    canonical_bytes: bytes
    compile_side_effects: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.plan_id, "plan_id")
        if not isinstance(self.canonical_bytes, bytes):
            raise TypeError("canonical_bytes must be bytes")

    @property
    def canonical_digest(self) -> str:
        return hashlib.sha256(self.canonical_bytes).hexdigest()


@dataclass(frozen=True, slots=True)
class EvidenceRecord:
    kind: EvidenceKind
    source: EvidenceSource
    reference: str
    value: str

    def __post_init__(self) -> None:
        if not self.reference:
            raise ValueError("evidence reference cannot be empty")
        if not isinstance(self.value, str):
            raise TypeError("evidence value must be a string")

    @property
    def evidence_id(self) -> str:
        return content_id("evidence", self)

    @property
    def content_digest(self) -> str:
        return content_digest(self)


@dataclass(frozen=True, slots=True)
class SecurityBoundaryObservation:
    kind: SecurityBoundaryKind
    attempted_reference: str
    rejected: bool
    failure_code: FailureCode
    evidence: EvidenceRecord

    def __post_init__(self) -> None:
        _require_text(self.attempted_reference, "attempted security-boundary reference")
        if not isinstance(self.kind, SecurityBoundaryKind):
            raise TypeError("kind must be SecurityBoundaryKind")
        if not isinstance(self.failure_code, FailureCode):
            raise TypeError("failure_code must be FailureCode")
        if self.evidence.kind is not EvidenceKind.SECURITY_BOUNDARY:
            raise ValueError("security-boundary observation requires SECURITY_BOUNDARY evidence")


@dataclass(frozen=True, slots=True)
class AuthorizationObservation:
    actor_id: str
    actor_source: IdentitySource
    policy_decision: PolicyDecision
    policy_evidence: EvidenceRecord | None
    approval_decision: ApprovalDecision = ApprovalDecision.NOT_REQUIRED
    approval_source: IdentitySource | None = None
    approval_evidence: EvidenceRecord | None = None
    command_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DispatchObservation:
    command_id: str
    boundary: DispatchBoundary
    actor_id: str
    result: DispatchResult
    policy_evidence_id: str = ""
    process_exit_code: int | None = None
    side_effect_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ExecutionObservation:
    execution_id: str
    dispatches: tuple[DispatchObservation, ...]
    completed: bool = False
    process_exit_code: int | None = None
    transaction_id: str = ""


@dataclass(frozen=True, slots=True)
class ArtifactObservation:
    artifact_id: str
    sha256: str
    declared_sha256: str
    byte_length: int
    project_id: str
    revision_id: str
    transaction_id: str
    trace_id: str
    media_header_valid: bool
    stale: bool = False
    duration_us: int | None = None
    exists: bool = True

    def __post_init__(self) -> None:
        if self.byte_length < 0:
            raise ValueError("artifact byte_length cannot be negative")
        if self.duration_us is not None and self.duration_us < 0:
            raise ValueError("artifact duration_us cannot be negative")


@dataclass(frozen=True, slots=True)
class VerificationObservation:
    verifier_id: str
    verifier_version: str
    independent: bool
    verdict: VerificationVerdict
    artifact_sha256: str
    duration_us: int | None
    evidence: tuple[EvidenceRecord, ...]


@dataclass(frozen=True, slots=True)
class ProvenanceLink:
    stage: PipelineStage
    object_id: str
    parent_id: str
    project_id: str
    revision_id: str
    transaction_id: str
    artifact_id: str
    trace_id: str
    evidence_source: EvidenceSource
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class DeliveryObservation:
    request_id: str
    payload_sha256: str
    causal_id: str
    execution_id: str


@dataclass(frozen=True, slots=True)
class IdempotencyObservation:
    deliveries: tuple[DeliveryObservation, ...]
    execution_ids: tuple[str, ...]
    side_effect_count: int

    def __post_init__(self) -> None:
        if self.side_effect_count < 0:
            raise ValueError("side_effect_count cannot be negative")


@dataclass(frozen=True, slots=True)
class RecoveryObservation:
    original_causal_id: str
    recovered_causal_id: str
    restart_count: int
    original_history_ids: tuple[str, ...]
    recovered_history_ids: tuple[str, ...]
    repeated_effect_count: int
    must_retry_stages: tuple[PipelineStage, ...]
    must_not_repeat_stages: tuple[PipelineStage, ...]
    actual_retried_stages: tuple[PipelineStage, ...]
    actual_repeated_stages: tuple[PipelineStage, ...]

    def __post_init__(self) -> None:
        if self.restart_count < 0:
            raise ValueError("restart_count cannot be negative")
        if self.repeated_effect_count < 0:
            raise ValueError("repeated_effect_count cannot be negative")


@dataclass(frozen=True, slots=True)
class ReplanAttempt:
    attempt_number: int
    plan_id: str
    previous_plan_id: str
    previous_command_ids: tuple[str, ...]
    preserved_command_ids: tuple[str, ...]
    policy_reauthorized: bool
    unrelated_work_changed: bool = False


@dataclass(frozen=True, slots=True)
class ReplanObservation:
    retry_budget: int | None
    attempts: tuple[ReplanAttempt, ...]
    previous_result_preserved: bool


@dataclass(frozen=True, slots=True)
class ObservedBehavior:
    """Typed facts captured at the test boundary; contains no subject verdict."""

    outcome: ObservedOutcome
    observed_request_id: str = ""
    observed_project_id: str = ""
    observed_revision_id: str = ""
    failure_code: FailureCode | None = None
    intent: IntentObservation | None = None
    strategy: StrategyObservation | None = None
    creative_ir: CreativeIRObservation | None = None
    constraint_traces: tuple[RequirementTrace, ...] = ()
    plan: PlanObservation | None = None
    repeated_plans: tuple[PlanObservation, ...] = ()
    identity_pairs: tuple[IdentityPair, ...] = ()
    authorization: tuple[AuthorizationObservation, ...] = ()
    execution: ExecutionObservation | None = None
    artifact: ArtifactObservation | None = None
    verification: VerificationObservation | None = None
    provenance: tuple[ProvenanceLink, ...] = ()
    idempotency: IdempotencyObservation | None = None
    recovery: RecoveryObservation | None = None
    replan: ReplanObservation | None = None
    tool_state: ToolSelectionState | None = None
    tool_state_evidence: EvidenceRecord | None = None
    security_boundaries: tuple[SecurityBoundaryObservation, ...] = ()
    subject_claimed_success: bool = False
    malformed_output: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, ObservedOutcome):
            raise TypeError("outcome must be ObservedOutcome")
        for name in ("observed_request_id", "observed_project_id", "observed_revision_id"):
            if not isinstance(getattr(self, name), str):
                raise TypeError(f"{name} must be a string")
        if self.failure_code is not None and not isinstance(self.failure_code, FailureCode):
            raise TypeError("failure_code must be FailureCode")
        optional_types = (
            ("intent", IntentObservation),
            ("strategy", StrategyObservation),
            ("creative_ir", CreativeIRObservation),
            ("plan", PlanObservation),
            ("execution", ExecutionObservation),
            ("artifact", ArtifactObservation),
            ("verification", VerificationObservation),
            ("idempotency", IdempotencyObservation),
            ("recovery", RecoveryObservation),
            ("replan", ReplanObservation),
            ("tool_state_evidence", EvidenceRecord),
        )
        for name, expected_type in optional_types:
            value = getattr(self, name)
            if value is not None and not isinstance(value, expected_type):
                raise TypeError(f"{name} must be {expected_type.__name__} or None")
        if self.tool_state is not None and not isinstance(self.tool_state, ToolSelectionState):
            raise TypeError("tool_state must be ToolSelectionState or None")
        tuple_contracts = (
            ("constraint_traces", RequirementTrace),
            ("repeated_plans", PlanObservation),
            ("identity_pairs", IdentityPair),
            ("authorization", AuthorizationObservation),
            ("provenance", ProvenanceLink),
            ("security_boundaries", SecurityBoundaryObservation),
        )
        for name, item_type in tuple_contracts:
            _require_tuple_items(getattr(self, name), item_type, name)
        if not isinstance(self.subject_claimed_success, bool) or not isinstance(
            self.malformed_output, bool
        ):
            raise TypeError("subject_claimed_success and malformed_output must be booleans")


@dataclass(frozen=True, slots=True)
class IdentityNode:
    path: str
    identity: str


@dataclass(frozen=True, slots=True)
class IdentityPair:
    relation: IdentityRelation
    before_root_id: str
    after_root_id: str
    before_nodes: tuple[IdentityNode, ...]
    after_nodes: tuple[IdentityNode, ...]
    allowed_changed_paths: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class FailureFinding:
    case_id: str
    oracle_id: str
    dimension: EvaluationDimension
    code: FailureCode
    severity: Severity
    hard_gate: bool
    detail: str
    evidence_ids: tuple[str, ...]

    @property
    def finding_id(self) -> str:
        return content_id("finding", self)


@dataclass(frozen=True, slots=True)
class BlockedCondition:
    code: str
    dimension: EvaluationDimension
    reason: str

    def __post_init__(self) -> None:
        _require_text(self.code, "blocked condition code")
        _require_text(self.reason, "blocked condition reason")

    @property
    def condition_id(self) -> str:
        return content_id("blocked", self)


@dataclass(frozen=True, slots=True)
class OracleResult:
    case_id: str
    oracle_id: str
    oracle_version: str
    dimension: EvaluationDimension
    status: Verdict
    findings: tuple[FailureFinding, ...] = ()
    evidence: tuple[EvidenceRecord, ...] = ()

    def __post_init__(self) -> None:
        evidence_ids = {record.evidence_id for record in self.evidence}
        for finding in self.findings:
            if finding.case_id != self.case_id:
                raise ValueError("finding belongs to a different case")
            if finding.oracle_id != self.oracle_id:
                raise ValueError("finding belongs to a different oracle")
            if not set(finding.evidence_ids).issubset(evidence_ids):
                raise ValueError("every finding evidence id must be present in its oracle result")


@dataclass(frozen=True, slots=True)
class DimensionResult:
    dimension: EvaluationDimension
    status: Verdict
    passed: int = 0
    failed: int = 0
    not_applicable: int = 0
    blocked: int = 0


@dataclass(frozen=True, slots=True)
class EvaluationResult:
    case_id: str
    case_key: str
    kind: CaseKind
    evaluation_input: EvaluationInput
    expected: ExpectedBehavior
    observed: ObservedBehavior | None
    status: Verdict
    oracle_results: tuple[OracleResult, ...]
    dimension_results: tuple[DimensionResult, ...]
    findings: tuple[FailureFinding, ...]
    evidence: tuple[EvidenceRecord, ...]
    blocked_conditions: tuple[BlockedCondition, ...] = ()


@dataclass(frozen=True, slots=True)
class E2EStageReadiness:
    stage: E2EStage
    status: E2EStageStatus
    evidence_ref: str
    reason: str


@dataclass(frozen=True, slots=True)
class E2EReadiness:
    adapter_id: str
    stages: tuple[E2EStageReadiness, ...]

    @property
    def status(self) -> E2EStageStatus:
        if any(item.status is E2EStageStatus.BLOCKED for item in self.stages):
            return E2EStageStatus.BLOCKED
        if any(item.status is E2EStageStatus.DEFERRED for item in self.stages):
            return E2EStageStatus.DEFERRED
        return E2EStageStatus.AVAILABLE


@dataclass(frozen=True, slots=True)
class EvaluationRun:
    suite_id: str
    suite_version: str
    subject_sha: str
    adapter_id: str
    results: tuple[EvaluationResult, ...]
    e2e_readiness: E2EReadiness
    gate_verdict: GateVerdict
    hard_failures: tuple[FailureFinding, ...]
    blocked_conditions: tuple[BlockedCondition, ...]
    oracle_versions: tuple[tuple[str, str], ...]

    @property
    def run_id(self) -> str:
        return content_id(
            "run",
            {
                "suite_id": self.suite_id,
                "suite_version": self.suite_version,
                "subject_sha": self.subject_sha,
                "adapter_id": self.adapter_id,
                "oracle_versions": self.oracle_versions,
                "results": tuple(
                    (
                        result.case_id,
                        result.status,
                        content_digest(result.observed)
                        if result.observed is not None
                        else "blocked",
                        tuple(record.evidence_id for record in result.evidence),
                        tuple(f.finding_id for f in result.findings),
                    )
                    for result in self.results
                ),
                "e2e_readiness": self.e2e_readiness,
                "gate_verdict": self.gate_verdict,
                "hard_failures": tuple(f.finding_id for f in self.hard_failures),
                "blocked_conditions": self.blocked_conditions,
            },
        )

    @property
    def dimensions(self) -> tuple[DimensionResult, ...]:
        aggregated = list(aggregate_dimensions(self.results))
        e2e_status = (
            Verdict.PASS
            if self.e2e_readiness.status is E2EStageStatus.AVAILABLE
            else Verdict.BLOCKED
        )
        for index, item in enumerate(aggregated):
            if item.dimension is EvaluationDimension.E2E_READINESS:
                aggregated[index] = DimensionResult(
                    dimension=EvaluationDimension.E2E_READINESS,
                    status=e2e_status,
                    passed=int(e2e_status is Verdict.PASS),
                    blocked=int(e2e_status is Verdict.BLOCKED),
                )
                break
        return tuple(aggregated)

    def report(self) -> EvaluationReport:
        return EvaluationReport(
            suite_id=self.suite_id,
            suite_version=self.suite_version,
            run_id=self.run_id,
            subject_sha=self.subject_sha,
            adapter_id=self.adapter_id,
            cases=self.results,
            dimensions=self.dimensions,
            hard_failures=self.hard_failures,
            blocked_conditions=self.blocked_conditions,
            e2e_readiness=self.e2e_readiness,
            verdict=self.gate_verdict,
            oracle_versions=self.oracle_versions,
        )


@dataclass(frozen=True, slots=True)
class EvaluationReport:
    suite_id: str
    suite_version: str
    run_id: str
    subject_sha: str
    adapter_id: str
    cases: tuple[EvaluationResult, ...]
    dimensions: tuple[DimensionResult, ...]
    hard_failures: tuple[FailureFinding, ...]
    blocked_conditions: tuple[BlockedCondition, ...]
    e2e_readiness: E2EReadiness
    verdict: GateVerdict
    oracle_versions: tuple[tuple[str, str], ...]
    schema_version: str = "1.0"

    def to_data(self) -> dict[str, Any]:
        cases = []
        oracle_versions = {oracle_id: version for oracle_id, version in self.oracle_versions}
        all_evidence: set[str] = set()
        failed_invariants: set[str] = set()
        for result in self.cases:
            for oracle_result in result.oracle_results:
                oracle_versions[oracle_result.oracle_id] = oracle_result.oracle_version
            for record in result.evidence:
                all_evidence.add(record.evidence_id)
            for finding in result.findings:
                failed_invariants.add(f"{finding.case_id}:{finding.oracle_id}:{finding.code.value}")
            cases.append(
                {
                    "case_id": result.case_id,
                    "case_key": result.case_key,
                    "kind": result.kind.value,
                    "status": result.status.value,
                    "input": to_primitive(result.evaluation_input),
                    "expected": to_primitive(result.expected),
                    "observed": to_primitive(result.observed),
                    "oracles": [
                        {
                            "id": oracle.oracle_id,
                            "version": oracle.oracle_version,
                            "dimension": oracle.dimension.value,
                            "status": oracle.status.value,
                            "finding_ids": [item.finding_id for item in oracle.findings],
                            "evidence_refs": [item.evidence_id for item in oracle.evidence],
                        }
                        for oracle in result.oracle_results
                    ],
                    "dimensions": {
                        item.dimension.value: item.status.value for item in result.dimension_results
                    },
                    "findings": [to_primitive(item) for item in result.findings],
                    "evidence": [to_primitive(item) for item in result.evidence],
                    "blocked_conditions": [
                        to_primitive(item) for item in result.blocked_conditions
                    ],
                }
            )
        return {
            "schema_version": self.schema_version,
            "suite": {"id": self.suite_id, "version": self.suite_version},
            "run_id": self.run_id,
            "subject": {"sha": self.subject_sha, "adapter": self.adapter_id},
            "case_ids": [item.case_id for item in self.cases],
            "oracle_versions": {key: oracle_versions[key] for key in sorted(oracle_versions)},
            "cases": cases,
            "dimensions": {item.dimension.value: item.status.value for item in self.dimensions},
            "hard_failures": [to_primitive(item) for item in self.hard_failures],
            "failed_invariants": sorted(failed_invariants),
            "blocked_conditions": [to_primitive(item) for item in self.blocked_conditions],
            "evidence_refs": sorted(all_evidence),
            "e2e_readiness": to_primitive(self.e2e_readiness),
            "verdict": self.verdict.value,
        }

    def to_json(self) -> str:
        return canonical_json(self.to_data())


HARD_GATE_DIMENSIONS = frozenset(
    {
        EvaluationDimension.CONSTRAINT_PRESERVATION,
        EvaluationDimension.CAPABILITY_VALIDITY,
        EvaluationDimension.AUTHORIZATION_INTEGRITY,
        EvaluationDimension.EXECUTION_INTEGRITY,
        EvaluationDimension.VERIFICATION_INTEGRITY,
        EvaluationDimension.PROVENANCE_INTEGRITY,
        EvaluationDimension.IDEMPOTENCY,
        EvaluationDimension.RECOVERY,
        EvaluationDimension.REPLAN_SAFETY,
        EvaluationDimension.ARCHITECTURE_BOUNDARY,
    }
)


FAILURE_TAXONOMY = frozenset(FailureCode)


def aggregate_dimensions(results: tuple[EvaluationResult, ...]) -> tuple[DimensionResult, ...]:
    counts: dict[EvaluationDimension, dict[Verdict, int]] = {
        dimension: {
            Verdict.PASS: 0,
            Verdict.FAIL: 0,
            Verdict.NOT_APPLICABLE: 0,
            Verdict.BLOCKED: 0,
        }
        for dimension in EvaluationDimension
    }
    for result in results:
        for item in result.dimension_results:
            counts[item.dimension][item.status] += max(
                1,
                item.passed + item.failed + item.not_applicable + item.blocked,
            )
    summary: list[DimensionResult] = []
    for dimension in EvaluationDimension:
        value = counts[dimension]
        if value[Verdict.FAIL]:
            status = Verdict.FAIL
        elif value[Verdict.BLOCKED]:
            status = Verdict.BLOCKED
        elif value[Verdict.PASS]:
            status = Verdict.PASS
        else:
            status = Verdict.NOT_APPLICABLE
        summary.append(
            DimensionResult(
                dimension=dimension,
                status=status,
                passed=value[Verdict.PASS],
                failed=value[Verdict.FAIL],
                not_applicable=value[Verdict.NOT_APPLICABLE],
                blocked=value[Verdict.BLOCKED],
            )
        )
    return tuple(summary)


def to_primitive(value: Any) -> Any:
    """Convert typed contracts to JSON-safe values with deterministic field names."""
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {item.name: to_primitive(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, bytes):
        return {"encoding": "hex", "value": value.hex()}
    if isinstance(value, tuple):
        return [to_primitive(item) for item in value]
    if isinstance(value, list):
        return [to_primitive(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): to_primitive(value[key]) for key in sorted(value, key=str)}
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise TypeError(f"cannot serialize evaluation value of type {type(value).__name__}")


def canonical_json(value: Any) -> str:
    return json.dumps(
        to_primitive(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_json_bytes(value: Any) -> bytes:
    return canonical_json(value).encode("utf-8")


def content_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def content_id(namespace: str, value: Any) -> str:
    _require_text(namespace, "identity namespace")
    return f"{namespace}-{content_digest(value)[:24]}"


def is_sha256(value: str) -> bool:
    return bool(re.fullmatch(r"(?:sha256:)?[0-9a-f]{64}", value))


def is_subject_sha(value: str) -> bool:
    return bool(re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", value))


def _require_text(value: str, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
