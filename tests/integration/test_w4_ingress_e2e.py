"""Real bearer authentication, MAC and SQLite for state-only durable ingress.

Credentials are test-provisioned. This is not the external-effect production E2E.
"""

from dataclasses import replace
from pathlib import Path

import pytest

from nexus_ai_agent.adapters.durable_studio_runtime import (
    ProvisionedProjectAuthorizer,
    build_production_durable_command_bus,
)
from nexus_ai_agent.application.principal import PrincipalId
from nexus_ai_agent.creative.studio.authorization import ProjectAccess
from nexus_ai_agent.creative.studio.bus import PlanTransactionState, _fingerprint
from nexus_ai_agent.creative.studio.models import (
    ActorIdentity,
    AuthorizationError,
    CommandResult,
    Project,
    TargetRef,
    Timeline,
    TypedCommand,
    new_project,
)
from nexus_ai_agent.creative.studio.plan_ir import PlanIR, PlanNode


def verify_play(project: Project, command: TypedCommand, result: CommandResult, key: str) -> bool:
    return (
        command.operation == "media.play"
        and project.timeline.playhead.is_playing
        and len(key) == 64
    )


def test_authenticated_ir_approval_execution_and_cold_replay(tmp_path: Path) -> None:
    principal = PrincipalId("prn_" + "a" * 32)
    actor = ActorIdentity(kind="user", actor_id=str(principal))
    credential = "test-only-bearer-" + "b" * 32
    project = new_project("project", "test", Timeline(timeline_id="timeline", duration_us=0))
    access = ProjectAccess(actor, "project", frozenset({"project:read", "project:approve"}))
    authorizer = ProvisionedProjectAuthorizer({str(principal): access})
    runtime = build_production_durable_command_bus(
        tmp_path / "db",
        tmp_path,
        approval_secret=b"k" * 32,
        project=project,
        credentials={credential: principal},
        authorizer=authorizer,
        verifiers={"media.play": verify_play},
    )
    command = TypedCommand(
        command_id="one",
        operation="media.play",
        actor=actor,
        target=TargetRef(project_id="project"),
        idempotency_key="play-once",
    )
    ir = PlanIR("project", "p", (PlanNode("a", "media.play", _fingerprint(command)),))
    commands = {"a": command}
    with pytest.raises(AuthorizationError):
        runtime.approve_plan_ir(ir, commands=commands, credential="forged")
    with pytest.raises(AuthorizationError):
        runtime.execute_approved_plan_ir(ir, "[]", commands=commands, credential=credential)
    token = runtime.approve_plan_ir(ir, commands=commands, credential=credential)
    with pytest.raises(AuthorizationError):
        runtime.execute_approved_plan_ir(
            ir, token.replace('"mac":"', '"mac":"f'), commands=commands, credential=credential
        )
    result = runtime.execute_approved_plan_ir(ir, token, commands=commands, credential=credential)
    assert result.state == PlanTransactionState.COMMITTED
    cold = build_production_durable_command_bus(
        tmp_path / "db",
        tmp_path,
        approval_secret=b"k" * 32,
        project=project,
        credentials={credential: principal},
        authorizer=authorizer,
        verifiers={"media.play": verify_play},
    )
    assert (
        cold.execute_approved_plan_ir(ir, token, commands=commands, credential=credential).state
        == PlanTransactionState.COMMITTED
    )
    with pytest.raises(Exception, match="approval|scope|binding"):
        cold.execute_approved_plan_ir(
            replace(ir, plan_id="other"), token, commands=commands, credential=credential
        )


def test_operation_truth_drift_fails_closed() -> None:
    from nexus_ai_agent.creative.studio.capabilities import build_wave1_registry

    registry = build_wave1_registry()
    with pytest.raises(ValueError, match="contract drift"):
        registry.verify_declared_contracts({})
    contracts = {
        operation: {
            "permission_level": registry.get_spec(operation).permission_level.value,
            "input_model": (
                f"{registry.get_spec(operation).input_model.__module__}."
                f"{registry.get_spec(operation).input_model.__qualname__}"
            ),
            "handler": registry.get_spec(operation).handler.__name__,
        }
        for operation in registry.list_operations()
    }
    assert registry.verify_declared_contracts(contracts) == tuple(sorted(contracts))
    contracts["media.play"]["permission_level"] = "D"
    with pytest.raises(ValueError, match="contract drift"):
        registry.verify_declared_contracts(contracts)


@pytest.mark.parametrize(
    "token", ["", "null", "{}", "[null]", '[{"binding":{}}]', "[1]", "x" * 1_048_577]
)
def test_malformed_approval_tokens_fail_closed(token: str) -> None:
    from nexus_ai_agent.adapters.durable_studio_runtime import ProvisionedPrincipalAuthority

    authority = ProvisionedPrincipalAuthority({"c" * 32: PrincipalId("prn_" + "b" * 32)}, b"k" * 32)
    with pytest.raises(AuthorizationError):
        authority.verify_token(token)
