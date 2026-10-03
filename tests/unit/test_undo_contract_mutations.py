"""Anti-vacuity mutations for the undo identity contract (task-223).

Each test compiles a *mutated* copy of ``creative/studio/capabilities.py`` --
the production source with one edit applied -- builds its Wave-1 registry, and
asserts the mutant behaves wrongly. The mutation is not the point; the proof that
``TestUndoIdentity`` would go red if someone made that edit is.

* M1 -- disable the identity gate             -> a stale/foreign id rewinds the
        newest (foreign) transaction again;
* M2 -- compare against the wrong field       -> a matching identity is refused;
* M3 -- invert the comparison                 -> a mismatching identity is
        accepted and rewinds a foreign transaction.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

from nexus_ai_agent.creative.studio import (
    CommandBus,
    TypedCommand,
    UndoConflictError,
    new_project,
)
from nexus_ai_agent.creative.studio.models import Project, Timeline
from nexus_ai_agent.creative.studio.testing import DEFAULT_STUDIO_ACTOR, default_authorizer

CAPABILITIES_SOURCE = (
    Path(__file__).parents[2] / "src/nexus_ai_agent/creative/studio/capabilities.py"
).read_text(encoding="utf-8")

GATE_ANCHOR = """    requested = context.input_data.get("transaction_id")
    if requested is not None and requested != last.transaction_id:
"""


def _mutant(source: str, name: str) -> ModuleType:
    spec = importlib.util.spec_from_loader(name, loader=None)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    module.__file__ = str(Path(__file__).parents[2] / "src/nexus_ai_agent/creative/studio")
    sys.modules[name] = module
    try:
        exec(compile(source, f"<mutant {name}>", "exec"), module.__dict__)
    finally:
        sys.modules.pop(name, None)
    return module


def _replace_once(old: str, new: str) -> str:
    assert CAPABILITIES_SOURCE.count(old) == 1, f"mutation anchor drifted: {old[:60]!r}"
    return CAPABILITIES_SOURCE.replace(old, new)


def _command(operation: str, command_id: str, **input_data: object) -> TypedCommand:
    return TypedCommand.model_validate(
        {
            "command_id": command_id,
            "operation": operation,
            "actor": DEFAULT_STUDIO_ACTOR.model_dump(mode="json"),
            "input": input_data,
        }
    )


def _project() -> Project:
    return new_project(
        "project_01", "Undo Mutation Probe", Timeline(timeline_id="tl_01", duration_us=10_000_000)
    )


def _bus(module: ModuleType) -> CommandBus:
    return CommandBus(
        state=_project(),
        registry=module.build_wave1_registry(),
        authorizer=default_authorizer("project_01"),
    )


def test_baseline_unmutated_registry_refuses_a_stale_identity() -> None:
    # Sanity: the production registry is the reference the mutants deviate from.
    module = _mutant(CAPABILITIES_SOURCE, "mutant_undo_baseline")
    bus = _bus(module)
    ours = bus.dispatch(_command("timeline.mark", "cmd_ours", at="اینجا", label="OURS"))
    bus.dispatch(_command("timeline.mark", "cmd_foreign", at="اینجا", label="FOREIGN"))
    with pytest.raises(UndoConflictError):
        bus.dispatch(_command("system.undo", "cmd_undo", transaction_id=ours.transaction_id))


def test_m1_disabling_the_gate_lets_a_stale_id_rewind_the_foreign_edit() -> None:
    mutant = _mutant(
        _replace_once(
            GATE_ANCHOR,
            '    requested = context.input_data.get("transaction_id")\n    if False:\n',
        ),
        "mutant_undo_m1",
    )
    bus = _bus(mutant)
    ours = bus.dispatch(_command("timeline.mark", "cmd_ours", at="اینجا", label="OURS"))
    bus.dispatch(_command("timeline.mark", "cmd_foreign", at="اینجا", label="FOREIGN"))
    # M1 is vacuous if the gate was never load-bearing: the stale-id undo must
    # now succeed and destroy the FOREIGN edit.
    bus.dispatch(_command("system.undo", "cmd_undo", transaction_id=ours.transaction_id))
    assert [m.label for m in bus.project.timeline.markers] == ["OURS"], (
        "M1 is vacuous: removing the identity gate did not re-expose the defect"
    )


def test_m2_comparing_the_wrong_field_refuses_a_matching_identity() -> None:
    mutant = _mutant(
        _replace_once(GATE_ANCHOR, GATE_ANCHOR.replace("last.transaction_id", "last.command_id")),
        "mutant_undo_m2",
    )
    bus = _bus(mutant)
    marked = bus.dispatch(_command("timeline.mark", "cmd_mark", at="اینجا", label="x"))
    # A matching transaction_id must be refused by M2 (it compares command_id),
    # which proves the field actually compared is transaction_id.
    with pytest.raises(UndoConflictError):
        bus.dispatch(_command("system.undo", "cmd_undo", transaction_id=marked.transaction_id))


def test_m3_inverting_the_comparison_accepts_a_foreign_identity() -> None:
    inverted = GATE_ANCHOR.replace(
        "requested != last.transaction_id", "requested == last.transaction_id"
    )
    mutant = _mutant(_replace_once(GATE_ANCHOR, inverted), "mutant_undo_m3")
    bus = _bus(mutant)
    ours = bus.dispatch(_command("timeline.mark", "cmd_ours", at="اینجا", label="OURS"))
    bus.dispatch(_command("timeline.mark", "cmd_foreign", at="اینجا", label="FOREIGN"))
    # Inverted: a mismatching identity (ours, not the newest) is now accepted and
    # rewinds the foreign transaction.
    bus.dispatch(_command("system.undo", "cmd_undo", transaction_id=ours.transaction_id))
    assert [m.label for m in bus.project.timeline.markers] == ["OURS"], (
        "M3 is vacuous: inverting the comparison did not accept a foreign identity"
    )
