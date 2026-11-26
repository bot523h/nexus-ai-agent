"""Compile a validated DAG into the existing command bus execution path.

Compilation grants no authority. The resulting transaction still requires the
bus's project, approval, registry and durable execution gates. Callback-based
external effects must be composed separately; this compiler accepts commands.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from nexus_ai_agent.creative.studio.bus import (
    PlanResultSummary,
    PlanStep,
    PlanTransaction,
    StudioExecutionPort,
    command_fingerprint,
    parse_command,
)
from nexus_ai_agent.creative.studio.durable_dedupe import ApprovalBinding, ApprovalVerifier
from nexus_ai_agent.creative.studio.models import ActorIdentity, TypedCommand
from nexus_ai_agent.creative.studio.plan_ir import PlanIR


def compile_plan(
    ir: PlanIR,
    commands: Mapping[str, TypedCommand],
    *,
    compensations: Mapping[str, Callable[[Any], Any]] | None = None,
) -> PlanTransaction:
    """Revalidate owned command copies and reject any changed or extra binding."""
    if not isinstance(ir, PlanIR):
        raise ValueError("typed PlanIR is required")
    ordered = ir.topological_nodes()
    if set(commands) != {node.node_id for node in ordered}:
        raise ValueError("commands must exactly cover the DAG")
    steps: list[PlanStep] = []
    for node in ordered:
        command = parse_command(commands[node.node_id])
        if (
            command.operation != node.operation
            or command_fingerprint(command) != node.command_digest
        ):
            raise ValueError("command differs from approved DAG node")
        if command.target.project_id != ir.project_id:
            raise ValueError("command project differs from DAG")
        steps.append(
            PlanStep(
                operation=node.operation,
                command=command,
                compensate=(compensations or {}).get(node.operation),
            )
        )
    return PlanTransaction(plan_id=ir.plan_id, steps=tuple(steps), admitted_ir_hash=ir.plan_hash)


def execute_durable_ir(
    bus: StudioExecutionPort,
    ir: PlanIR,
    commands: Mapping[str, TypedCommand],
    *,
    approval_attestations: tuple[ApprovalBinding, ...],
    policy_version: str,
    authenticated_actor: ActorIdentity,
    approval_verifier: ApprovalVerifier,
    deadline_monotonic_ns: int,
) -> PlanTransaction | PlanResultSummary:
    """Compile then enter durable admission; no legacy/non-durable fallback."""
    return bus.dispatch_plan(
        compile_plan(ir, commands),
        approval_attestations=approval_attestations,
        policy_version=policy_version,
        authenticated_actor=authenticated_actor,
        approval_verifier=approval_verifier,
        deadline_monotonic_ns=deadline_monotonic_ns,
    )
