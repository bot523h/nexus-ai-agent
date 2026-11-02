"""Compile a typed AST into a registry-validated :class:`CompiledPlan`.

The compiler is the **one** place that maps an AST to registry operations, and
it validates every choice against the authoritative ``CapabilityRegistry``
that is *injected* by the composition root (the same registry the CommandBus
enforces) — a plan can never name an operation, schema version, or input the
registry does not expose. The registry object is never imported or built here.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from nexus_ai_agent.creative.studio.capabilities import CapabilityRegistry
from nexus_ai_agent.nagar.ibpc.canonical import digest_of
from nexus_ai_agent.nagar.ibpc.models import (
    CapabilityIdentity,
    CompiledPlan,
    PlannedStep,
    TypedCreativeAST,
)


class CompilationRefused(ValueError):
    """Typed refusal: the AST cannot become a plan against this registry."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}".strip(": "))
        self.reason = reason
        self.detail = detail


def capability_identity_of(registry: CapabilityRegistry, operation: str) -> CapabilityIdentity:
    """Snapshot the registry's own description of one operation.

    ``schema_digest`` binds the operation's *input JSON schema*, so a
    registry-side schema change moves every plan digest compiled after it.
    """
    try:
        spec = registry.get_spec(operation)
    except Exception as exc:  # UnknownOperationError and friends
        raise CompilationRefused("unknown_operation", f"{operation}: {exc}") from exc
    schema_digest = digest_of(spec.input_model.model_json_schema())
    return CapabilityIdentity(
        operation_id=spec.operation_id,
        schema_version=spec.schema_version,
        permission_level=spec.permission_level.value,
        execution_modes=tuple(spec.execution_modes),
        required_packs=tuple(spec.required_packs),
        required_permissions=tuple(spec.effective_permissions),
        schema_digest=schema_digest,
    )


def compile_ast(ast: TypedCreativeAST, *, registry: CapabilityRegistry) -> CompiledPlan:
    """AST → plan, fail-closed at every gate.

    Gates: AST executable (no ambiguities) → operation registered → input
    validates against the operation's schema model → policy allows the
    operation at its permission level → plan digest minted.
    """
    if not ast.executable:
        raise CompilationRefused(
            "ambiguous_ast", "; ".join(ast.ambiguities) or "AST carries ambiguities"
        )
    capability = capability_identity_of(registry, ast.operation)

    spec = registry.get_spec(ast.operation)
    try:
        validated = spec.input_model.model_validate(dict(ast.params))
    except ValidationError as exc:
        raise CompilationRefused("invalid_input", f"{ast.operation}: {exc}") from exc

    decision = registry.check_permission(ast.operation, confirmed=False)
    if not decision.allowed:
        raise CompilationRefused("policy_denied", f"{ast.operation}: {decision.reason}")

    if "local" not in spec.execution_modes:
        raise CompilationRefused("no_local_mode", f"{ast.operation} advertises no local execution")

    step = PlannedStep(
        operation=ast.operation,
        input=validated.model_dump(mode="json"),
        target={"project_id": ast.project_id, "asset_id": ast.asset_id},
    )
    return CompiledPlan(
        ast_digest=ast.ast_digest,
        capability=capability,
        steps=(step,),
        execution_mode="local",
    )


def plan_input_for_step(plan: CompiledPlan, index: int = 0) -> dict[str, Any]:
    return dict(plan.steps[index].input)


__all__ = ["CompilationRefused", "capability_identity_of", "compile_ast", "plan_input_for_step"]
