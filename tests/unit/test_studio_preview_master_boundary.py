"""Preview/master execution boundary (task-186, contract doc §13).

Registration-time proofs: advertising ``preview`` without a declared
semantics is refused, and an orphaned declaration is refused (both
fail-closed). Dispatch proofs: the bus stamps every ``CommandResult`` with
``execution_mode`` and ``authoritative``; a ``state_equivalent`` preview is
authoritative as *state truth*, a ``non_authoritative_realization`` preview
never is, and there is no promotion path. The pinned Gate-2 baseline (an
unadvertised preview mode is denied) stays green unmodified.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ConfigDict

from nexus_ai_agent.creative.studio import (
    CapabilityError,
    CapabilityRegistry,
    CommandBus,
    CommandExecutionError,
    ExecutionPolicyError,
    OperationContext,
    OperationOutcome,
    OperationSpec,
    PermissionLevel,
    Project,
    Timeline,
    TypedCommand,
    UnknownOperationError,
    build_wave1_registry,
    is_master_evidence,
    new_project,
)


class _NoopInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _noop_handler(project: Project, context: OperationContext) -> OperationOutcome:
    return OperationOutcome(project, context.history, {"ran": True})


def _failing_handler(project: Project, context: OperationContext) -> OperationOutcome:
    raise RuntimeError("realization exploded")


@pytest.fixture()
def project() -> Project:
    return new_project("project_01", "Preview Demo", Timeline(timeline_id="tl", duration_us=1))


def _command(operation: str, mode: str, command_id: str = "cmd_preview") -> TypedCommand:
    return TypedCommand.model_validate(
        {
            "command_id": command_id,
            "operation": operation,
            "input": {},
            "execution_policy": {"mode": mode},
        }
    )


def _registry_with_preview_ops() -> CapabilityRegistry:
    registry = CapabilityRegistry()
    registry.register_domain("test", "preview boundary test domain")
    registry.register_operation(
        "test",
        "state_op",
        OperationSpec(
            operation_id="test.state_edit",
            description="pure state edit, preview == master",
            permission_level=PermissionLevel.REVERSIBLE,
            input_model=_NoopInput,
            handler=_noop_handler,
            execution_modes=("local", "preview"),
            preview_semantics="state_equivalent",
        ),
    )
    registry.register_operation(
        "test",
        "render_op",
        OperationSpec(
            operation_id="test.render_preview",
            description="realization op; preview artifacts are not masters",
            permission_level=PermissionLevel.REVERSIBLE,
            input_model=_NoopInput,
            handler=_noop_handler,
            execution_modes=("local", "preview"),
            preview_semantics="non_authoritative_realization",
        ),
    )
    return registry


class TestRegistrationFailClosed:
    def test_preview_advertisement_without_semantics_is_refused(self) -> None:
        registry = CapabilityRegistry()
        registry.register_domain("test")
        with pytest.raises(ValueError, match="requires explicit preview_semantics"):
            registry.register_operation(
                "test",
                "bad",
                OperationSpec(
                    operation_id="test.undeclared",
                    description="preview advertised without semantics",
                    permission_level=PermissionLevel.IMMEDIATE,
                    input_model=_NoopInput,
                    handler=_noop_handler,
                    execution_modes=("local", "preview"),
                ),
            )

    def test_orphaned_semantics_without_preview_advertisement_is_refused(self) -> None:
        registry = CapabilityRegistry()
        registry.register_domain("test")
        with pytest.raises(ValueError, match="not advertised"):
            registry.register_operation(
                "test",
                "bad",
                OperationSpec(
                    operation_id="test.orphan",
                    description="semantics without the advertisement",
                    permission_level=PermissionLevel.IMMEDIATE,
                    input_model=_NoopInput,
                    handler=_noop_handler,
                    execution_modes=("local",),
                    preview_semantics="state_equivalent",
                ),
            )

    def test_all_shipped_catalogs_register_unchanged(self) -> None:
        # Wave-1 catalog: five operations, all local-only (no preview yet).
        registry = build_wave1_registry()
        assert set(registry.list_operations()) == {
            "media.play",
            "media.pause",
            "timeline.mark",
            "timeline.split_at_playhead",
            "system.undo",
        }
        for operation_id in registry.list_operations():
            spec = registry.get_spec(operation_id)
            assert spec.execution_modes == ("local",)
            assert spec.preview_semantics is None


class TestDispatchStamps:
    def test_local_execution_is_always_master_authoritative(self, project: Project) -> None:
        bus = CommandBus(state=project, registry=_registry_with_preview_ops())
        result = bus.dispatch(_command("test.state_edit", "local"))
        assert result.diagnostics["execution_mode"] == "local"
        assert result.diagnostics["authoritative"] is True
        assert is_master_evidence(result) is True

    def test_state_equivalent_preview_is_authoritative_state_truth(self, project: Project) -> None:
        bus = CommandBus(state=project, registry=_registry_with_preview_ops())
        result = bus.dispatch(_command("test.state_edit", "preview"))
        assert result.diagnostics["execution_mode"] == "preview"
        assert result.diagnostics["authoritative"] is True
        assert is_master_evidence(result) is True

    def test_non_authoritative_preview_is_never_master_evidence(self, project: Project) -> None:
        bus = CommandBus(state=project, registry=_registry_with_preview_ops())
        preview = bus.dispatch(_command("test.render_preview", "preview"))
        assert preview.diagnostics["execution_mode"] == "preview"
        assert preview.diagnostics["authoritative"] is False
        assert is_master_evidence(preview) is False
        # The same operation under local execution IS master evidence: the
        # boundary follows the executed mode, not the operation identity.
        master = bus.dispatch(_command("test.render_preview", "local", "cmd_master"))
        assert master.diagnostics["execution_mode"] == "local"
        assert is_master_evidence(master) is True

    def test_failed_execution_never_produces_a_forged_stamp(self, project: Project) -> None:
        registry = _registry_with_preview_ops()
        registry.register_operation(
            "test",
            "boom",
            OperationSpec(
                operation_id="test.boom",
                description="always fails",
                permission_level=PermissionLevel.REVERSIBLE,
                input_model=_NoopInput,
                handler=_failing_handler,
                execution_modes=("local", "preview"),
                preview_semantics="non_authoritative_realization",
            ),
        )
        bus = CommandBus(state=project, registry=registry)
        with pytest.raises(CommandExecutionError):
            bus.dispatch(_command("test.boom", "preview"))
        assert bus.state_revision == 0
        assert bus.history == ()


class TestPinnedBaselineUnchanged:
    def test_unadvertised_preview_mode_is_still_denied(self, project: Project) -> None:
        # Gate-2 pin, restated as a regression guard: Wave-1 operations do not
        # advertise preview, so a preview request is refused at stage 5.
        bus = CommandBus(state=project)
        with pytest.raises(ExecutionPolicyError, match="unavailable execution mode"):
            bus.dispatch(_command("media.play", "preview"))
        assert bus.state_revision == 0

    def test_unknown_operation_fails_before_mode_policy(self, project: Project) -> None:
        bus = CommandBus(state=project, registry=_registry_with_preview_ops())
        with pytest.raises(UnknownOperationError):
            bus.dispatch(_command("test.does_not_exist", "preview"))

    def test_capability_gate_fires_before_mode_policy(self, project: Project) -> None:
        registry = _registry_with_preview_ops()
        unavailable = registry.get_capability("test.state_op")
        unavailable.available = False
        bus = CommandBus(state=project, registry=registry)
        with pytest.raises(CapabilityError):
            bus.dispatch(_command("test.state_edit", "preview"))
        assert bus.state_revision == 0


class TestAuthoritativeForHelper:
    def test_unadvertised_mode_fails_closed(self) -> None:
        spec = OperationSpec(
            operation_id="test.local_only",
            description="local only",
            permission_level=PermissionLevel.IMMEDIATE,
            input_model=_NoopInput,
            handler=_noop_handler,
        )
        assert spec.authoritative_for("local") is True
        assert spec.authoritative_for("preview") is False

    def test_non_authoritative_preview_fails_closed(self) -> None:
        spec = OperationSpec(
            operation_id="test.render",
            description="realization",
            permission_level=PermissionLevel.IMMEDIATE,
            input_model=_NoopInput,
            handler=_noop_handler,
            execution_modes=("local", "preview"),
            preview_semantics="non_authoritative_realization",
        )
        assert spec.authoritative_for("local") is True
        assert spec.authoritative_for("preview") is False
