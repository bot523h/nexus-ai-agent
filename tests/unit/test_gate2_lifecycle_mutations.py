"""Anti-vacuity mutations for the Gate-2 x lifecycle seam (task-183).

A green seam suite can still be vacuous.  Each test here compiles a *mutated*
copy of ``creative/studio/bus.py`` -- the production source, one edit applied --
and asserts the mutant behaves wrongly.  The mutation is not the point; the
proof that the seam tests would go red if someone made that edit is.

* M1 -- delete the lifecycle gate            -> an EXPERIMENTAL pack executes
        without opt-in;
* M2 -- move the gate after the idempotency reservation / precondition check
        -> a refused pack reports the wrong failure and consumes the key;
* M3 -- hardcode ``allow_experimental=True`` -> the opt-in stops meaning
        anything;
* M4 -- fabricate the actor grant instead of calling the authorizer
        -> an unauthorized actor reaches the lifecycle gate and the handler;
* M5 -- skip the capability gate (``describe`` instead of ``check_capability``)
        -> an unavailable capability reaches the handler.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

from nexus_ai_agent.creative.studio.lifecycle import PackRequirementError
from nexus_ai_agent.creative.studio.models import (
    AuthorizationError,
    CapabilityError,
    PreconditionError,
    Preconditions,
    Project,
)
from tests.unit.test_gate2_lifecycle_seam import (
    AVAILABLE_PACK,
    EXPERIMENTAL_PACK,
    Calls,
    build_registry,
    command,
    grant,
)

BUS_SOURCE = (Path(__file__).parents[2] / "src/nexus_ai_agent/creative/studio/bus.py").read_text(
    encoding="utf-8"
)

GATE_CALL = """        if descriptor.required_packs:
            check_required_packs(
                descriptor.required_packs, allow_experimental=self._allow_experimental
            )
"""
AUTHORIZE_CALL = "            access = self._authorizer.authorize(command.actor, project_id)"
CAPABILITY_CALL = "        descriptor = self._registry.check_capability(command)"
APPLY_MARKER = (
    "    def _apply_guarded(\n"
    "        self, command: TypedCommand, spec: OperationSpec, input_data: dict[str, Any]\n"
    "    ) -> CommandResult:\n"
)


def _mutant(source: str, name: str) -> ModuleType:
    spec = importlib.util.spec_from_loader(name, loader=None)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    module.__file__ = str(BUS_SOURCE)
    sys.modules[name] = module
    try:
        exec(compile(source, f"<mutant {name}>", "exec"), module.__dict__)
    finally:
        sys.modules.pop(name, None)
    return module


def _replace_once(old: str, new: str) -> str:
    assert BUS_SOURCE.count(old) == 1, f"mutation anchor drifted: {old[:60]!r}"
    return BUS_SOURCE.replace(old, new)


@pytest.fixture
def project() -> Project:
    from nexus_ai_agent.creative.studio import Timeline, new_project

    return new_project(
        "project_01", "Mutation Probe", Timeline(timeline_id="tl_01", duration_us=10_000_000)
    )


def _bus(module: ModuleType, project: Project, calls: Calls, packs: tuple[str, ...], **kw: object):
    available = bool(kw.pop("available", True))
    actor_id = str(kw.pop("authorized_actor", "alice"))
    allow_experimental = bool(kw.pop("allow_experimental", False))
    assert not kw
    return module.CommandBus(
        project,
        registry=build_registry(packs, calls, available=available),
        authorizer=grant(project, actor_id),
        allow_experimental=allow_experimental,
    )


def test_m1_deleting_the_lifecycle_gate_lets_an_experimental_pack_execute(
    project: Project,
) -> None:
    calls = Calls()
    mutant = _mutant(_replace_once(GATE_CALL, ""), "mutant_bus_m1")
    bus = _bus(mutant, project, calls, (EXPERIMENTAL_PACK,))
    bus.dispatch(command(project))  # canonical bus raises PackRequirementError here
    assert calls.count == 1, "M1 is vacuous: the gate was never load-bearing"


def test_m2_moving_the_gate_after_the_reservation_changes_the_failure(
    project: Project,
) -> None:
    """The mutant checks preconditions first, so the pack refusal is masked."""
    moved = _replace_once(GATE_CALL, "").replace(
        APPLY_MARKER,
        APPLY_MARKER
        + """        if spec.required_packs:
            check_required_packs(spec.required_packs, allow_experimental=self._allow_experimental)
""",
        1,
    )
    mutant = _mutant(moved, "mutant_bus_m2")
    calls = Calls()
    bus = _bus(mutant, project, calls, (EXPERIMENTAL_PACK,))
    stale = command(project, preconditions=Preconditions(state_revision=999))
    with pytest.raises(PreconditionError):
        bus.dispatch(stale)
    assert calls.count == 0
    # The canonical bus refuses the same command on the *pack*, not the revision.
    from nexus_ai_agent.creative.studio import CommandBus

    canonical = CommandBus(
        project,
        registry=build_registry((EXPERIMENTAL_PACK,), Calls()),
        authorizer=grant(project),
    )
    with pytest.raises(PackRequirementError):
        canonical.dispatch(stale)


def test_m3_hardcoding_the_opt_in_destroys_the_experimental_contract(
    project: Project,
) -> None:
    mutant = _mutant(
        _replace_once(
            "allow_experimental=self._allow_experimental\n            )",
            "allow_experimental=True\n            )",
        ),
        "mutant_bus_m3",
    )
    calls = Calls()
    bus = _bus(mutant, project, calls, (EXPERIMENTAL_PACK,), allow_experimental=False)
    bus.dispatch(command(project))
    assert calls.count == 1, "M3 is vacuous: allow_experimental is ignored anyway"


def test_m4_fabricating_the_actor_grant_lets_an_intruder_through(project: Project) -> None:
    mutant = _mutant(
        _replace_once(
            AUTHORIZE_CALL,
            "            access = ProjectAccess(\n"
            "                actor=command.actor,\n"
            "                project_id=project_id,\n"
            '                permissions=frozenset({"project:read", "project:write"}),\n'
            "            )",
        ),
        "mutant_bus_m4",
    )
    calls = Calls()
    bus = _bus(mutant, project, calls, (EXPERIMENTAL_PACK,), allow_experimental=True)
    bus.dispatch(command(project, actor_id="mallory"))
    assert calls.count == 1, "M4 is vacuous: the authorizer never gated anything"

    from nexus_ai_agent.creative.studio import CommandBus

    canonical = CommandBus(
        project,
        registry=build_registry((EXPERIMENTAL_PACK,), Calls()),
        authorizer=grant(project),
        allow_experimental=True,
    )
    with pytest.raises(AuthorizationError):
        canonical.dispatch(command(project, actor_id="mallory"))


def test_m5_skipping_the_capability_gate_lets_the_handler_run(project: Project) -> None:
    mutant = _mutant(
        _replace_once(
            CAPABILITY_CALL,
            "        descriptor = self._registry.describe(command.operation)",
        ),
        "mutant_bus_m5",
    )
    calls = Calls()
    bus = _bus(mutant, project, calls, (AVAILABLE_PACK,), available=False)
    bus.dispatch(command(project))
    assert calls.count == 1, "M5 is vacuous: the capability gate never denied anything"

    from nexus_ai_agent.creative.studio import CommandBus

    canonical = CommandBus(
        project,
        registry=build_registry((AVAILABLE_PACK,), Calls(), available=False),
        authorizer=grant(project),
    )
    with pytest.raises(CapabilityError):
        canonical.dispatch(command(project))
