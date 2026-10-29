"""Executable integration contracts for Agent 2 and Agent 3 (task-186, §15).

Agent 2 = Assistant/driver consumer: discovers the capability surface, emits
typed schema-2 commands through the bus, consumes typed results/errors and
the undo path. Agent 3 = runtime/execution consumer: composes registries,
registers operations under the preview/master and pack gates, and reads the
result stamps.

Everything here runs through the *public* studio API only — these tests are
the contract both agents code against. The nine-category verification matrix
(happy path, invalid input, missing capability, unavailable pack, permission
failure, malformed command, execution failure, retry/replay, compatibility
regression) is exercised end to end.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict

from nexus_ai_agent.creative.studio import (
    ActorIdentity,
    AuthorizationError,
    CapabilityRegistry,
    CapabilitySurface,
    CommandBus,
    CommandExecutionError,
    CommandValidationError,
    ExecutionPolicyError,
    IdempotencyConflictError,
    OperationContext,
    OperationOutcome,
    OperationSpec,
    PermissionLevel,
    Project,
    ProjectAccess,
    Timeline,
    TypedCommand,
    UnknownOperationError,
    build_capability_surface,
    build_wave1_registry,
    error_code_of,
    is_master_evidence,
    new_project,
)
from nexus_ai_agent.creative.studio import lifecycle as studio_lifecycle
from nexus_ai_agent.creative.studio.lifecycle import LifecycleState, PackLifecycle

REPO_ROOT = Path(__file__).parents[2]
CONTRACT_DOC = REPO_ROOT / "docs" / "architecture" / "COMMAND_CAPABILITY_CONTRACT.md"


class _NoopInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _noop_handler(project: Project, context: OperationContext) -> OperationOutcome:
    return OperationOutcome(project, context.history, {"ran": True})


def _exploding_handler(project: Project, context: OperationContext) -> OperationOutcome:
    raise RuntimeError("execution failure matrix")


class _StaticAuthorizer:
    def __init__(self, access: ProjectAccess) -> None:
        self._access = access

    def authorize(self, actor: ActorIdentity, project_id: str) -> ProjectAccess:
        if self._access.actor != actor or self._access.project_id != project_id:
            raise AuthorizationError("no grant")
        return self._access


def _project() -> Project:
    return new_project(
        "project_01", "Integration", Timeline(timeline_id="tl", duration_us=10_000_000)
    )


def _claimed(operation: str, *, command_id: str, input_: dict | None = None, **extra) -> str:
    payload = {
        "command_id": command_id,
        "operation": operation,
        "schema_version": 2,
        "actor": {"kind": "agent", "actor_id": "agent-02"},
        "target": {"project_id": "project_01"},
        "provenance": {"source": "agent", "source_id": "agent-02-integration"},
        "input": input_ or {},
        **extra,
    }
    return json.dumps(payload)


@pytest.fixture()
def authorizer() -> _StaticAuthorizer:
    return _StaticAuthorizer(
        ProjectAccess(
            actor=ActorIdentity(kind="agent", actor_id="agent-02"),
            project_id="project_01",
            permissions=frozenset({"project:read", "project:write"}),
        )
    )


class TestAgent2IntentToUndo:
    """The full driver chain: surface → typed command → result → undo."""

    def test_discover_plan_dispatch_undo(self, authorizer: _StaticAuthorizer) -> None:
        registry = build_wave1_registry()

        # 1. Discovery: the Assistant sees only runtime-truth capabilities.
        surface = build_capability_surface(registry)
        advertised = {op.operation_id for op in surface.operations}
        assert "timeline.mark" in advertised

        # 2. Planning happens strictly from the surface view.
        mark_view = next(op for op in surface.operations if op.operation_id == "timeline.mark")
        assert mark_view.input_schema.get("type") == "object"
        snapshot = {
            "capability_id": mark_view.capability_id,
            "operation": mark_view.operation_id,
            "version": mark_view.capability_version,
            "operation_schema_version": mark_view.operation_schema_version,
            "pack_provider": mark_view.pack_provider,
        }

        bus = CommandBus(state=_project(), registry=registry, authorizer=authorizer)
        base_hash = bus.state_hash

        # 3. Typed schema-2 command, advisory snapshot built from discovery.
        mark_command = _claimed(
            "timeline.mark",
            command_id="cmd_mark_01",
            input_={"at": {"kind": "absolute", "timecode_us": 2_500_000}, "label": "beat"},
            capability_snapshot=snapshot,
        )
        result = bus.dispatch(mark_command)
        assert result.status == "applied"
        assert is_master_evidence(result) is True
        assert bus.state_hash != base_hash

        # 4. Undo through the same public path restores the exact state hash.
        undo_result = bus.dispatch(_claimed("system.undo", command_id="cmd_undo_01"))
        assert undo_result.state_hash == base_hash
        assert undo_result.output["undone_operation"] == "timeline.mark"

    def test_capability_snapshot_from_surface_is_accepted_but_advisory(
        self, authorizer: _StaticAuthorizer
    ) -> None:
        registry = build_wave1_registry()
        bus = CommandBus(state=_project(), registry=registry, authorizer=authorizer)
        surface = build_capability_surface(registry)
        play_view = next(op for op in surface.operations if op.operation_id == "media.play")
        good_snapshot = {
            "capability_id": play_view.capability_id,
            "operation": "media.play",
            "version": play_view.capability_version,
            "operation_schema_version": play_view.operation_schema_version,
            "pack_provider": play_view.pack_provider,
        }
        result = bus.dispatch(
            _claimed("media.play", command_id="cmd_play", capability_snapshot=good_snapshot)
        )
        assert result.status == "applied"
        # A forged provider is still refused even when shaped like a surface row.
        forged = dict(good_snapshot, pack_provider="evil.provider")
        with pytest.raises(Exception) as excinfo:
            bus.dispatch(
                _claimed("media.play", command_id="cmd_play_2", capability_snapshot=forged)
            )
        assert error_code_of(excinfo.value) == "capability_unavailable"


class TestNineCategoryMatrix:
    def test_01_happy_path(self, authorizer: _StaticAuthorizer) -> None:
        bus = CommandBus(state=_project(), authorizer=authorizer)
        result = bus.dispatch(_claimed("media.play", command_id="cmd_ok"))
        assert result.status == "applied"

    def test_02_invalid_input(self, authorizer: _StaticAuthorizer) -> None:
        bus = CommandBus(state=_project(), authorizer=authorizer)
        with pytest.raises(CommandValidationError) as excinfo:
            bus.dispatch(_claimed("timeline.mark", command_id="cmd_bad", input_={"label": "x"}))
        assert error_code_of(excinfo.value) == "command_validation"

    def test_03_missing_capability(self, authorizer: _StaticAuthorizer) -> None:
        bus = CommandBus(state=_project(), authorizer=authorizer)
        with pytest.raises(UnknownOperationError) as excinfo:
            bus.dispatch(_claimed("timeline.enhance", command_id="cmd_missing"))
        assert error_code_of(excinfo.value) == "unknown_operation"
        # The missing operation is also absent from the discovery surface.
        surface = build_capability_surface(build_wave1_registry())
        assert "timeline.enhance" not in {op.operation_id for op in surface.operations}

    def test_04_unavailable_pack(
        self, authorizer: _StaticAuthorizer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(
            studio_lifecycle.PACK_LIFECYCLE,
            "test.pack.stub",
            PackLifecycle(
                pack_id="test.pack.stub", state=LifecycleState.STUB, reason="matrix record"
            ),
        )
        registry = CapabilityRegistry()
        registry.register_domain("test")
        registry.register_operation(
            "test",
            "stubbed",
            OperationSpec(
                operation_id="test.stubbed",
                description="requires a STUB pack",
                permission_level=PermissionLevel.REVERSIBLE,
                input_model=_NoopInput,
                handler=_noop_handler,
                required_packs=("test.pack.stub",),
            ),
        )
        # Discovery hides it (Law 4) ...
        surface = build_capability_surface(registry)
        assert surface.operations == ()
        assert surface.excluded[0].reason.value == "pack_stub"
        # ... and the bus pack gate refuses it fail-closed (defense in depth).
        from nexus_ai_agent.creative.studio.lifecycle import PackRequirementError

        bus = CommandBus(state=_project(), registry=registry, authorizer=authorizer)
        with pytest.raises(PackRequirementError) as excinfo:
            bus.dispatch(_claimed("test.stubbed", command_id="cmd_stub"))
        assert error_code_of(excinfo.value) == "pack_requirement"

    def test_05_permission_failure(self) -> None:
        read_only = _StaticAuthorizer(
            ProjectAccess(
                actor=ActorIdentity(kind="agent", actor_id="agent-02"),
                project_id="project_01",
                permissions=frozenset({"project:read"}),
            )
        )
        bus = CommandBus(state=_project(), authorizer=read_only)
        with pytest.raises(AuthorizationError) as excinfo:
            bus.dispatch(
                _claimed(
                    "timeline.mark",
                    command_id="cmd_perm",
                    input_={"at": {"kind": "absolute", "timecode_us": 2_500_000}, "label": "beat"},
                )
            )
        assert error_code_of(excinfo.value) == "authorization"

    def test_06_malformed_command(self, authorizer: _StaticAuthorizer) -> None:
        bus = CommandBus(state=_project(), authorizer=authorizer)
        duplicate_keys = '{"command_id": "c", "command_id": "c2", "operation": "media.play"}'
        with pytest.raises(CommandValidationError) as excinfo:
            bus.dispatch(duplicate_keys)
        assert error_code_of(excinfo.value) == "command_validation"

    def test_07_execution_failure(self, authorizer: _StaticAuthorizer) -> None:
        registry = build_wave1_registry()
        registry.register_operation(
            "system",
            "fragile",
            OperationSpec(
                operation_id="system.fragile",
                description="always raises",
                permission_level=PermissionLevel.REVERSIBLE,
                input_model=_NoopInput,
                handler=_exploding_handler,
            ),
        )
        bus = CommandBus(state=_project(), registry=registry, authorizer=authorizer)
        with pytest.raises(CommandExecutionError) as excinfo:
            bus.dispatch(_claimed("system.fragile", command_id="cmd_boom"))
        assert error_code_of(excinfo.value) == "execution_failed"
        assert bus.state_revision == 0

    def test_08_replay_and_conflict(self, authorizer: _StaticAuthorizer) -> None:
        bus = CommandBus(state=_project(), authorizer=authorizer)
        first = bus.dispatch(_claimed("media.play", command_id="cmd_k1", idempotency_key="key-1"))
        replay = bus.dispatch(
            _claimed("media.play", command_id="cmd_k1_retry", idempotency_key="key-1")
        )
        assert replay.transaction_id == first.transaction_id
        assert bus.state_revision == 1  # no second application
        # Same (project, operation, key) with a different payload is a
        # deterministic conflict; the scope is per-operation, so the conflict
        # must reuse media.play with a changed input.
        with pytest.raises(IdempotencyConflictError) as excinfo:
            bus.dispatch(
                _claimed(
                    "media.play",
                    command_id="cmd_k2",
                    idempotency_key="key-1",
                    input_={"start": "here"},
                )
            )
        assert error_code_of(excinfo.value) == "idempotency_conflict"

    def test_09_compatibility_regression(self, authorizer: _StaticAuthorizer) -> None:
        bus = CommandBus(state=_project(), authorizer=authorizer)
        bad_schema = _claimed("media.play", command_id="cmd_s3")
        forged = json.loads(bad_schema)
        forged["schema_version"] = 3
        with pytest.raises(CommandValidationError):
            bus.dispatch(json.dumps(forged))
        forged_v2_protocol = json.loads(bad_schema)
        forged_v2_protocol["protocol_version"] = "nagar.command.v2"
        with pytest.raises(CommandValidationError) as excinfo:
            bus.dispatch(json.dumps(forged_v2_protocol))
        assert error_code_of(excinfo.value) == "command_validation"


class TestAgent3RuntimeContract:
    def test_shipped_runtime_truth_law4_on_real_packs(self) -> None:
        """The real six-pack composition projected through Law 4."""
        from nexus_ai_agent.creative.packs.runtime import build_runtime_registry

        registry = build_runtime_registry()
        default_surface = build_capability_surface(registry)
        opted_in = build_capability_surface(registry, include_experimental=True)

        default_ids = {op.operation_id for op in default_surface.operations}
        opted_ids = {op.operation_id for op in opted_in.operations}

        # An AVAILABLE-lifecycle pack operation is advertised by default ...
        assert "slideshow.scan_assets" in default_ids
        # ... while EXPERIMENTAL-pack operations are hidden until opt-in.
        assert "color.adjust_exposure" not in default_ids
        assert "color.adjust_exposure" in opted_ids
        assert default_ids < opted_ids

        # Every advertised operation's packs resolve executable under the flag.
        for op in default_surface.operations:
            for state in op.pack_lifecycle_states:
                assert state.state is LifecycleState.AVAILABLE

    def test_operation_handler_registration_stays_typed(self) -> None:
        # Agent 3's registration contract: the fail-closed preview gate and the
        # forbid-extra schema gate apply to every new operation alike.
        registry = CapabilityRegistry()
        registry.register_domain("agent3")

        class _Loose(BaseModel):
            pass

        with pytest.raises(ValueError, match="forbid extra fields"):
            registry.register_operation(
                "agent3",
                "loose",
                OperationSpec(
                    operation_id="agent3.loose",
                    description="schema does not forbid extras",
                    permission_level=PermissionLevel.IMMEDIATE,
                    input_model=_Loose,
                    handler=_noop_handler,
                ),
            )

    def test_preview_stamp_flows_to_agent3_consumers(self, authorizer: _StaticAuthorizer) -> None:
        registry = CapabilityRegistry()
        registry.register_domain("agent3")
        registry.register_operation(
            "agent3",
            "realize",
            OperationSpec(
                operation_id="agent3.realize",
                description="realization with preview twin",
                permission_level=PermissionLevel.REVERSIBLE,
                input_model=_NoopInput,
                handler=_noop_handler,
                execution_modes=("local", "preview"),
                preview_semantics="non_authoritative_realization",
            ),
        )
        bus = CommandBus(state=_project(), registry=registry, authorizer=authorizer)
        preview = bus.dispatch(
            _claimed("agent3.realize", command_id="cmd_p", execution_policy={"mode": "preview"})
        )
        master = bus.dispatch(_claimed("agent3.realize", command_id="cmd_m"))
        assert (
            preview.diagnostics["execution_mode"],
            preview.diagnostics["authoritative"],
        ) == ("preview", False)
        assert (master.diagnostics["execution_mode"], master.diagnostics["authoritative"]) == (
            "local",
            True,
        )
        assert is_master_evidence(preview) is False
        assert is_master_evidence(master) is True


class TestContractDocumentPublished:
    """The human-readable half of the integration contract (§15)."""

    def test_canonical_contract_doc_publishes_the_agent_surfaces(self) -> None:
        text = CONTRACT_DOC.read_text(encoding="utf-8")
        for marker in (
            "Capability discovery contract",
            "nagar.discovery.v1",
            "Preview/master execution boundary",
            "state_equivalent",
            "non_authoritative_realization",
            "Integration contracts",
            "Agent 2",
            "Agent 3",
            "Core API",
        ):
            assert marker in text, f"contract doc must publish: {marker!r}"

    def test_surface_protocol_literal_matches_the_doc(self) -> None:
        text = CONTRACT_DOC.read_text(encoding="utf-8")
        surface = build_capability_surface(build_wave1_registry())
        assert surface.protocol_version in text
        assert json.dumps(surface.model_dump(mode="json"))  # serializable

    def test_unadvertised_preview_denial_error_is_contracted(self) -> None:
        text = CONTRACT_DOC.read_text(encoding="utf-8")
        assert "execution_policy" in text
        assert "pack_requirement" in text
        assert "unknown_operation" in text


def test_execution_policy_error_code_mapping_is_exercised() -> None:
    bus = CommandBus(state=_project())
    with pytest.raises(ExecutionPolicyError) as excinfo:
        bus.dispatch(
            TypedCommand.model_validate(
                {
                    "command_id": "cmd_mode",
                    "operation": "timeline.mark",
                    "input": {
                        "at": {"kind": "absolute", "timecode_us": 2_500_000},
                        "label": "beat",
                    },
                    "execution_policy": {"mode": "preview"},
                }
            )
        )
    assert error_code_of(excinfo.value) == "execution_policy"


def test_surface_is_a_subset_of_the_registry_allowlist() -> None:
    registry = build_wave1_registry()
    surface: CapabilitySurface = build_capability_surface(registry)
    for op in surface.operations:
        assert op.operation_id in registry
        spec = registry.get_spec(op.operation_id)
        assert op.operation_schema_version == spec.schema_version
        assert op.execution_modes == spec.execution_modes
