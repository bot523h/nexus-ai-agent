"""Adversarial contract probe for PR#87 (Studio Core / Agent 01) — task-191.

NOT part of the main test suite: it imports ``creative.studio.discovery``,
which exists only on the PR#87 branch.  Run it against a PR#87 checkout::

    git worktree add /tmp/wt87 <pr87-sha>
    cd /tmp/wt87 && PYTHONPATH=/tmp/wt87/src \\
        pytest -q -p no:cacheprovider /path/to/scripts/pr87_contract_probe.py

Every test states ONE invariant required by the master-session brief (Rule 6)
and asserts it.  A FAILED test is a contract violation of PR#87 as it stands,
not a defect of this probe; the expected outcome at PR#87 ``2ed4495`` is
recorded in ``NAGAR_MASTER_SESSION_2026-09-25.md`` §6.

Invariant groups:

* D — discovery: stale identity, excluded operation, registry change;
* P — preview/master: automatic promotion, forged stamp, missing stamp,
  non-authoritative publication, verifier absence;
* A2 — Agent 2 chain: discover → plan → snapshot → command → dispatch →
  result → undo;
* A3 — Agent 3 chain: register → compose → execute → result stamp.
"""

from __future__ import annotations

import pytest
from nexus_ai_agent.creative.studio.discovery import (
    ExclusionReason,
    build_capability_surface,
    is_master_evidence,
    surface_identity,
)
from pydantic import BaseModel, ConfigDict, ValidationError

from nexus_ai_agent.creative.studio import (
    CapabilityRegistry,
    CommandBus,
    OperationContext,
    OperationOutcome,
    OperationSpec,
    PermissionLevel,
    Project,
    Timeline,
    TypedCommand,
    build_wave1_registry,
    new_project,
)
from nexus_ai_agent.creative.studio import lifecycle as studio_lifecycle
from nexus_ai_agent.creative.studio.lifecycle import LifecycleState, PackLifecycle
from nexus_ai_agent.creative.studio.models import CommandResult, NagarError


class _NoInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _noop(project: Project, context: OperationContext) -> OperationOutcome:
    return OperationOutcome(project, context.history, {"ran": True})


def _project() -> Project:
    return new_project("project_01", "Probe", Timeline(timeline_id="tl", duration_us=10_000_000))


def _registry(*, stub_pack: bool = False, version: str = "1.0.0") -> CapabilityRegistry:
    registry = CapabilityRegistry()
    registry.register_domain("probe", "adversarial probe domain")
    registry.register_operation(
        "probe",
        "edit",
        OperationSpec(
            operation_id="probe.edit",
            description="pure state edit",
            permission_level=PermissionLevel.IMMEDIATE,
            input_model=_NoInput,
            handler=_noop,
            execution_modes=("local", "preview"),
            preview_semantics="state_equivalent",
        ),
        capability_version=version,
    )
    registry.register_operation(
        "probe",
        "realize",
        OperationSpec(
            operation_id="probe.realize",
            description="realization; preview is not master",
            permission_level=PermissionLevel.IMMEDIATE,
            input_model=_NoInput,
            handler=_noop,
            execution_modes=("local", "preview"),
            preview_semantics="non_authoritative_realization",
            required_packs=("probe.pack.stub",) if stub_pack else (),
        ),
    )
    return registry


def _command(operation: str, *, mode: str = "local", command_id: str = "cmd_1", **extra: object):
    return TypedCommand.model_validate(
        {
            "command_id": command_id,
            "operation": operation,
            "input": {},
            "execution_policy": {"mode": mode},
            **extra,
        }
    )


@pytest.fixture()
def stub_pack(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(
        studio_lifecycle.PACK_LIFECYCLE,
        "probe.pack.stub",
        PackLifecycle(pack_id="probe.pack.stub", state=LifecycleState.STUB, reason="probe"),
    )


# --------------------------------------------------------------------- D ----


def test_D1_registry_change_changes_surface_identity() -> None:
    """A registry change must be detectable as a stale snapshot."""
    before = surface_identity(build_capability_surface(_registry()))
    after = surface_identity(build_capability_surface(_registry(version="2.0.0")))
    assert before != after


def test_D2_stale_surface_identity_is_rejected_at_dispatch() -> None:
    """A plan made against surface S1 must not dispatch once the runtime is S2.

    Required: some enforcement point binds the planner's ``surface_identity``
    to dispatch.  The envelope is ``extra="forbid"`` and has no identity field,
    so the only way to *carry* a plan identity is refused outright; staleness is
    detected only per-operation via ``capability_snapshot`` version rules.
    """
    stale = surface_identity(build_capability_surface(_registry()))
    bus = CommandBus(state=_project(), registry=_registry(version="2.0.0"))
    try:
        command = _command("probe.edit", surface_identity=stale)
    except ValidationError:
        pytest.fail(
            "no enforcement point: TypedCommand cannot carry surface_identity, and no "
            "bus/planner API compares a planned identity with the live surface"
        )
    with pytest.raises(NagarError):
        bus.dispatch(command)


def test_D3_stale_capability_snapshot_after_major_bump_is_rejected() -> None:
    old = build_capability_surface(_registry())
    view = next(op for op in old.operations if op.operation_id == "probe.edit")
    snapshot = {
        "capability_id": view.capability_id,
        "operation": view.operation_id,
        "version": view.capability_version,
        "operation_schema_version": view.operation_schema_version,
        "pack_provider": view.pack_provider,
    }
    bus = CommandBus(state=_project(), registry=_registry(version="2.0.0"))
    with pytest.raises(NagarError):
        bus.dispatch(_command("probe.edit", capability_snapshot=snapshot))


def test_D4_excluded_operation_is_not_advertised_and_cannot_dispatch(stub_pack: None) -> None:
    registry = _registry(stub_pack=True)
    surface = build_capability_surface(registry)
    excluded = {entry.operation_id: entry.reason for entry in surface.excluded}
    assert excluded.get("probe.realize") is ExclusionReason.PACK_STUB
    assert "probe.realize" not in {op.operation_id for op in surface.operations}
    bus = CommandBus(state=_project(), registry=registry)
    revision = bus.state_revision
    with pytest.raises(NagarError):
        bus.dispatch(_command("probe.realize"))
    assert bus.state_revision == revision  # zero work on refusal


# --------------------------------------------------------------------- P ----


def test_P1_preview_realization_is_never_master_evidence() -> None:
    bus = CommandBus(state=_project(), registry=_registry())
    result = bus.dispatch(_command("probe.realize", mode="preview"))
    assert result.diagnostics["authoritative"] is False
    assert is_master_evidence(result) is False


def test_P2_forged_authoritative_stamp_is_insufficient() -> None:
    """A hand-built result claiming ``authoritative=True`` must not qualify."""
    forged = CommandResult(
        transaction_id="tx_forged",
        state_revision=1,
        state_hash="sha256:" + "0" * 64,
        diagnostics={"authoritative": True, "execution_mode": "local"},
    )
    assert is_master_evidence(forged) is False


def test_P3_missing_stamp_is_rejected() -> None:
    unstamped = CommandResult(
        transaction_id="tx", state_revision=1, state_hash="sha256:" + "0" * 64
    )
    assert is_master_evidence(unstamped) is False


def test_P4_preview_result_cannot_be_restamped_into_master() -> None:
    """Mutating a genuine preview result's diagnostics must not promote it."""
    bus = CommandBus(state=_project(), registry=_registry())
    preview = bus.dispatch(_command("probe.realize", mode="preview"))
    promoted = preview.model_copy(
        update={"diagnostics": {**preview.diagnostics, "authoritative": True}}
    )
    assert is_master_evidence(promoted) is False


def test_P5_master_evidence_requires_a_verified_artifact() -> None:
    """Verifier absence ⇒ no master/L4: a no-op handler produced no artifact."""
    bus = CommandBus(state=_project(), registry=_registry())
    result = bus.dispatch(_command("probe.realize"))  # local mode, handler wrote nothing
    assert result.output == {"ran": True}
    assert is_master_evidence(result) is False, (
        "is_master_evidence() is a mode stamp only: a local execution that produced "
        "no artifact and was never verified qualifies as 'master evidence'"
    )


# -------------------------------------------------------------------- A2 ----


def test_A2_discover_plan_snapshot_command_dispatch_result_undo() -> None:
    registry = build_wave1_registry()
    surface = build_capability_surface(registry)
    view = next(op for op in surface.operations if op.operation_id == "timeline.mark")
    snapshot = {
        "capability_id": view.capability_id,
        "operation": view.operation_id,
        "version": view.capability_version,
        "operation_schema_version": view.operation_schema_version,
        "pack_provider": view.pack_provider,
    }
    bus = CommandBus(state=_project(), registry=registry)
    base = bus.state_hash
    result = bus.dispatch(
        TypedCommand.model_validate(
            {
                "command_id": "cmd_mark",
                "operation": "timeline.mark",
                "input": {"at": {"kind": "absolute", "timecode_us": 1_000_000}, "label": "m"},
                "capability_snapshot": snapshot,
            }
        )
    )
    assert result.diagnostics["authoritative"] is True
    assert bus.state_hash != base
    undo = bus.dispatch(
        TypedCommand.model_validate({"command_id": "cmd_undo", "operation": "system.undo"})
    )
    assert undo.state_hash == base


# -------------------------------------------------------------------- A3 ----


def test_A3_register_compose_execute_stamp() -> None:
    registry = _registry()
    bus = CommandBus(state=_project(), registry=registry)
    master = bus.dispatch(_command("probe.realize", command_id="cmd_m"))
    preview = bus.dispatch(_command("probe.realize", mode="preview", command_id="cmd_p"))
    assert (master.diagnostics["execution_mode"], master.diagnostics["authoritative"]) == (
        "local",
        True,
    )
    assert (preview.diagnostics["execution_mode"], preview.diagnostics["authoritative"]) == (
        "preview",
        False,
    )
