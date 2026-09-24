"""The 4-state lifecycle machine and the fail-closed pack gate (session 3).

Every pack operation declares ``required_packs``; session 2 proved nothing
checked it.  This file pins the full (state × gate) matrix:

* ``AVAILABLE`` passes with or without opt-in;
* ``EXPERIMENTAL`` refuses without opt-in, passes with it;
* ``STUB``/``RETIRED`` refuse unconditionally; unknown ids refuse loudly;
* the bus enforces the gate at step 3.5 (before permission/input work), so a
  refused pack does no work and leaves state untouched;
* every ``required_packs`` id in the runtime registry resolves (a typo'd
  pack id anywhere in the 77-op catalog fails the suite).
"""

from __future__ import annotations

import pytest

from nexus_ai_agent.creative.packs.runtime import build_runtime_registry
from nexus_ai_agent.creative.studio.bus import CommandBus
from nexus_ai_agent.creative.studio.lifecycle import (
    PACK_LIFECYCLE,
    LifecycleState,
    PackLifecycle,
    PackRequirementError,
    check_required_packs,
    pack_lifecycle,
)
from nexus_ai_agent.creative.studio.models import (
    Timeline,
    TypedCommand,
    new_project,
)


def _record(state: LifecycleState) -> PackLifecycle:
    return PackLifecycle(pack_id="test.pack", state=state, reason="matrix probe")


# ---------------------------------------------------------------------------
# the state × gate matrix, cell by cell
# ---------------------------------------------------------------------------


def test_available_passes_without_opt_in() -> None:
    _record(LifecycleState.AVAILABLE).require_executable(allow_experimental=False)


def test_available_passes_with_opt_in() -> None:
    _record(LifecycleState.AVAILABLE).require_executable(allow_experimental=True)


def test_experimental_refuses_without_opt_in() -> None:
    with pytest.raises(PackRequirementError, match="experimental.*AVAILABLE"):
        _record(LifecycleState.EXPERIMENTAL).require_executable(allow_experimental=False)


def test_experimental_passes_with_opt_in() -> None:
    _record(LifecycleState.EXPERIMENTAL).require_executable(allow_experimental=True)


def test_stub_refuses_even_with_opt_in() -> None:
    with pytest.raises(PackRequirementError, match="stub"):
        _record(LifecycleState.STUB).require_executable(allow_experimental=True)


def test_retired_refuses_and_names_the_successor() -> None:
    record = PackLifecycle(
        pack_id="old.pack",
        state=LifecycleState.RETIRED,
        reason="withdrawn",
        successor="new.pack",
    )
    with pytest.raises(PackRequirementError, match="retired.*successor: new.pack"):
        record.require_executable(allow_experimental=True)


def test_unknown_pack_id_fails_closed_with_known_list() -> None:
    with pytest.raises(PackRequirementError, match="unknown pack.*nexus.slideshow.compose"):
        pack_lifecycle("nexus.typo.pack")


def test_gate_collapses_duplicates_and_passes_empty() -> None:
    assert check_required_packs(()) == ()
    resolved = check_required_packs(["nexus.slideshow.compose", "nexus.slideshow.compose"])
    assert [record.pack_id for record in resolved] == ["nexus.slideshow.compose"]


def test_gate_names_the_first_refusal() -> None:
    with pytest.raises(PackRequirementError, match="nexus.audio.studio.*experimental"):
        check_required_packs(["nexus.slideshow.compose", "nexus.audio.studio"])


# ---------------------------------------------------------------------------
# the shipped table: real ids, honest states
# ---------------------------------------------------------------------------


def test_table_covers_exactly_the_eight_manifest_packs() -> None:
    assert sorted(PACK_LIFECYCLE) == [
        "nexus.audio.studio",
        "nexus.color.delivery",
        "nexus.edit.timeline",
        "nexus.language.caption",
        "nexus.motion.graphics",
        "nexus.slideshow.compose",
        "nexus.vision.portrait",
        "nexus.vision.scene",
    ]


def test_every_registry_pack_id_resolves() -> None:
    """A typo'd ``required_packs`` id anywhere in the catalog fails here."""
    registry = build_runtime_registry()
    declared: set[str] = set()
    for operation_id in registry.list_operations():
        declared.update(registry.get_spec(operation_id).required_packs)
    assert declared  # the gate has something to guard
    for pack_id in sorted(declared):
        assert pack_lifecycle(pack_id).pack_id == pack_id


def test_proven_packs_are_available_and_mapped_packs_experimental() -> None:
    assert PACK_LIFECYCLE["nexus.slideshow.compose"].state is LifecycleState.AVAILABLE
    assert PACK_LIFECYCLE["nexus.language.caption"].state is LifecycleState.AVAILABLE
    assert PACK_LIFECYCLE["nexus.edit.timeline"].state is LifecycleState.AVAILABLE
    for pack_id in (
        "nexus.audio.studio",
        "nexus.motion.graphics",
        "nexus.color.delivery",
        "nexus.vision.portrait",
        "nexus.vision.scene",
    ):
        assert PACK_LIFECYCLE[pack_id].state is LifecycleState.EXPERIMENTAL, pack_id


# ---------------------------------------------------------------------------
# bus integration: step 3.5 refuses before any work
# ---------------------------------------------------------------------------


def _project() -> object:
    timeline = Timeline(timeline_id="tl", duration_us=10_000_000)
    return new_project("p", "Gate", timeline)


def test_bus_refuses_experimental_pack_without_opt_in() -> None:
    from nexus_ai_agent.creative.packs.audio.operations import build_audio_registry

    bus = CommandBus(_project(), registry=build_audio_registry())  # type: ignore[arg-type]
    before = (bus.state_revision, bus.state_hash)
    with pytest.raises(PackRequirementError, match="nexus.audio.studio"):
        bus.dispatch(
            TypedCommand(
                command_id="c1",
                operation="audio.detect_beats",
                input={"audio_asset_id": "x", "sensitivity": 0.5},
            )
        )
    assert (bus.state_revision, bus.state_hash) == before  # no work done


def test_bus_passes_experimental_pack_with_opt_in() -> None:
    from nexus_ai_agent.creative.packs.audio.operations import build_audio_registry
    from nexus_ai_agent.creative.studio.models import AssetRecord

    project = _project()
    project = project.model_copy(
        update={
            "assets": [
                AssetRecord(
                    asset_id="music",
                    media_kind="audio",
                    content_sha256="sha256:m",
                    duration_us=10_000_000,
                )
            ]
        }
    )
    bus = CommandBus(project, registry=build_audio_registry(), allow_experimental=True)  # type: ignore[arg-type]
    result = bus.dispatch(
        TypedCommand(
            command_id="c1",
            operation="audio.detect_beats",
            input={"audio_asset_id": "music", "sensitivity": 0.5},
        )
    )
    assert result.status == "applied"


def test_bus_passes_available_pack_without_opt_in() -> None:
    from nexus_ai_agent.creative.packs.slideshow.operations import build_slideshow_registry

    bus = CommandBus(_project(), registry=build_slideshow_registry())  # type: ignore[arg-type]
    result = bus.dispatch(
        TypedCommand(
            command_id="c1",
            operation="slideshow.suggest_tone",
            input={"tempo_bpm": 100.0, "image_count": 5},
        )
    )
    assert result.status == "applied"


def test_bus_refuses_unknown_pack_id() -> None:
    from dataclasses import replace

    from nexus_ai_agent.creative.packs.audio.operations import build_audio_registry

    registry = build_audio_registry()
    spec = registry.get_spec("audio.detect_beats")
    tampered = replace(spec, required_packs=("nexus.typo.pack",))
    registry._index["audio.detect_beats"] = tampered  # simulate a typo'd declaration
    bus = CommandBus(_project(), registry=registry, allow_experimental=True)  # type: ignore[arg-type]
    with pytest.raises(PackRequirementError, match="unknown pack"):
        bus.dispatch(
            TypedCommand(
                command_id="c1",
                operation="audio.detect_beats",
                input={"audio_asset_id": "x", "sensitivity": 0.5},
            )
        )
