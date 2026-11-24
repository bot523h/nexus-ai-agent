"""Authenticated durable ingress facade; concrete adapters live outside studio.

The facade accepts credentials, never a caller-asserted ActorIdentity or a
caller-supplied approval verifier. Composition is trusted application code.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from nexus_ai_agent.creative.studio.authorization import ProjectAuthorizer
from nexus_ai_agent.creative.studio.bus import CommandBus, PlanResultSummary, PlanTransaction
from nexus_ai_agent.creative.studio.durable_dedupe import (
    ApprovalBinding,
    ApprovalVerifier,
    DurablePlanDedupePort,
    PlanApprovalRequirement,
)
from nexus_ai_agent.creative.studio.models import ActorIdentity, AuthorizationError, TypedCommand
from nexus_ai_agent.creative.studio.plan_execution import compile_plan
from nexus_ai_agent.creative.studio.plan_ir import PlanIR


class RuntimeAuthority(Protocol):
    def authenticate(self, credential: str) -> ActorIdentity: ...

    def issue(self, requirements: tuple[PlanApprovalRequirement, ...]) -> str: ...

    def verify_token(self, token: str) -> tuple[tuple[ApprovalBinding, ...], ApprovalVerifier]: ...


@dataclass(frozen=True)
class DurableStudioExecutionRuntime:
    """Single ingress for explicit human approval and approved DAG execution.

    This is an application boundary, not a sandbox against hostile Python code
    that can access its private fields. Transport must protect bearer secrets.
    """

    _bus: CommandBus
    _store: DurablePlanDedupePort
    _authority: RuntimeAuthority
    _authorizer: ProjectAuthorizer
    policy_version: str = "w4.v1"

    def __post_init__(self) -> None:
        if self._store is None or self._bus._durable_store is not self._store:
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
            compile_plan(plan_ir, commands),
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
    ) -> PlanTransaction | PlanResultSummary:
        actor = self._authority.authenticate(credential)
        bindings, verifier = self._authority.verify_token(approval_token)
        return self._bus.dispatch_plan(
            compile_plan(plan_ir, commands),
            approval_attestations=bindings,
            policy_version=self.policy_version,
            authenticated_actor=actor,
            approval_verifier=verifier,
            deadline_monotonic_ns=self._store.monotonic_ns() + 30_000_000_000,
        )
