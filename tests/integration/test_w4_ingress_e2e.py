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
from nexus_ai_agent.creative.studio.runtime import DurableStudioExecutionRuntime


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


def external_runtime(
    root: Path,
) -> tuple[DurableStudioExecutionRuntime, PlanIR, dict[str, TypedCommand], str]:
    """Deployment fixture: real credentials, authority, MAC, DB and destination.

    The extra operation contract is explicitly provisioned for this deployment;
    it does not claim that the repository's default Wave-1 registry includes it.
    """
    import json

    from nexus_ai_agent.adapters.durable_studio_runtime import (
        register_filesystem_artifact_operation,
    )
    from nexus_ai_agent.creative.studio.bus import command_fingerprint
    from nexus_ai_agent.creative.studio.capabilities import build_wave1_registry

    principal = PrincipalId("prn_" + "a" * 32)
    actor = ActorIdentity(kind="user", actor_id=str(principal))
    credential = "test-only-bearer-" + "b" * 32
    project = new_project("project", "test", Timeline(timeline_id="timeline", duration_us=0))
    authorizer = ProvisionedProjectAuthorizer(
        {
            str(principal): ProjectAccess(
                actor,
                "project",
                frozenset({"project:read", "project:write", "project:approve", "project:repair"}),
            )
        }
    )
    registry = build_wave1_registry()
    register_filesystem_artifact_operation(registry)
    truth_path = root / "operation_truth.json"
    if not truth_path.exists():
        rows = []
        for operation in registry.list_operations():
            spec = registry.get_spec(operation)
            rows.append(
                {
                    "operation_id": operation,
                    "evidence": {
                        "permission_level": spec.permission_level.value,
                        "input_model": (
                            f"{spec.input_model.__module__}.{spec.input_model.__qualname__}"
                        ),
                        "handler": spec.handler.__name__,
                        "precondition_contract": spec.precondition_contract,
                        "postcondition_contract": spec.postcondition_contract,
                    },
                }
            )
        truth_path.write_text(
            json.dumps({"schema": "nagar.operation_truth.v1", "operations": rows})
        )
    runtime = build_production_durable_command_bus(
        root / "external.db",
        root,
        approval_secret=b"k" * 32,
        project=project,
        credentials={credential: principal},
        authorizer=authorizer,
        registry=registry,
        verifiers={"media.play": verify_play},
        operation_truth_path=truth_path,
    )
    commands = {
        "a": TypedCommand(
            command_id="one",
            operation="media.play",
            actor=actor,
            target=TargetRef(project_id="project"),
            idempotency_key="play-once",
        ),
        "b": TypedCommand(
            command_id="two",
            operation="artifact.publish",
            actor=actor,
            target=TargetRef(project_id="project"),
            idempotency_key="artifact-once",
            input={"content": "durable artifact"},
        ),
    }
    ir = PlanIR(
        "project",
        "external-plan",
        tuple(
            PlanNode(
                key, command.operation, command_fingerprint(command), ("a",) if key == "b" else ()
            )
            for key, command in commands.items()
        ),
    )
    return runtime, ir, commands, credential


def test_external_artifact_ingress_and_cold_replay(tmp_path: Path) -> None:
    import sqlite3

    runtime, ir, commands, credential = external_runtime(tmp_path)
    token = runtime.approve_plan_ir(ir, commands=commands, credential=credential)
    result = runtime.execute_approved_plan_ir(ir, token, commands=commands, credential=credential)
    assert result.state == PlanTransactionState.COMMITTED
    files = list(tmp_path.rglob("*.effect"))
    assert len(files) == 1 and files[0].read_bytes() == b"durable artifact"
    before = files[0].stat()
    with sqlite3.connect(tmp_path / "external.db") as conn:
        assert conn.execute("SELECT status FROM plans").fetchone() == ("COMMITTED",)
        assert conn.execute("SELECT evidence_json FROM plans").fetchone()[0]
    cold, _, _, _ = external_runtime(tmp_path)
    replay = cold.execute_approved_plan_ir(ir, token, commands=commands, credential=credential)
    assert replay.state == PlanTransactionState.COMMITTED
    after = files[0].stat()
    assert (before.st_ino, before.st_mtime_ns) == (after.st_ino, after.st_mtime_ns)


def _expire_durable_lease(database: Path, plan_id: str) -> None:
    """Move a plan's stored lease deadline into the past.

    The child in the subprocess-death test really dies (exit 73) and really
    leaves the plan ``EXECUTING``. Its lease is deliberately wide (60 s) so no
    loaded runner can expire it mid-step and turn the crash boundary into a
    ``PlanRollbackError``. Recovery consults ``plans.lease_expires_at`` and
    nothing else, so moving that one column is the deterministic equivalent of
    waiting out the lease — without a 60 s sleep that would still be a race.

    The deadline lives in the store's **monotonic** clock domain
    (``time.monotonic_ns()/1e9``, never epoch), so the backdate must use the
    same domain: the parent and the child share one boot, which is exactly what
    the store's clock-domain guard requires of two instances on one database.
    """
    import sqlite3
    import time

    with sqlite3.connect(database) as conn:
        changed = conn.execute(
            "UPDATE plans SET lease_expires_at=? WHERE project_id='project' AND plan_id=?",
            (time.monotonic() - 1.0, plan_id),
        ).rowcount
    assert changed == 1, f"expected exactly one leased plan row, updated {changed}"


@pytest.mark.parametrize("published", [False, True])
def test_subprocess_death_and_authenticated_external_recovery(
    tmp_path: Path, published: bool
) -> None:
    import json
    import os
    import sqlite3
    import subprocess
    import sys

    from nexus_ai_agent.adapters.fenced_filesystem_destination import (
        FencedFilesystemEffectDestination,
        StaleEpochError,
    )
    from nexus_ai_agent.creative.studio.durable_dedupe import PlanStoreStateError

    script = """
import json, os, runpy, sys
from pathlib import Path
from nexus_ai_agent.adapters.fenced_filesystem_destination import FencedFilesystemEffectDestination
helpers = runpy.run_path(sys.argv[1])
root = Path(sys.argv[2])
runtime, ir, commands, credential = helpers['external_runtime'](root)
token = runtime.approve_plan_ir(ir, commands=commands, credential=credential)
(root / 'token.json').write_text(token)
original = FencedFilesystemEffectDestination.apply
def crash(self, **kwargs):
    if sys.argv[3] == 'present':
        original(self, **kwargs)
    (root / 'crash_boundary.json').write_text(json.dumps({'exit': 73}))
    os._exit(73)
FencedFilesystemEffectDestination.apply = crash
runtime.execute_approved_plan_ir(ir, token, commands=commands,
    credential=credential, lease_ttl_seconds=60.0)
raise AssertionError('child unexpectedly returned')
"""
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            script,
            str(Path(__file__).resolve()),
            str(tmp_path),
            "present" if published else "absent",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    stdout, stderr = child.communicate(timeout=15)
    assert child.returncode == 73, (stdout, stderr)
    assert (tmp_path / "crash_boundary.json").is_file(), "the child never reached the crash"
    runtime, ir, _, credential = external_runtime(tmp_path)
    _expire_durable_lease(tmp_path / "external.db", ir.plan_id)
    plan_hash = json.loads((tmp_path / "token.json").read_text())[0]["binding"]["plan_hash"]
    with pytest.raises(AuthorizationError):
        runtime.inspect_recovery(
            "project",
            ir.plan_id,
            plan_hash,
            credential="forged",
            expected_status="IN_DOUBT",
            expected_attempt=1,
        )
    with sqlite3.connect(tmp_path / "external.db") as conn:
        assert conn.execute("SELECT status FROM plans").fetchone() == ("EXECUTING",)
        effect_key = conn.execute(
            "SELECT effect_key FROM compensation_journal WHERE step_id='step-1'"
        ).fetchone()[0]
    evidence = runtime.inspect_recovery(
        "project",
        ir.plan_id,
        plan_hash,
        credential=credential,
        expected_status="IN_DOUBT",
        expected_attempt=1,
    )
    with sqlite3.connect(tmp_path / "external.db") as conn:
        assert conn.execute("SELECT status FROM plans").fetchone() == ("IN_DOUBT",)
    if published:
        assert evidence.effect_evidences[-1].observation_status == "PRESENT"
        runtime.recover(
            evidence,
            credential=credential,
            resolution_action="acknowledge_present",
            reason="observed on trusted disk",
        )
        with pytest.raises(PlanStoreStateError):
            runtime.recover(
                evidence,
                credential=credential,
                resolution_action="acknowledge_present",
                reason="replay",
            )
        evidence = runtime.inspect_recovery(
            "project",
            ir.plan_id,
            plan_hash,
            credential=credential,
            expected_status="IN_DOUBT",
            expected_attempt=1,
        )
    with pytest.raises(AuthorizationError):
        runtime.recover(
            evidence, credential="forged", resolution_action="compensate_present", reason="denied"
        )
    runtime.recover(
        evidence,
        credential=credential,
        resolution_action="compensate_present" if published else "authorize_takeover",
        reason="recover dead worker",
    )
    writer = FencedFilesystemEffectDestination(tmp_path, project_id="project")
    with pytest.raises(StaleEpochError):
        writer.apply(effect_key=effect_key, fencing_epoch=1, payload=b"durable artifact")
    assert not list(tmp_path.rglob("*.effect"))
    with sqlite3.connect(tmp_path / "external.db") as conn:
        assert conn.execute("SELECT status,attempt FROM plans").fetchone() == (
            ("ROLLED_BACK" if published else "RESERVED"),
            2,
        )
    with pytest.raises(PlanStoreStateError):
        runtime.recover(
            evidence, credential=credential, resolution_action="authorize_takeover", reason="replay"
        )


def test_runtime_port_wiring_and_unauthorized_repair(tmp_path: Path) -> None:
    from nexus_ai_agent.creative.studio.durable_dedupe import PlanStoreStateError

    runtime, ir, commands, credential = external_runtime(tmp_path)
    token = runtime.approve_plan_ir(ir, commands=commands, credential=credential)
    import json

    plan_hash = json.loads(token)[0]["binding"]["plan_hash"]
    # Neither identity strings nor possession of a token opens the store's
    # context-local repair authority outside authenticated runtime ingress.
    with pytest.raises(PlanStoreStateError, match="authorization denied"):
        runtime._store.inspect_recovery(
            "project",
            ir.plan_id,
            plan_hash,
            expected_status="IN_DOUBT",
            expected_attempt=1,
            operator_id="prn_" + "a" * 32,
        )
    with pytest.raises(ValueError, match="configured durable store"):
        replace(runtime, _store=None)  # type: ignore[arg-type]


def test_independent_postcondition_checks_actual_payload(tmp_path: Path) -> None:
    import hashlib

    from nexus_ai_agent.adapters.fenced_filesystem_destination import (
        FencedFilesystemEffectDestination,
        FilesystemObservationVerifier,
    )

    destination = FencedFilesystemEffectDestination(tmp_path, project_id="project")
    reader = FilesystemObservationVerifier(tmp_path, project_id="project")
    observed = destination.apply(effect_key="f" * 64, fencing_epoch=1, payload=b"actual")
    assert reader.verify(observed)
    assert reader.verify_payload(observed, hashlib.sha256(b"actual").hexdigest())
    assert not reader.verify_payload(observed, hashlib.sha256(b"claimed").hexdigest())


def test_repair_scope_requires_permission_and_cannot_leak_to_another_thread() -> None:
    from concurrent.futures import ThreadPoolExecutor

    from nexus_ai_agent.adapters.durable_studio_runtime import ProvisionedPrincipalAuthority

    principal = PrincipalId("prn_" + "a" * 32)
    actor = ActorIdentity(kind="user", actor_id=str(principal))
    credential = "r" * 32
    readonly = ProvisionedPrincipalAuthority(
        {credential: principal},
        b"k" * 32,
        ProvisionedProjectAuthorizer(
            {str(principal): ProjectAccess(actor, "p", frozenset({"project:read"}))}
        ),
    )
    with pytest.raises(AuthorizationError):
        with readonly.repair_session(credential, "p"):
            pytest.fail("read-only principal entered repair scope")
    authority = ProvisionedPrincipalAuthority(
        {credential: principal},
        b"k" * 32,
        ProvisionedProjectAuthorizer(
            {str(principal): ProjectAccess(actor, "p", frozenset({"project:repair"}))}
        ),
    )
    assert not authority.authorize_repair("p", actor.actor_id, "IN_DOUBT", 1)
    with authority.repair_session(credential, "p"):
        assert authority.authorize_repair("p", actor.actor_id, "IN_DOUBT", 1)
        assert not authority.authorize_repair("other", actor.actor_id, "IN_DOUBT", 1)
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert not pool.submit(
                authority.authorize_repair, "p", actor.actor_id, "IN_DOUBT", 1
            ).result()
    assert not authority.authorize_repair("p", actor.actor_id, "IN_DOUBT", 1)


def test_public_command_helpers_revalidate_and_preserve_semantic_identity() -> None:
    from nexus_ai_agent.creative.studio.bus import command_fingerprint, parse_command
    from nexus_ai_agent.creative.studio.models import CommandValidationError

    command = TypedCommand(command_id="one", operation="media.play")
    assert parse_command(command) == command
    assert command_fingerprint(command) == command_fingerprint(
        command.model_copy(update={"command_id": "two"})
    )
    with pytest.raises(CommandValidationError):
        parse_command('{"operation":"media.play","operation":"media.pause"}')
