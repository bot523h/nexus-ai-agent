"""Authenticated durable ingress facade; concrete adapters live outside studio.

The facade accepts credentials, never a caller-asserted ActorIdentity or a
caller-supplied approval verifier. Composition is trusted application code.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from nexus_ai_agent.creative.studio.authorization import ProjectAuthorizer
from nexus_ai_agent.creative.studio.bus import (
    PlanResultSummary,
    PlanTransaction,
    StudioExecutionPort,
)
from nexus_ai_agent.creative.studio.durable_dedupe import (
    ApprovalBinding,
    ApprovalVerifier,
    DurablePlanDedupePort,
    PlanApprovalRequirement,
    RecoveryEvidence,
)
from nexus_ai_agent.creative.studio.models import ActorIdentity, AuthorizationError, TypedCommand
from nexus_ai_agent.creative.studio.plan_execution import compile_plan
from nexus_ai_agent.creative.studio.plan_ir import PlanIR


class RepairSession(Protocol):
    def __enter__(self) -> ActorIdentity: ...

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: Any
    ) -> bool | None: ...


class RuntimeAuthority(Protocol):
    def repair_session(self, credential: str, project_id: str) -> RepairSession: ...

    def authenticate(self, credential: str) -> ActorIdentity: ...

    def issue(self, requirements: tuple[PlanApprovalRequirement, ...]) -> str: ...

    def verify_token(self, token: str) -> tuple[tuple[ApprovalBinding, ...], ApprovalVerifier]: ...


@dataclass(frozen=True)
class DurableStudioExecutionRuntime:
    """Single ingress for explicit human approval and approved DAG execution.

    This is an application boundary, not a sandbox against hostile Python code
    that can access its private fields. Transport must protect bearer secrets.
    """

    _bus: StudioExecutionPort
    _store: DurablePlanDedupePort
    _authority: RuntimeAuthority
    _authorizer: ProjectAuthorizer
    policy_version: str = "w4.v1"
    _compensations: Mapping[str, Callable[[Any], Any]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self._store is None or not self._bus.uses_durable_store(self._store):
            raise ValueError("runtime requires the bus's configured durable store")

    def approve_plan_ir(
        self, plan_ir: PlanIR, *, commands: Mapping[str, TypedCommand], credential: str
    ) -> str:
        actor = self._authority.authenticate(credential)
        access = self._authorizer.authorize(actor, plan_ir.project_id)
        if access.actor != actor or access.project_id != plan_ir.project_id:
            raise AuthorizationError("approval authority returned mismatched access")
        access.require_permissions(("project:approve",))
        requirements = self._bus.durable_approval_requirements(
            compile_plan(plan_ir, commands, compensations=self._compensations),
            policy_version=self.policy_version,
            authenticated_actor=actor,
        )
        return self._authority.issue(requirements)

    def execute_approved_plan_ir(
        self,
        plan_ir: PlanIR,
        approval_token: str,
        *,
        commands: Mapping[str, TypedCommand],
        credential: str,
        lease_ttl_seconds: float = 30.0,
    ) -> PlanTransaction | PlanResultSummary:
        actor = self._authority.authenticate(credential)
        bindings, verifier = self._authority.verify_token(approval_token)
        return self._bus.dispatch_plan(
            compile_plan(plan_ir, commands, compensations=self._compensations),
            approval_attestations=bindings,
            policy_version=self.policy_version,
            authenticated_actor=actor,
            approval_verifier=verifier,
            deadline_monotonic_ns=self._store.monotonic_ns() + 30_000_000_000,
            lease_ttl_seconds=lease_ttl_seconds,
        )

    def inspect_recovery(
        self,
        project_id: str,
        plan_id: str,
        plan_hash: str,
        *,
        credential: str,
        expected_status: str,
        expected_attempt: int,
    ) -> RecoveryEvidence[Any]:
        with self._authority.repair_session(credential, project_id) as actor:
            return self._bus.inspect_durable_recovery(
                plan_id,
                plan_hash,
                authenticated_operator=actor,
                expected_status=expected_status,
                expected_attempt=expected_attempt,
            )

    def recover(
        self,
        evidence: RecoveryEvidence[Any],
        *,
        credential: str,
        resolution_action: str,
        reason: str,
    ) -> None:
        with self._authority.repair_session(credential, evidence.project_id) as actor:
            self._bus.recover_durable_plan(
                evidence.plan_id,
                evidence.plan_hash,
                authenticated_operator=actor,
                expected_status=evidence.expected_status,
                expected_attempt=evidence.expected_attempt,
                resolution_action=resolution_action,
                reason=reason,
                recovery_evidence=evidence,
            )
