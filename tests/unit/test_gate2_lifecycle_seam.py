"""Gate 2 x capability lifecycle seam (task-183).

PR#72 owns the canonical Gate-2 pipeline; PR#67 owns the capability lifecycle
(``required_packs`` / ``allow_experimental``).  This module proves the *single*
seam between them: the pack gate sits at step 4b -- after the trusted
actor/capability grant and before policy, reference pinning, idempotency
reservation and the pure handler.

Cases A-H of the integration contract:

* A unknown pack           -> refused, zero handler execution
* B STUB / RETIRED         -> refused, zero side effect
* C EXPERIMENTAL, no opt-in-> refused
* D EXPERIMENTAL, opt-in   -> allowed
* E unauthorized actor     -> refused *before* lifecycle can grant anything
* F capability unavailable -> refused even for a fully authorized actor
* G actor + capability + lifecycle -> reaches the canonical handler
* H duplicate replay       -> no second effect
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from nexus_ai_agent.creative.studio import (
    ActorIdentity,
    AuthorizationError,
    CapabilityError,
    CapabilityRegistry,
    CommandBus,
    CommandProvenance,
    CommandValidationError,
    OperationContext,
    OperationOutcome,
    OperationSpec,
    PermissionLevel,
    Project,
    ProjectAccess,
    Timeline,
    TypedCommand,
    UnknownOperationError,
    new_project,
)
from nexus_ai_agent.creative.studio.lifecycle import (
    PACK_LIFECYCLE,
    LifecycleState,
    PackLifecycle,
    PackRequirementError,
)
from nexus_ai_agent.creative.studio.models import (
    ExecutionPolicy,
    ExecutionPolicyError,
    InputRef,
    InputReferenceError,
    Preconditions,
    TargetRef,
)

AVAILABLE_PACK = "nexus.slideshow.compose"
EXPERIMENTAL_PACK = "nexus.audio.studio"
UNKNOWN_PACK = "nexus.typo.pack"
STUB_PACK = "test.stub.pack"
RETIRED_PACK = "test.retired.pack"


class LabInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str = "ok"


class Calls:
    """Runtime-execution witness: the handler is the only thing that bumps it."""

    def __init__(self) -> None:
        self.count = 0


@pytest.fixture
def project() -> Project:
    return new_project(
        "project_01", "Lifecycle Seam", Timeline(timeline_id="tl_01", duration_us=10_000_000)
    )


@pytest.fixture
def calls() -> Calls:
    return Calls()


def build_registry(
    required_packs: tuple[str, ...],
    calls: Calls,
    *,
    available: bool = True,
) -> CapabilityRegistry:
    registry = CapabilityRegistry()
    registry.register_domain("lab", "Seam probe domain")

    def _handler(project: Project, context: OperationContext) -> OperationOutcome:
        calls.count += 1
        return OperationOutcome(project, context.history, {"ran": True})

    registry.register_operation(
        "lab",
        "packs",
        OperationSpec(
            operation_id="lab.run",
            description="Probe operation guarded by a pack requirement.",
            permission_level=PermissionLevel.REVERSIBLE,
            input_model=LabInput,
            handler=_handler,
            required_packs=required_packs,
        ),
        available=available,
    )
    return registry


def actor(actor_id: str = "alice") -> ActorIdentity:
    return ActorIdentity(kind="user", actor_id=actor_id)


def grant(project: Project, actor_id: str = "alice") -> ProjectAccess:
    return ProjectAccess(
        actor=actor(actor_id),
        project_id=project.project_id,
        permissions=frozenset({"project:read", "project:write"}),
    )


def command(
    project: Project,
    *,
    actor_id: str = "alice",
    idempotency_key: str | None = None,
    command_id: str = "cmd_01",
    preconditions: Preconditions | None = None,
) -> TypedCommand:
    payload: dict[str, Any] = {
        "command_id": command_id,
        "schema_version": 2,
        "operation": "lab.run",
        "actor": actor(actor_id).model_dump(mode="json"),
        "target": TargetRef(project_id=project.project_id).model_dump(mode="json"),
        "provenance": CommandProvenance(source="local", source_id="seam-test").model_dump(
            mode="json"
        ),
        "input": {"note": "ok"},
    }
    if idempotency_key is not None:
        payload["idempotency_key"] = idempotency_key
    if preconditions is not None:
        payload["preconditions"] = preconditions.model_dump(mode="json")
    return TypedCommand.model_validate(payload)


def make_bus(
    project: Project,
    calls: Calls,
    required_packs: tuple[str, ...],
    *,
    allow_experimental: bool = False,
    available: bool = True,
    authorized_actor: str | None = "alice",
) -> CommandBus:
    authorizer = grant(project, authorized_actor) if authorized_actor is not None else None
    return CommandBus(
        project,
        registry=build_registry(required_packs, calls, available=available),
        authorizer=authorizer,
        allow_experimental=allow_experimental,
    )


@pytest.fixture
def stub_and_retired(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(
        PACK_LIFECYCLE,
        STUB_PACK,
        PackLifecycle(pack_id=STUB_PACK, state=LifecycleState.STUB, reason="probe"),
    )
    monkeypatch.setitem(
        PACK_LIFECYCLE,
        RETIRED_PACK,
        PackLifecycle(
            pack_id=RETIRED_PACK,
            state=LifecycleState.RETIRED,
            reason="withdrawn",
            successor="test.new.pack",
        ),
    )


def assert_untouched(bus: CommandBus, calls: Calls, revision: int) -> None:
    """Zero execution *and* zero project mutation (revision, hash, history)."""
    assert calls.count == 0
    assert bus.state_revision == revision
    assert bus.history == ()
    assert bus.state_hash == bus.project.state_hash
    assert bus.project.state_revision == revision


# ---------------------------------------------------------------------------
# A-H: the integration contract
# ---------------------------------------------------------------------------


class TestLifecycleSeam:
    def test_a_unknown_pack_is_refused_with_zero_execution(
        self, project: Project, calls: Calls
    ) -> None:
        bus = make_bus(project, calls, (UNKNOWN_PACK,), allow_experimental=True)
        with pytest.raises(PackRequirementError, match="unknown pack"):
            bus.dispatch(command(project))
        assert_untouched(bus, calls, project.state_revision)

    @pytest.mark.parametrize("pack", [STUB_PACK, RETIRED_PACK])
    def test_b_stub_and_retired_refuse_with_zero_side_effect(
        self, project: Project, calls: Calls, stub_and_retired: None, pack: str
    ) -> None:
        bus = make_bus(project, calls, (pack,), allow_experimental=True)
        before = bus.state_hash
        with pytest.raises(PackRequirementError):
            bus.dispatch(command(project))
        assert_untouched(bus, calls, project.state_revision)
        assert bus.state_hash == before

    def test_c_experimental_without_opt_in_is_refused(self, project: Project, calls: Calls) -> None:
        bus = make_bus(project, calls, (EXPERIMENTAL_PACK,))
        with pytest.raises(PackRequirementError, match="experimental"):
            bus.dispatch(command(project))
        assert_untouched(bus, calls, project.state_revision)

    def test_d_experimental_with_explicit_opt_in_executes(
        self, project: Project, calls: Calls
    ) -> None:
        bus = make_bus(project, calls, (EXPERIMENTAL_PACK,), allow_experimental=True)
        result = bus.dispatch(command(project))
        assert calls.count == 1
        assert result.output == {"ran": True}

    def test_e_unauthorized_actor_is_refused_before_the_lifecycle_gate(
        self, project: Project, calls: Calls
    ) -> None:
        """An intruder cannot reach -- let alone be granted by -- the pack gate."""
        bus = make_bus(
            project,
            calls,
            (EXPERIMENTAL_PACK,),
            allow_experimental=True,
            authorized_actor="alice",
        )
        with pytest.raises(AuthorizationError):
            bus.dispatch(command(project, actor_id="mallory"))
        assert_untouched(bus, calls, project.state_revision)

    def test_e2_unauthorized_actor_on_an_unknown_pack_fails_on_authorization(
        self, project: Project, calls: Calls
    ) -> None:
        """Order proof: authorization error, not the pack error, surfaces first."""
        bus = make_bus(project, calls, (UNKNOWN_PACK,), authorized_actor="alice")
        with pytest.raises(AuthorizationError):
            bus.dispatch(command(project, actor_id="mallory"))

    def test_e3_actor_lacking_permissions_cannot_be_rescued_by_lifecycle(
        self, project: Project, calls: Calls
    ) -> None:
        read_only = ProjectAccess(
            actor=actor(), project_id=project.project_id, permissions=frozenset({"project:read"})
        )
        bus = CommandBus(
            project,
            registry=build_registry((AVAILABLE_PACK,), calls),
            authorizer=read_only,
            allow_experimental=True,
        )
        with pytest.raises(AuthorizationError, match="permissions"):
            bus.dispatch(command(project))
        assert calls.count == 0

    def test_f_missing_capability_with_a_valid_actor_is_refused(
        self, project: Project, calls: Calls
    ) -> None:
        bus = make_bus(project, calls, (AVAILABLE_PACK,), available=False)
        with pytest.raises(CapabilityError, match="unavailable"):
            bus.dispatch(command(project))
        assert_untouched(bus, calls, project.state_revision)

    def test_g_valid_actor_capability_and_lifecycle_reach_the_handler(
        self, project: Project, calls: Calls
    ) -> None:
        bus = make_bus(project, calls, (AVAILABLE_PACK,))
        result = bus.dispatch(command(project))
        assert calls.count == 1
        assert result.state_revision == project.state_revision + 1
        assert [tx.operation for tx in bus.history] == ["lab.run"]

    def test_h_duplicate_replay_has_no_second_effect(self, project: Project, calls: Calls) -> None:
        bus = make_bus(project, calls, (EXPERIMENTAL_PACK,), allow_experimental=True)
        first = bus.dispatch(command(project, idempotency_key="key-1"))
        second = bus.dispatch(command(project, idempotency_key="key-1", command_id="cmd_02"))
        assert calls.count == 1
        assert second.transaction_id == first.transaction_id
        assert bus.state_revision == first.state_revision


# ---------------------------------------------------------------------------
# seam placement: the gate's neighbours in the canonical order
# ---------------------------------------------------------------------------


class TestSeamPlacement:
    def test_lifecycle_runs_before_the_idempotency_reservation(
        self, project: Project, calls: Calls, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A refused pack must not consume (or poison) an idempotency key."""
        bus = make_bus(project, calls, (EXPERIMENTAL_PACK,))
        with pytest.raises(PackRequirementError):
            bus.dispatch(command(project, idempotency_key="key-1"))
        assert calls.count == 0

        # The same key still works once the opt-in is in place: no reservation
        # survived the refusal.
        opened = make_bus(project, calls, (EXPERIMENTAL_PACK,), allow_experimental=True)
        result = opened.dispatch(command(project, idempotency_key="key-1"))
        assert calls.count == 1
        assert result.output == {"ran": True}

    def test_lifecycle_runs_before_revision_preconditions(
        self, project: Project, calls: Calls
    ) -> None:
        """A stale command on a refused pack reports the pack, not the revision."""
        bus = make_bus(project, calls, (EXPERIMENTAL_PACK,))
        stale = command(project, preconditions=Preconditions(state_revision=999))
        with pytest.raises(PackRequirementError):
            bus.dispatch(stale)
        assert calls.count == 0

    def test_lifecycle_runs_before_execution_policy(self, project: Project, calls: Calls) -> None:
        """4b before 5: an unadvertised mode on a refused pack reports the pack."""
        preview = command(project).model_copy(
            update={"execution_policy": ExecutionPolicy(mode="preview")}
        )
        with pytest.raises(PackRequirementError):
            make_bus(project, calls, (EXPERIMENTAL_PACK,)).dispatch(preview)
        # Positive control: with the gate open, the same command reaches stage 5.
        with pytest.raises(ExecutionPolicyError):
            make_bus(project, calls, (EXPERIMENTAL_PACK,), allow_experimental=True).dispatch(
                preview
            )
        assert calls.count == 0

    def test_lifecycle_runs_before_reference_validation(
        self, project: Project, calls: Calls
    ) -> None:
        """4b before 6: a dangling input ref on a refused pack reports the pack."""
        ghost = command(project).model_copy(
            update={
                "input_refs": (
                    InputRef(ref_type="asset", project_id=project.project_id, ref_id="ghost"),
                )
            }
        )
        with pytest.raises(PackRequirementError):
            make_bus(project, calls, (EXPERIMENTAL_PACK,)).dispatch(ghost)
        # Positive control: with the gate open, the same command reaches stage 6.
        with pytest.raises(InputReferenceError):
            make_bus(project, calls, (EXPERIMENTAL_PACK,), allow_experimental=True).dispatch(ghost)
        assert calls.count == 0

    def test_unknown_operation_never_reaches_the_lifecycle_gate(
        self, project: Project, calls: Calls
    ) -> None:
        bus = make_bus(project, calls, (AVAILABLE_PACK,))
        payload = command(project).model_dump(mode="json")
        payload["operation"] = "lab.ghost"
        with pytest.raises(UnknownOperationError):
            bus.dispatch(payload)
        assert calls.count == 0

    def test_allow_experimental_is_not_an_envelope_field(self, project: Project) -> None:
        """A client cannot widen its own lifecycle through the command JSON."""
        payload = command(project).model_dump(mode="json")
        payload["allow_experimental"] = True
        with pytest.raises(ValidationError):
            TypedCommand.model_validate(payload)

    def test_required_packs_are_registry_truth_not_client_input(
        self, project: Project, calls: Calls
    ) -> None:
        """The envelope cannot shrink the pack requirement to an empty tuple."""
        bus = make_bus(project, calls, (EXPERIMENTAL_PACK,))
        payload = command(project).model_dump(mode="json")
        payload["required_packs"] = []
        with pytest.raises(CommandValidationError):
            bus.dispatch(payload)
        assert calls.count == 0

    def test_no_pack_requirement_means_no_gate(self, project: Project, calls: Calls) -> None:
        bus = make_bus(project, calls, ())
        bus.dispatch(command(project))
        assert calls.count == 1


# ---------------------------------------------------------------------------
# the live runtime registry must resolve against the lifecycle table
# ---------------------------------------------------------------------------


def test_every_runtime_required_pack_resolves_in_the_lifecycle_table() -> None:
    from nexus_ai_agent.creative.packs.runtime import build_runtime_registry

    registry = build_runtime_registry()
    unknown = {
        pack
        for operation in registry.list_operations()
        for pack in registry.get_spec(operation).required_packs
        if pack not in PACK_LIFECYCLE
    }
    assert unknown == set()
