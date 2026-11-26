"""CP4 boundary tests. Fault-injected authorities are explicitly test doubles."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel

from nexus_ai_agent.adapters import durable_studio_runtime as composition
from nexus_ai_agent.adapters.durable_plan_dedupe import DurablePlanDedupeStore
from nexus_ai_agent.adapters.fenced_filesystem_destination import (
    DestinationStateError,
    FencedFilesystemEffectDestination,
    FilesystemObservationVerifier,
    StaleEpochError,
)
from nexus_ai_agent.application.principal import PrincipalId
from nexus_ai_agent.creative.studio.authorization import ProjectAccess
from nexus_ai_agent.creative.studio.bus import (
    CommandBus,
    PlanStep,
    PlanTransactionError,
    command_fingerprint,
)
from nexus_ai_agent.creative.studio.capabilities import (
    CapabilityRegistry,
    OperationContext,
    OperationOutcome,
    OperationSpec,
    PlayCommandInput,
    build_wave1_registry,
)
from nexus_ai_agent.creative.studio.durable_dedupe import ExternalEffectObservation
from nexus_ai_agent.creative.studio.models import (
    ActorIdentity,
    AuthorizationError,
    PermissionLevel,
    Project,
    TargetRef,
    Timeline,
    TypedCommand,
    new_project,
)
from nexus_ai_agent.creative.studio.plan_execution import compile_plan, execute_durable_ir
from nexus_ai_agent.creative.studio.plan_ir import PlanIR, PlanNode
from nexus_ai_agent.creative.studio.runtime import DurableStudioExecutionRuntime


@pytest.fixture
def project() -> Project:
    return new_project("project", "test", Timeline(timeline_id="timeline", duration_us=0))


def test_ir_rejects_untyped_containers_and_compiler_refuses_fallback(project: Project) -> None:
    malformed: Any = ["parent"]
    with pytest.raises(ValueError, match="bounded tuple"):
        PlanNode("a", "op", "a" * 64, malformed)
    malformed = (object(),)
    with pytest.raises(ValueError, match="typed nodes"):
        PlanIR("project", "p", malformed)
    malformed = object()
    with pytest.raises(ValueError, match="typed PlanIR"):
        compile_plan(malformed, {})
    command = TypedCommand(
        command_id="c", operation="media.play", target=TargetRef(project_id="project")
    )
    ir = PlanIR("project", "p", (PlanNode("a", command.operation, command_fingerprint(command)),))
    with pytest.raises(PlanTransactionError, match="store is not configured"):
        execute_durable_ir(
            CommandBus(project),
            ir,
            {"a": command},
            approval_attestations=(),
            policy_version="v1",
            authenticated_actor=ActorIdentity(kind="user", actor_id="a"),
            approval_verifier=AllowVerifier(),
            deadline_monotonic_ns=1,
        )


class AllowVerifier:
    def verify(self, binding: object) -> bool:
        return True


class MismatchedAuthority:
    def authorize(self, actor: ActorIdentity, project_id: str) -> ProjectAccess:
        return ProjectAccess(actor, "another", frozenset({"project:repair", "project:approve"}))


def test_authority_configuration_and_mismatched_grants(tmp_path: Path, project: Project) -> None:
    principal = PrincipalId("prn_" + "a" * 32)
    for credentials in ({}, {"short": principal}):
        with pytest.raises(ValueError, match="bearer"):
            composition.ProvisionedPrincipalAuthority(credentials, b"k" * 32)
    authority = composition.ProvisionedPrincipalAuthority({"c" * 32: principal}, b"k" * 32)
    with pytest.raises(AuthorizationError, match="not composed"):
        with authority.repair_session("c" * 32, "project"):
            pytest.fail("unconfigured repair entered")
    with pytest.raises(AuthorizationError):
        authority.authenticate("c" * 4097)
    actor = authority.authenticate("c" * 32)
    with pytest.raises(AuthorizationError, match="no project access"):
        composition.ProvisionedProjectAuthorizer({}).authorize(actor, "project")
    bad = MismatchedAuthority()
    authority = composition.ProvisionedPrincipalAuthority({"c" * 32: principal}, b"k" * 32, bad)
    with pytest.raises(AuthorizationError, match="mismatched"):
        with authority.repair_session("c" * 32, "project"):
            pytest.fail("wrong project entered")
    store = DurablePlanDedupeStore(tmp_path / "db")
    runtime = DurableStudioExecutionRuntime(
        CommandBus(project, durable_store=store, authorizer=bad), store, authority, bad
    )
    command = TypedCommand(
        command_id="c", operation="media.play", actor=actor, target=TargetRef(project_id="project")
    )
    ir = PlanIR("project", "p", (PlanNode("a", command.operation, command_fingerprint(command)),))
    with pytest.raises(AuthorizationError, match="mismatched"):
        runtime.approve_plan_ir(ir, commands={"a": command}, credential="c" * 32)


def test_artifact_preconditions_and_compensation(tmp_path: Path, project: Project) -> None:
    destination = FencedFilesystemEffectDestination(tmp_path, project_id="project")
    reader = FilesystemObservationVerifier(tmp_path, project_id="project")
    command = TypedCommand(command_id="c", operation="artifact.publish", input={"content": "bytes"})
    context = OperationContext(command, command.input, ())
    with pytest.raises(RuntimeError, match="not composed"):
        composition._uncomposed_artifact(project, context)
    handler = composition._artifact_handler(destination)
    with pytest.raises(RuntimeError, match="live durable fence"):
        handler(project, context)
    bounded = replace(
        context,
        input_data={"content": "é" * 600_000},
        effect_key="a" * 64,
        fencing_epoch=1,
        durable_heartbeat=lambda: None,
    )
    with pytest.raises(ValueError, match="byte limit"):
        handler(project, bounded)
    result = handler(project, replace(bounded, input_data=command.input))
    compensate = composition._artifact_compensation(str(tmp_path), "project")
    with pytest.raises(RuntimeError, match="authenticated recovery"):
        compensate(None)
    assert compensate(SimpleNamespace(output=result.output)) is True
    assert destination.inspect(effect_key="a" * 64, fencing_epoch=1).status == "ABSENT"
    invalid: Any = SimpleNamespace(output={})
    assert not composition._artifact_verifier(reader)(project, command, invalid, "a" * 64)


def test_destination_compensation_refuses_stale_or_changed_evidence(tmp_path: Path) -> None:
    destination = FencedFilesystemEffectDestination(tmp_path, project_id="project")
    reader = FilesystemObservationVerifier(tmp_path, project_id="project")
    unknown = ExternalEffectObservation("a" * 64, 1, "UNKNOWN", "")
    with pytest.raises(DestinationStateError, match="PRESENT"):
        destination.compensate(unknown)
    assert not reader.verify_payload(unknown, "a" * 64)
    forged = ExternalEffectObservation("a" * 64, 1, "PRESENT", "b" * 64)
    with pytest.raises(DestinationStateError, match="tombstone"):
        destination.compensate(forged)
    observed = destination.apply(effect_key="a" * 64, fencing_epoch=1, payload=b"data")
    with pytest.raises(DestinationStateError, match="changed"):
        destination.compensate(forged)
    destination.apply(effect_key="a" * 64, fencing_epoch=2, payload=b"data")
    with pytest.raises(StaleEpochError, match="newer epoch"):
        destination.compensate(observed)


def noop(project: Project, context: OperationContext) -> OperationOutcome:
    return OperationOutcome(project, context.history, {})


@pytest.mark.parametrize("change", ["domain", "duplicate", "input", "schema", "mode", "contract"])
def test_registry_refuses_invalid_registration(change: str) -> None:
    registry = CapabilityRegistry()
    spec = OperationSpec("example.play", "test", PermissionLevel.IMMEDIATE, PlayCommandInput, noop)
    if change == "domain":
        spec = replace(spec, operation_id="other.play")
    elif change == "duplicate":
        registry.register_operation("example", "play", spec)
    elif change == "input":

        class PermissiveInput(BaseModel):
            pass

        spec = replace(spec, input_model=PermissiveInput)
    elif change == "schema":
        spec = replace(spec, schema_version=0)
    elif change == "mode":
        invalid_mode: Any = ("remote",)
        spec = replace(spec, execution_modes=invalid_mode)
    elif change == "contract":
        registry.register_operation("example", "play", replace(spec, external_effects=True))
        with pytest.raises(ValueError, match="verification contracts"):
            registry.verify_declared_contracts({})
        return
    with pytest.raises(ValueError):
        registry.register_operation("example", "play", spec)


def test_registry_domain_filter_and_version_rejection() -> None:
    registry = build_wave1_registry()
    assert registry.list_operations("media") == ["media.pause", "media.play"]
    assert registry.list_operations("absent") == []
    with pytest.raises(ValueError):
        registry.register_operation(
            "example",
            "play",
            OperationSpec(
                "example.play", "test", PermissionLevel.IMMEDIATE, PlayCommandInput, noop
            ),
            capability_version="bad",
        )


@pytest.mark.parametrize("failure", ["root", "schema", "duplicate", "external"])
def test_composition_rejects_invalid_deployment(
    tmp_path: Path, project: Project, failure: str
) -> None:
    import json

    principal = PrincipalId("prn_" + "a" * 32)
    actor = ActorIdentity(kind="user", actor_id=str(principal))
    registry = build_wave1_registry()
    if failure == "external":
        registry.register_operation(
            "example",
            "play",
            OperationSpec(
                "example.play",
                "test",
                PermissionLevel.IMMEDIATE,
                PlayCommandInput,
                noop,
                external_effects=True,
            ),
        )
    truth = {
        "schema": "bad" if failure == "schema" else "nagar.operation_truth.v1",
        "operations": [{"operation_id": "duplicate", "evidence": {}}] * 2,
    }
    truth_path = tmp_path / "truth.json"
    truth_path.write_text(json.dumps(truth))
    with pytest.raises(ValueError):
        composition.build_production_durable_command_bus(
            tmp_path / "db",
            tmp_path / "missing" if failure == "root" else tmp_path,
            approval_secret="k" * 32,
            project=project,
            credentials={"c" * 32: principal},
            authorizer=ProjectAccess(actor, "project", frozenset()),
            verifiers={},
            registry=registry,
            operation_truth_path=truth_path,
        )


def recovery_store(
    tmp_path: Path, published: bool = False
) -> tuple[DurablePlanDedupeStore, FencedFilesystemEffectDestination]:
    from nexus_ai_agent.creative.studio.durable_dedupe import RecoveryRoute

    destination = FencedFilesystemEffectDestination(tmp_path, project_id="default")
    verifier = FilesystemObservationVerifier(tmp_path, project_id="default")
    store = DurablePlanDedupeStore(
        tmp_path / "db",
        repair_authorizer=lambda *_args: True,
        recovery_routes={
            ("default", "step"): RecoveryRoute(destination, verifier, "reader", destination)
        },
    )
    lease = store.reserve_and_start("p", "a" * 64, nonce="n", external_effects=True)
    store.record_compensation_intent(
        "p",
        "a" * 64,
        attempt=lease.attempt,
        owner_token=lease.owner_token,
        step_id="step",
        effect_key="b" * 64,
    )
    if published:
        destination.apply(effect_key="b" * 64, fencing_epoch=1, payload=b"payload")
    store.quarantine("p", "a" * 64, attempt=lease.attempt, owner_token=lease.owner_token)
    return store, destination


@pytest.mark.parametrize(
    "failure",
    ["invalid_status", "invalid_attempt", "capacity", "journal_cas", "incomplete_snapshot"],
)
def test_recovery_issuance_and_atomic_journal_guards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    import sqlite3

    from nexus_ai_agent.adapters import durable_plan_dedupe as adapter
    from nexus_ai_agent.creative.studio.durable_dedupe import (
        PlanStoreCapacityError,
        PlanStoreStateError,
    )

    store, _ = recovery_store(tmp_path)
    kwargs: dict[str, Any] = dict(expected_status="IN_DOUBT", expected_attempt=1, operator_id="op")
    if failure.startswith("invalid"):
        kwargs.update(
            {"expected_status": "EXECUTING"}
            if failure == "invalid_status"
            else {"expected_attempt": True}
        )
        with pytest.raises(ValueError):
            store.inspect_recovery("default", "p", "a" * 64, **kwargs)
        return
    if failure == "capacity":
        monkeypatch.setattr(adapter, "MAX_RECORDS", 0)
        with pytest.raises(PlanStoreCapacityError, match="inspection capacity"):
            store.inspect_recovery("default", "p", "a" * 64, **kwargs)
        return
    evidence = store.inspect_recovery("default", "p", "a" * 64, **kwargs)
    if failure == "journal_cas":
        with sqlite3.connect(store.path) as conn:
            conn.execute(
                "CREATE TRIGGER suppress_journal BEFORE UPDATE ON compensation_journal "
                "BEGIN SELECT RAISE(IGNORE); END"
            )
    else:
        # Privileged DB corruption tests the second coverage guard even when
        # the stored issuance row itself has been damaged (not a forged caller).
        evidence = replace(evidence, effect_evidences=())
        with sqlite3.connect(store.path) as conn:
            conn.execute(
                "UPDATE plan_recovery_inspections SET evidence_json=?",
                (store._recovery_json(evidence),),
            )
    with pytest.raises(PlanStoreStateError):
        store.repair_in_doubt(
            "default",
            "p",
            "a" * 64,
            recovery_evidence=evidence,
            resolution_action="authorize_takeover",
            reason="checked",
            **kwargs,
        )
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT status FROM plans").fetchone() == ("IN_DOUBT",)
        assert conn.execute("SELECT consumed FROM plan_recovery_inspections").fetchone() == (0,)


@pytest.mark.parametrize("failure", ["missing_route", "changed_destination", "bad_postcondition"])
def test_present_compensation_refuses_unproven_routes_and_results(
    tmp_path: Path, failure: str
) -> None:
    from nexus_ai_agent.creative.studio.durable_dedupe import PlanStoreStateError, RecoveryRoute

    store, destination = recovery_store(tmp_path, published=True)
    evidence = store.inspect_recovery(
        "default", "p", "a" * 64, expected_status="IN_DOUBT", expected_attempt=1, operator_id="op"
    )
    if failure == "missing_route":
        store._recovery_routes = {}
    elif failure == "changed_destination":
        destination.apply(effect_key="b" * 64, fencing_epoch=2, payload=b"payload")
    else:

        class WrongCompensator:
            def compensate(
                self, observation: ExternalEffectObservation
            ) -> ExternalEffectObservation:
                return observation  # Test double falsely claiming compensation without removal.

        store._recovery_routes = {
            ("default", "step"): RecoveryRoute(
                destination,
                FilesystemObservationVerifier(tmp_path, project_id="default"),
                "reader",
                WrongCompensator(),
            )
        }
    with pytest.raises(PlanStoreStateError):
        store.repair_in_doubt(
            "default",
            "p",
            "a" * 64,
            expected_status="IN_DOUBT",
            expected_attempt=1,
            operator_id="op",
            resolution_action="compensate_present",
            reason="checked",
            recovery_evidence=evidence,
        )


@pytest.mark.parametrize("atomic", [True, False])
def test_legacy_takeover_without_reserved_epoch_is_rejected(tmp_path: Path, atomic: bool) -> None:
    import sqlite3

    from nexus_ai_agent.creative.studio.durable_dedupe import PlanStoreStateError

    store = DurablePlanDedupeStore(tmp_path / "db")
    store.reserve("p", "a" * 64, external_effects=True)
    with sqlite3.connect(store.path) as conn:
        conn.execute("UPDATE plans SET takeover_ready=1,attempt=1")
    with pytest.raises(PlanStoreStateError, match="legacy takeover"):
        if atomic:
            store.reserve_and_start("p", "a" * 64, external_effects=True, nonce="n")
        else:
            store.start("p", "a" * 64, nonce="n")


def test_invalid_recovery_route_configuration(tmp_path: Path) -> None:
    from nexus_ai_agent.creative.studio.durable_dedupe import RecoveryRoute

    invalid: Any = object()
    with pytest.raises(ValueError, match="configured"):
        DurablePlanDedupeStore(tmp_path / "db", recovery_routes={("p", "step"): invalid})
    destination: Any = FencedFilesystemEffectDestination(tmp_path, project_id="p")
    with pytest.raises(ValueError, match="separate"):
        DurablePlanDedupeStore(
            tmp_path / "db",
            recovery_routes={("p", "step"): RecoveryRoute(destination, destination, "same")},
        )


def test_bus_constructor_and_plan_shape_refusals(tmp_path: Path, project: Project) -> None:
    from nexus_ai_agent.creative.studio.bus import compose_durable_command_bus
    from nexus_ai_agent.creative.studio.models import CapabilityError

    store = DurablePlanDedupeStore(tmp_path / "db")
    with pytest.raises(ValueError, match="only one"):
        CommandBus(project, durable_store=store, durable_dedupe_store=store)
    invalid: Any = None
    with pytest.raises(ValueError, match="callable"):
        CommandBus(project, durable_verifiers={"media.play": invalid})
    with pytest.raises(CapabilityError, match="unknown engine"):
        CommandBus(project, engine_ports={"unknown": object()})
    with pytest.raises(ValueError, match="cold bus"):
        CommandBus(None)
    with pytest.raises(ValueError, match="authority"):
        compose_durable_command_bus(
            project, registry=build_wave1_registry(), authorizer=invalid, store=store, verifiers={}
        )
    with pytest.raises(ValueError, match="callable"):
        PlanStep("op", execute=lambda: None, compensate=42)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="require compensation"):
        PlanStep("op", execute=lambda: None, external_effects=True)


def test_callback_evidence_handles_unreplayable_and_recursive_values() -> None:
    from functools import partial

    from nexus_ai_agent.creative.studio.bus import _callable_evidence, _replay_safe, _stable_value

    assert _stable_value(1.5, frozenset()) == 1.5
    assert not _replay_safe(_stable_value(float("nan"), frozenset()))
    assert _stable_value(b"a", frozenset()) == {"bytes": "61"}
    recursive: list[Any] = []
    recursive.append(recursive)
    assert not _replay_safe(_stable_value(recursive, frozenset()))
    function = lambda: True  # noqa: E731 - exercise callable identity recursion
    assert not _replay_safe(_callable_evidence(function, frozenset({id(function)})))
    wrapped = partial(int, "10", base=16)
    assert _replay_safe(_callable_evidence(wrapped))
    assert "partial" in _callable_evidence(wrapped)


def cold_fixture(tmp_path: Path, project: Project) -> tuple[Any, ...]:
    from nexus_ai_agent.creative.studio.bus import PlanTransaction

    principal = PrincipalId("prn_" + "a" * 32)
    actor = ActorIdentity(kind="user", actor_id=str(principal))
    access = ProjectAccess(actor, "project", frozenset({"project:read", "project:repair"}))
    store = DurablePlanDedupeStore(tmp_path / "db")
    verifier = lambda p, c, r, k: p.timeline.playhead.is_playing  # noqa: E731
    bus = CommandBus(
        project, authorizer=access, durable_store=store, durable_verifiers={"media.play": verifier}
    )
    command = TypedCommand(
        command_id="c",
        operation="media.play",
        actor=actor,
        target=TargetRef(project_id="project"),
        idempotency_key="once",
    )
    plan = PlanTransaction(plan_id="p", steps=(PlanStep("media.play", command=command),))
    requirements = bus.durable_approval_requirements(
        plan, policy_version="v1", authenticated_actor=actor
    )
    issuer = composition.ProvisionedPrincipalAuthority({"c" * 32: principal}, b"k" * 32)
    bindings, authority = issuer.verify_token(issuer.issue(requirements))
    bus.dispatch_plan(
        plan,
        approval_attestations=bindings,
        policy_version="v1",
        authenticated_actor=actor,
        approval_verifier=authority,
        deadline_monotonic_ns=store.monotonic_ns() + 10**10,
    )
    cold = CommandBus(
        project, authorizer=access, durable_store=store, durable_verifiers={"media.play": verifier}
    )
    return cold, store, PlanTransaction(plan_id="p", steps=plan.steps), bindings, authority, actor


@pytest.mark.parametrize(
    "failure",
    [
        "executing",
        "finished",
        "actor_type",
        "bindings_type",
        "actor_binding",
        "grant",
        "duplicate_approvals",
        "missing_store",
    ],
)
def test_cold_replay_checks_authority_before_receipt(
    tmp_path: Path, project: Project, failure: str
) -> None:
    from nexus_ai_agent.creative.studio.bus import PlanTransactionState
    from nexus_ai_agent.creative.studio.durable_dedupe import ApprovalBindingError

    bus, store, plan, bindings, authority, actor = cold_fixture(tmp_path, project)
    if failure == "executing":
        bus._executing = True
    elif failure == "finished":
        plan.state = PlanTransactionState.COMMITTED
    elif failure == "actor_type":
        actor = "forged"
    elif failure == "bindings_type":
        bindings = [bindings[0]]
    elif failure == "actor_binding":
        bindings = (replace(bindings[0], actor_id="other"),)
    elif failure == "grant":
        bus._authorizer = MismatchedAuthority()
    elif failure == "duplicate_approvals":
        bindings = (bindings[0], bindings[0])
    elif failure == "missing_store":
        bus._durable_store = None
        assert (
            bus._try_cold_durable_replay(
                plan,
                bindings,
                policy_version="v1",
                authenticated_actor=actor,
                approval_verifier=authority,
                deadline_monotonic_ns=store.monotonic_ns() + 10**10,
            )
            is None
        )
        return
    with pytest.raises((PlanTransactionError, ApprovalBindingError, AuthorizationError)):
        bus._try_cold_durable_replay(
            plan,
            bindings,
            policy_version="v1",
            authenticated_actor=actor,
            approval_verifier=authority,
            deadline_monotonic_ns=store.monotonic_ns() + 10**10,
        )
    assert bus.project == project


@pytest.mark.parametrize(
    "failure",
    [
        "size",
        "legacy",
        "base_key",
        "base_hash",
        "project",
        "external_scope",
        "trace_chain",
        "trace_result",
        "prior_result",
    ],
)
def test_cold_replay_rejects_corrupt_persisted_receipts(
    tmp_path: Path, project: Project, failure: str
) -> None:
    import json
    import sqlite3

    from nexus_ai_agent.creative.studio.bus import _durable_hash
    from nexus_ai_agent.creative.studio.durable_dedupe import PlanStoreStateError

    bus, store, plan, bindings, authority, actor = cold_fixture(tmp_path, project)
    with sqlite3.connect(store.path) as conn:
        receipt = json.loads(conn.execute("SELECT evidence_json FROM plans").fetchone()[0])
    payload = receipt["payload"]
    if failure == "size":
        encoded = " " * 65537
    else:
        if failure == "legacy":
            del payload["before_project"]
            bus._project = None
        elif failure == "base_key":
            payload["before_idempotency"] = [{"key": ["wrong"], "result": None}]
        elif failure == "base_hash":
            payload["before_hash"] = "f" * 64
        elif failure == "project":
            bus._project = new_project("other", "other", Timeline(timeline_id="t", duration_us=0))
        elif failure == "external_scope":
            with sqlite3.connect(store.path) as conn:
                conn.execute("UPDATE plans SET external_effects=1")
        elif failure == "trace_chain":
            payload["trace"][0]["parent_revision"] += 1
        elif failure == "trace_result":
            payload["trace"][0]["new_state_hash"] = "f" * 64
        elif failure == "prior_result":
            payload["idempotency"][0]["result"]["state_hash"] = "f" * 64
        receipt["digest"] = _durable_hash(payload)
        encoded = json.dumps(receipt)
    with sqlite3.connect(store.path) as conn:
        conn.execute("UPDATE plans SET evidence_json=?", (encoded,))
    before = bus._project
    with pytest.raises(PlanStoreStateError):
        bus._try_cold_durable_replay(
            plan,
            bindings,
            policy_version="v1",
            authenticated_actor=actor,
            approval_verifier=authority,
            deadline_monotonic_ns=store.monotonic_ns() + 10**10,
        )
    assert bus._project == before


@pytest.mark.parametrize("repair", [False, True])
@pytest.mark.parametrize("failure", ["unavailable", "actor", "grant"])
def test_recovery_bus_authority_guards(
    tmp_path: Path, project: Project, repair: bool, failure: str
) -> None:
    actor: Any = ActorIdentity(kind="user", actor_id="alice")
    bus = CommandBus(
        project,
        durable_store=DurablePlanDedupeStore(tmp_path / "db"),
        authorizer=MismatchedAuthority(),
    )
    if failure == "unavailable":
        bus._executing = True
    elif failure == "actor":
        actor = "alice"
    with pytest.raises(AuthorizationError):
        if repair:
            bus.recover_durable_plan(
                "p",
                "a" * 64,
                authenticated_operator=actor,
                expected_status="IN_DOUBT",
                expected_attempt=1,
                resolution_action="authorize_takeover",
                reason="checked",
            )
        else:
            bus.inspect_durable_recovery(
                "p",
                "a" * 64,
                authenticated_operator=actor,
                expected_status="IN_DOUBT",
                expected_attempt=1,
            )


@pytest.mark.parametrize("failure", ["deadline", "store", "size", "chain", "result"])
def test_receipt_rehydration_final_guards(tmp_path: Path, project: Project, failure: str) -> None:
    import json
    import sqlite3

    from nexus_ai_agent.creative.studio.bus import _durable_hash
    from nexus_ai_agent.creative.studio.durable_dedupe import PlanStoreStateError

    bus, store, plan, bindings, authority, actor = cold_fixture(tmp_path, project)
    expected = bus.durable_approval_requirements(
        plan, policy_version="v1", authenticated_actor=actor
    )[0]
    with sqlite3.connect(store.path) as conn:
        raw = conn.execute("SELECT evidence_json FROM plans").fetchone()[0]
    deadline = None
    if failure == "deadline":
        deadline = 0
    elif failure == "store":
        deadline = 1
        bus._durable_store = None
    elif failure == "size":
        raw = "x" * 65537
    else:
        receipt = json.loads(raw)
        payload = receipt["payload"]
        field = "previous_state_hash" if failure == "chain" else "new_state_hash"
        payload["history"][-1][field] = "f" * 64
        payload["trace"][-1][field] = "f" * 64
        receipt["digest"] = _durable_hash(payload)
        raw = json.dumps(receipt)
    with pytest.raises((TimeoutError, PlanStoreStateError)):
        bus._rehydrate_durable_receipt(plan, expected, raw, deadline_monotonic_ns=deadline)
    assert bus.project == project


@pytest.mark.parametrize(
    "failure", ["deadline_before", "deadline_after", "receipt_changed", "legacy_warm"]
)
def test_replay_final_cas_and_deadlines(
    tmp_path: Path, project: Project, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    import json
    import sqlite3

    from nexus_ai_agent.creative.studio.bus import _durable_hash
    from nexus_ai_agent.creative.studio.durable_dedupe import PlanStoreStateError

    bus, store, plan, bindings, authority, actor = cold_fixture(tmp_path, project)
    if failure == "legacy_warm":
        with sqlite3.connect(store.path) as conn:
            receipt = json.loads(conn.execute("SELECT evidence_json FROM plans").fetchone()[0])
            del receipt["payload"]["before_project"]
            receipt["digest"] = _durable_hash(receipt["payload"])
            conn.execute("UPDATE plans SET evidence_json=?", (json.dumps(receipt),))
        assert (
            bus._try_cold_durable_replay(
                plan,
                bindings,
                policy_version="v1",
                authenticated_actor=actor,
                approval_verifier=authority,
                deadline_monotonic_ns=store.monotonic_ns() + 10**10,
            )
            is None
        )
        return
    if failure == "deadline_before":
        monkeypatch.setattr(store, "monotonic_ns", lambda: 100)
    elif failure == "deadline_after":
        times = iter([0, 100])
        monkeypatch.setattr(store, "monotonic_ns", lambda: next(times))
    else:
        original = store.replay_committed

        def changed(*args: Any, **kwargs: Any) -> Any:
            return replace(original(*args, **kwargs), evidence_json="{}")

        monkeypatch.setattr(store, "replay_committed", changed)
        monkeypatch.setattr(store, "monotonic_ns", lambda: 0)
    with pytest.raises((TimeoutError, PlanStoreStateError)):
        bus._try_cold_durable_replay(
            plan,
            bindings,
            policy_version="v1",
            authenticated_actor=actor,
            approval_verifier=authority,
            deadline_monotonic_ns=50,
        )
    assert bus.project == project


@pytest.mark.parametrize(
    "failure",
    [
        "mark",
        "step",
        "finish",
        "quarantine",
        "false_compensation",
        "changed_compensation",
        "no_callback",
    ],
)
def test_durable_cleanup_failures_never_publish_receipts(
    tmp_path: Path, project: Project, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    import sqlite3

    from nexus_ai_agent.creative.studio.bus import PlanRollbackError, PlanTransaction

    actor = ActorIdentity(kind="user", actor_id="alice")
    registry = CapabilityRegistry()

    def handler(state: Project, context: OperationContext) -> OperationOutcome:
        if context.command.command_id == "second" and failure != "finish":
            if failure == "changed_compensation":
                plan._accepted_callbacks = ("changed", *plan._accepted_callbacks[1:])
            raise RuntimeError("second handler failed")
        return OperationOutcome(state, context.history, {"ok": True})

    registry.register_operation(
        "example",
        "play",
        OperationSpec("example.play", "test", PermissionLevel.IMMEDIATE, PlayCommandInput, handler),
    )
    store = DurablePlanDedupeStore(tmp_path / "db")
    bus = CommandBus(
        project,
        registry=registry,
        authorizer=ProjectAccess(actor, "project", frozenset({"project:read"})),
        durable_store=store,
        durable_verifiers={"example.play": lambda *args: True},
    )

    def compensate(result: Any) -> bool:
        return failure != "false_compensation"

    plan = PlanTransaction(
        plan_id="p",
        steps=tuple(
            PlanStep(
                "example.play",
                command=TypedCommand(
                    command_id=name, operation="example.play", actor=actor, idempotency_key=name
                ),
                compensate=None if failure == "no_callback" else compensate,
            )
            for name in ("first", "second")
        ),
    )
    requirements = bus.durable_approval_requirements(
        plan, policy_version="v1", authenticated_actor=actor
    )
    issuer = composition.ProvisionedPrincipalAuthority(
        {"c" * 32: PrincipalId("prn_" + "a" * 32)}, b"k" * 32
    )
    bindings, authority = issuer.verify_token(issuer.issue(requirements))

    def fail(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("injected persistence failure")

    if failure in ("mark", "step", "quarantine"):
        monkeypatch.setattr(
            store,
            {
                "mark": "mark_compensating",
                "step": "record_compensation_step",
                "quarantine": "quarantine",
            }[failure],
            fail,
        )
    elif failure == "finish":
        monkeypatch.setattr(store, "finish", fail)
        monkeypatch.setattr(store, "record_compensation", fail)
    from nexus_ai_agent.creative.studio.models import CommandExecutionError

    with pytest.raises((PlanRollbackError, CommandExecutionError, RuntimeError)):
        bus.dispatch_plan(
            plan,
            approval_attestations=bindings,
            policy_version="v1",
            authenticated_actor=actor,
            approval_verifier=authority,
            deadline_monotonic_ns=store.monotonic_ns() + 10**10,
        )
    assert bus.project == project
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT status FROM plans").fetchone() != ("COMMITTED",)


def test_handler_reference_errors_do_not_mutate_project(project: Project) -> None:
    from nexus_ai_agent.creative.studio.capabilities import (
        _resolved_playhead,
        _timeline_split_at_playhead,
    )
    from nexus_ai_agent.creative.studio.models import CommandValidationError

    with pytest.raises(CommandValidationError, match="reference field"):
        _resolved_playhead({}, "at")
    command = TypedCommand(
        command_id="c",
        operation="timeline.split_at_playhead",
        target=TargetRef(track_id="missing", clip_id="missing"),
    )
    context = OperationContext(
        command, {"at": project.timeline.playhead.model_dump(mode="json")}, ()
    )
    with pytest.raises(CommandValidationError, match="track not found"):
        _timeline_split_at_playhead(project, context)


@pytest.mark.parametrize("failure", ["missing_store", "actor", "expiry", "reference"])
def test_durable_boundary_rechecks_live_authority(
    tmp_path: Path, project: Project, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    from nexus_ai_agent.creative.studio.bus import parse_command
    from nexus_ai_agent.creative.studio.durable_dedupe import ApprovalBindingError

    bus, store, plan, bindings, authority, actor = cold_fixture(tmp_path, project)
    if failure == "reference":
        command = parse_command(plan.steps[0].command).model_copy(
            update={"input": {"start": project.timeline.playhead.model_dump(mode="json")}}
        )
        plan.steps = (PlanStep("media.play", command=command),)
    expected = bus.durable_approval_requirements(
        plan, policy_version="v1", authenticated_actor=actor
    )[0]
    issuer = composition.ProvisionedPrincipalAuthority(
        {"c" * 32: PrincipalId("prn_" + "a" * 32)}, b"k" * 32
    )
    bindings, authority = issuer.verify_token(issuer.issue((expected,)))
    command = parse_command(plan.steps[0].command)
    if failure == "missing_store":
        bus._durable_store = None
    elif failure == "actor":
        actor = ActorIdentity(kind="user", actor_id="other")
    elif failure == "expiry":
        clock = iter([bindings[0].issued_at + 1, bindings[0].expires_at + 1])
        monkeypatch.setattr(store, "utc_timestamp", lambda: next(clock))
    if failure == "reference":
        bus._validate_durable_boundary(
            plan,
            0,
            plan.steps[0],
            command,
            expected,
            bindings[0],
            authenticated_actor=actor,
            approval_verifier=authority,
        )
        bus.dispatch(command)
    else:
        with pytest.raises((PlanTransactionError, AuthorizationError, ApprovalBindingError)):
            bus._validate_durable_boundary(
                plan,
                0,
                plan.steps[0],
                command,
                expected,
                bindings[0],
                authenticated_actor=actor,
                approval_verifier=authority,
            )


def test_in_memory_cache_and_undo_replay_guards(project: Project) -> None:
    from nexus_ai_agent.creative.studio.bus import PlanTransaction, _snapshot_cache_bytes

    assert _snapshot_cache_bytes({"p": ("hash", SimpleNamespace(_snapshot=None))}) == 0
    opaque = object()

    def callback() -> bool:
        return opaque is not None

    plan = PlanTransaction(
        plan_id="opaque", steps=(PlanStep("custom", execute=callback, compensate=lambda _: True),)
    )
    bus = CommandBus(project)
    plan.commit(bus)
    duplicate = PlanTransaction(plan_id="opaque", steps=plan.steps)
    with pytest.raises(PlanTransactionError, match="opaque"):
        duplicate.commit(bus)
    actor = ActorIdentity(kind="user", actor_id="alice")
    access = ProjectAccess(actor, "project", frozenset({"project:read"}))
    bus = CommandBus(project, authorizer=access)
    plan = PlanTransaction(
        steps=(PlanStep("custom", execute=lambda: True, compensate=lambda _: True),)
    )
    plan.commit(bus)
    with pytest.raises(AuthorizationError, match="custom undo"):
        plan.rollback_committed_plan()
    plan.rollback_committed_plan(
        authorization=TypedCommand(command_id="auth", operation="media.play", actor=actor)
    )


def test_dispatch_refuses_changed_spec_inflight_key_and_optin(project: Project) -> None:
    from nexus_ai_agent.creative.studio.bus import _Reservation
    from nexus_ai_agent.creative.studio.models import CommandExecutionError, ExecutionPolicyError

    bus = CommandBus(project)
    command = TypedCommand(command_id="c", operation="media.play", idempotency_key="busy")
    with pytest.raises(PlanTransactionError, match="capability changed"):
        bus._dispatch_locked(
            command,
            expected_spec=replace(bus.registry.get_spec("media.play"), description="changed"),
        )
    bus._idempotency[("project", "media.play", "busy")] = _Reservation(command_fingerprint(command))
    with pytest.raises(CommandExecutionError, match="already executing"):
        bus.dispatch(command)
    registry = CapabilityRegistry()
    registry.register_operation(
        "example",
        "play",
        OperationSpec(
            "example.play",
            "test",
            PermissionLevel.IMMEDIATE,
            PlayCommandInput,
            noop,
            requires_opt_in=True,
        ),
    )
    with pytest.raises(ExecutionPolicyError, match="opt-in"):
        CommandBus(project, registry=registry).dispatch(
            TypedCommand(command_id="c", operation="example.play")
        )


def test_direct_receipt_rehydration_with_valid_deadline(tmp_path: Path, project: Project) -> None:
    import sqlite3

    from nexus_ai_agent.creative.studio.bus import PlanTransactionState

    bus, store, plan, bindings, authority, actor = cold_fixture(tmp_path, project)
    expected = bus.durable_approval_requirements(
        plan, policy_version="v1", authenticated_actor=actor
    )[0]
    with sqlite3.connect(store.path) as conn:
        raw = conn.execute("SELECT evidence_json FROM plans").fetchone()[0]
    result = bus._rehydrate_durable_receipt(
        plan, expected, raw, deadline_monotonic_ns=store.monotonic_ns() + 10**10
    )
    assert result.state == PlanTransactionState.COMMITTED


@pytest.mark.parametrize("boundary", ["reserve", "heartbeat", "verify"])
def test_mutated_plan_is_refused_at_every_durable_boundary(
    tmp_path: Path, project: Project, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    bus, store, plan, _, _, actor = cold_fixture(tmp_path, project)
    plan.plan_id = "new-plan"
    requirements = bus.durable_approval_requirements(
        plan, policy_version="v1", authenticated_actor=actor
    )
    issuer = composition.ProvisionedPrincipalAuthority(
        {"c" * 32: PrincipalId("prn_" + "a" * 32)}, b"k" * 32
    )
    bindings, authority = issuer.verify_token(issuer.issue(requirements))
    if boundary == "verify":

        def mutate(*args: Any) -> bool:
            plan.steps = ()
            return True

        bus._durable_verifiers["media.play"] = mutate
    else:
        method = "reserve_and_start" if boundary == "reserve" else "heartbeat"
        original = getattr(store, method)

        def mutate_store(*args: Any, **kwargs: Any) -> Any:
            result = original(*args, **kwargs)
            plan.steps = ()
            return result

        monkeypatch.setattr(store, method, mutate_store)
    with pytest.raises(PlanTransactionError, match="changed"):
        bus.dispatch_plan(
            plan,
            approval_attestations=bindings,
            policy_version="v1",
            authenticated_actor=actor,
            approval_verifier=authority,
            deadline_monotonic_ns=store.monotonic_ns() + 10**10,
        )
    assert bus.project == project
