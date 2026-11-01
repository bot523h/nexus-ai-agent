"""Capability discovery contract (task-186, contract doc §14).

Law 4 proofs: the Assistant-facing surface is a fail-closed projection of
runtime truth — unavailable capabilities and unknown/STUB/RETIRED packs are
hidden with typed reason codes, EXPERIMENTAL packs need the composition-root
opt-in, the projection is deterministic and JSON-stable, and exclusion
records never carry permission data. Plus the typed error contract: every
dispatch failure maps to a stable string code (most-derived class first).
"""

from __future__ import annotations

import json

import pytest
from pydantic import BaseModel, ConfigDict

from nexus_ai_agent.creative.studio import (
    CapabilityRegistry,
    CapabilitySurface,
    CommandBus,
    CommandResult,
    ExclusionReason,
    OperationContext,
    OperationOutcome,
    OperationSpec,
    PermissionLevel,
    Project,
    build_capability_surface,
    build_wave1_registry,
    error_code_of,
    is_master_evidence,
    new_project,
    surface_canonical_json,
    surface_identity,
)
from nexus_ai_agent.creative.studio import lifecycle as studio_lifecycle
from nexus_ai_agent.creative.studio.discovery import ERROR_CONTRACT
from nexus_ai_agent.creative.studio.lifecycle import LifecycleState, PackLifecycle
from nexus_ai_agent.creative.studio.models import (
    AuthorizationError,
    CapabilityError,
    CapabilityVersionError,
    CommandExecutionError,
    CommandValidationError,
    ExecutionPolicyError,
    IdempotencyConflictError,
    InputReferenceError,
    NagarError,
    PermissionDeniedError,
    Playhead,
    PreconditionError,
    ReferenceResolutionError,
    Timeline,
    TypedCommand,
    UndoStackEmptyError,
    UnknownCapabilityError,
    UnknownOperationError,
)


class _NoopInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _noop_handler(project: Project, context: OperationContext) -> OperationOutcome:
    return OperationOutcome(project, context.history, {"ran": True})


def _spec(
    operation_id: str,
    *,
    required_packs: tuple[str, ...] = (),
    permission_level: PermissionLevel = PermissionLevel.IMMEDIATE,
) -> OperationSpec:
    return OperationSpec(
        operation_id=operation_id,
        description=f"test operation {operation_id}",
        permission_level=permission_level,
        input_model=_NoopInput,
        handler=_noop_handler,
        required_packs=required_packs,
    )


def _registry_with_pack_ops() -> CapabilityRegistry:
    registry = CapabilityRegistry()
    registry.register_domain("test", "discovery test domain")
    registry.register_operation("test", "plain", _spec("test.no_pack"))
    registry.register_operation(
        "test", "packed", _spec("test.on_available", required_packs=("test.pack.available",))
    )
    registry.register_operation(
        "test", "exp", _spec("test.on_experimental", required_packs=("test.pack.experimental",))
    )
    registry.register_operation(
        "test", "stub", _spec("test.on_stub", required_packs=("test.pack.stub",))
    )
    registry.register_operation(
        "test", "retired", _spec("test.on_retired", required_packs=("test.pack.retired",))
    )
    registry.register_operation(
        "test", "ghost", _spec("test.on_unknown", required_packs=("test.pack.never_existed",))
    )
    return registry


@pytest.fixture()
def pack_table(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install deterministic lifecycle records for the discovery test packs."""
    for pack_id, state in (
        ("test.pack.available", LifecycleState.AVAILABLE),
        ("test.pack.experimental", LifecycleState.EXPERIMENTAL),
        ("test.pack.stub", LifecycleState.STUB),
        ("test.pack.retired", LifecycleState.RETIRED),
    ):
        monkeypatch.setitem(
            studio_lifecycle.PACK_LIFECYCLE,
            pack_id,
            PackLifecycle(pack_id=pack_id, state=state, reason="discovery test record"),
        )


def _ids(surface: CapabilitySurface) -> set[str]:
    return {op.operation_id for op in surface.operations}


def _excluded(surface: CapabilitySurface) -> dict[str, ExclusionReason]:
    return {entry.operation_id: entry.reason for entry in surface.excluded}


class TestWave1Surface:
    def test_wave1_registry_advertises_all_five_operations(self) -> None:
        surface = build_capability_surface(build_wave1_registry())
        assert _ids(surface) == {
            "media.play",
            "media.pause",
            "timeline.mark",
            "timeline.split_at_playhead",
            "system.undo",
        }
        assert surface.excluded == ()
        assert surface.protocol_version == "nagar.discovery.v1"
        assert surface.operation_count == 5

    def test_surface_is_deterministic_and_json_stable(self) -> None:
        registry = build_wave1_registry()
        first = build_capability_surface(registry)
        second = build_capability_surface(registry)
        assert surface_canonical_json(first) == surface_canonical_json(second)
        assert surface_identity(first) == surface_identity(second)
        # The Assistant receives plain JSON: round-trip must be lossless.
        round_tripped = json.loads(surface_canonical_json(first))
        assert CapabilitySurface.model_validate(round_tripped) == first

    def test_operation_view_carries_everything_an_assistant_needs(self) -> None:
        surface = build_capability_surface(build_wave1_registry())
        mark = next(op for op in surface.operations if op.operation_id == "timeline.mark")
        assert mark.capability_id == "timeline.marking"
        assert mark.permission_level is PermissionLevel.REVERSIBLE
        assert "project:write" in mark.required_permissions
        assert mark.execution_modes == ("local",)
        assert mark.operation_schema_version == 1
        assert mark.input_schema.get("type") == "object"
        assert mark.required_packs == ()
        assert mark.pack_provider == "nagar.core"
        assert mark.deterministic is True

    def test_surface_key_contract_is_stable(self) -> None:
        surface = build_capability_surface(build_wave1_registry())
        assert set(surface.model_dump(mode="json")) == {
            "protocol_version",
            "include_experimental",
            "operations",
            "excluded",
        }
        operation_keys = set(surface.operations[0].model_dump(mode="json"))
        assert operation_keys == {
            "operation_id",
            "capability_id",
            "capability_version",
            "description",
            "permission_level",
            "required_permissions",
            "execution_modes",
            "preview_semantics",
            "operation_schema_version",
            "input_schema",
            "required_packs",
            "pack_lifecycle_states",
            "pack_provider",
            "deterministic",
        }


class TestLaw4Projection:
    def test_pack_lifecycle_truth_filters_the_surface(self, pack_table: None) -> None:
        surface = build_capability_surface(_registry_with_pack_ops())
        assert _ids(surface) == {"test.no_pack", "test.on_available"}
        reasons = _excluded(surface)
        assert reasons == {
            "test.on_experimental": ExclusionReason.PACK_EXPERIMENTAL_NOT_OPTED_IN,
            "test.on_stub": ExclusionReason.PACK_STUB,
            "test.on_retired": ExclusionReason.PACK_RETIRED,
            "test.on_unknown": ExclusionReason.PACK_UNKNOWN,
        }
        # The advertised pack operation reports the resolved lifecycle state.
        advertised = next(op for op in surface.operations if op.operation_id == "test.on_available")
        assert [(p.pack_id, p.state) for p in advertised.pack_lifecycle_states] == [
            ("test.pack.available", LifecycleState.AVAILABLE)
        ]

    def test_experimental_opt_in_mirrors_the_bus_composition_flag(self, pack_table: None) -> None:
        opted_in = build_capability_surface(_registry_with_pack_ops(), include_experimental=True)
        assert "test.on_experimental" in _ids(opted_in)
        assert opted_in.include_experimental is True
        experimental = next(
            op for op in opted_in.operations if op.operation_id == "test.on_experimental"
        )
        assert [(p.pack_id, p.state) for p in experimental.pack_lifecycle_states] == [
            ("test.pack.experimental", LifecycleState.EXPERIMENTAL)
        ]
        # STUB/RETIRED/unknown stay hidden even under the opt-in: maturity is
        # not the same axis as experimental permission.
        reasons = _excluded(opted_in)
        assert reasons["test.on_stub"] is ExclusionReason.PACK_STUB
        assert reasons["test.on_retired"] is ExclusionReason.PACK_RETIRED
        assert reasons["test.on_unknown"] is ExclusionReason.PACK_UNKNOWN

    def test_unavailable_capability_is_hidden(self) -> None:
        registry = CapabilityRegistry()
        registry.register_domain("test")
        registry.register_operation("test", "down", _spec("test.disabled"), available=False)
        registry.register_operation("test", "up", _spec("test.enabled"))
        surface = build_capability_surface(registry)
        assert _ids(surface) == {"test.enabled"}
        assert _excluded(surface) == {"test.disabled": ExclusionReason.CAPABILITY_UNAVAILABLE}

    def test_projection_is_independent_of_registration_order(self) -> None:
        def build(reverse: bool) -> CapabilitySurface:
            registry = CapabilityRegistry()
            registry.register_domain("test")
            ops = [_spec(f"test.op_{index}") for index in range(6)]
            for spec in reversed(ops) if reverse else ops:
                registry.register_operation("test", "order", spec)
            return build_capability_surface(registry)

        assert surface_canonical_json(build(False)) == surface_canonical_json(build(True))

    def test_exclusion_records_carry_no_permission_data(self) -> None:
        fields = set(_exclusion_fields())
        assert fields == {"operation_id", "reason", "detail"}


def _exclusion_fields() -> list[str]:
    from nexus_ai_agent.creative.studio import ExcludedOperation

    return list(ExcludedOperation.model_fields)


class TestErrorContract:
    def test_every_registered_error_class_has_a_unique_stable_code(self) -> None:
        codes = [code for _, code in ERROR_CONTRACT]
        assert len(codes) == len(set(codes)), "contract codes must be unique"

    def test_most_derived_class_wins_resolution(self) -> None:
        assert error_code_of(UnknownOperationError("x")) == "unknown_operation"
        assert error_code_of(IdempotencyConflictError("x")) == "idempotency_conflict"
        assert error_code_of(CommandValidationError("x")) == "command_validation"
        assert error_code_of(AuthorizationError("x")) == "authorization"
        assert error_code_of(ExecutionPolicyError("x")) == "execution_policy"
        assert error_code_of(PermissionDeniedError("x")) == "permission_denied"
        assert error_code_of(UnknownCapabilityError("x")) == "unknown_capability"
        assert error_code_of(CapabilityVersionError("x")) == "capability_version"
        assert error_code_of(CapabilityError("x")) == "capability_unavailable"
        assert error_code_of(InputReferenceError("x")) == "input_reference"
        assert error_code_of(ReferenceResolutionError("x")) == "reference_resolution"
        assert error_code_of(PreconditionError("x")) == "precondition"
        assert error_code_of(UndoStackEmptyError("x")) == "undo_stack_empty"
        assert error_code_of(CommandExecutionError("x")) == "execution_failed"
        assert error_code_of(NagarError("x")) == "nagar_error"

    def test_pack_requirement_error_maps_to_its_own_code(self) -> None:
        from nexus_ai_agent.creative.studio.lifecycle import PackRequirementError

        assert error_code_of(PackRequirementError("x")) == "pack_requirement"

    def test_codes_match_what_the_bus_actually_raises(self) -> None:
        timeline = Timeline(timeline_id="tl", duration_us=1_000_000)
        bus = CommandBus(state=new_project("project_01", "P", timeline))
        with pytest.raises(UnknownOperationError) as excinfo:
            bus.dispatch(
                TypedCommand.model_validate({"command_id": "c1", "operation": "nope.nope"})
            )
        assert error_code_of(excinfo.value) == "unknown_operation"


class TestMasterEvidenceGate:
    def test_unstamped_result_is_never_master_evidence(self) -> None:
        bare = CommandResult(transaction_id="tx", state_revision=1, state_hash="sha256:x")
        assert is_master_evidence(bare) is False

    def test_authoritative_false_is_never_master_evidence(self) -> None:
        stamped = CommandResult(
            transaction_id="tx",
            state_revision=1,
            state_hash="sha256:x",
            diagnostics={"execution_mode": "preview", "authoritative": False},
        )
        assert is_master_evidence(stamped) is False

    def test_authoritative_true_is_master_evidence(self) -> None:
        stamped = CommandResult(
            transaction_id="tx",
            state_revision=1,
            state_hash="sha256:x",
            diagnostics={"execution_mode": "local", "authoritative": True},
        )
        assert is_master_evidence(stamped) is True


def test_playhead_import_keeps_models_pinned() -> None:
    # Guard: the discovery layer builds on the canonical models, never on
    # private copies. If this import ever fails, a fork happened.
    assert Playhead(timecode_us=0).captured_at_command is True
