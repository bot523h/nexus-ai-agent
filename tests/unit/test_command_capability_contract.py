"""Gate 2 boundary probes (failure stage is named in each test).

No live server, wall clock, shell or media encoder: the same typed bus accepts
JSON for an API and a model for a local engine. A spy handler proves that none
of the negative cases can reach an operation's execution request.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import Any

import pytest
from nagar_helpers import TEST_ACTOR, authorized_bus, command_for

from nexus_ai_agent.creative.packs.runtime import build_runtime_registry
from nexus_ai_agent.creative.studio.authorization import ProjectAccess
from nexus_ai_agent.creative.studio.bus import CommandBus
from nexus_ai_agent.creative.studio.capabilities import (
    CapabilityRegistry,
    MarkCommandInput,
    OperationContext,
    OperationOutcome,
    OperationSpec,
    build_wave1_registry,
)
from nexus_ai_agent.creative.studio.models import (
    AssetRecord,
    AuthorizationError,
    CapabilityError,
    CapabilityVersionError,
    Clip,
    CommandExecutionError,
    CommandValidationError,
    ExecutionPolicyError,
    IdempotencyConflictError,
    InputReferenceError,
    MediaRef,
    PermissionLevel,
    Project,
    Timeline,
    TimeRangeUS,
    Track,
    TypedCommand,
    UnknownCapabilityError,
    UnknownOperationError,
    compute_state_hash,
    new_project,
)

HASH = "sha256:" + "ab" * 32


def _project(project_id: str = "p-1") -> Project:
    media = MediaRef(
        asset_id="source", content_sha256=HASH, media_kind="video", duration_us=5_000_000
    )
    clip = Clip(
        clip_id="clip-1",
        media_ref=media,
        source_range=TimeRangeUS(start_us=0, end_us=5_000_000),
        timeline_range=TimeRangeUS(start_us=0, end_us=5_000_000),
    )
    timeline = Timeline(
        timeline_id="timeline-1",
        duration_us=5_000_000,
        tracks=[Track(track_id="video", name="Video", kind="video", clips=[clip])],
    )
    project = new_project(project_id, "Project", timeline).model_copy(
        update={
            "assets": [
                AssetRecord(
                    asset_id="source",
                    media_kind="video",
                    content_sha256=HASH,
                    duration_us=5_000_000,
                )
            ]
        }
    )
    return Project.model_validate(project.model_dump(mode="json"))


def _command(bus: CommandBus, **changes: Any) -> dict[str, Any]:
    command = command_for(
        bus,
        command_id="cmd-1",
        operation="timeline.mark",
        input={"at": "start", "label": "one"},
        idempotency_key="key-1",
    ).model_dump(mode="json")
    command.update(changes)
    return command


def _spy_bus(project: Project | None = None) -> tuple[CommandBus, list[str]]:
    project = project or _project()
    calls: list[str] = []

    def handler(state: Project, context: OperationContext) -> OperationOutcome:
        calls.append(context.command.command_id)
        return OperationOutcome(state, context.history, {"calls": len(calls)})

    registry = CapabilityRegistry()
    registry.register_operation(
        "timeline",
        "marking",
        OperationSpec(
            operation_id="timeline.mark",
            description="spy",
            permission_level=PermissionLevel.REVERSIBLE,
            input_model=MarkCommandInput,
            handler=handler,
            reference_fields=("at",),
        ),
    )
    return authorized_bus(project, registry=registry), calls


def test_bus_revalidates_initial_project_snapshot_and_derived_state_hash() -> None:
    project = _project().model_copy(update={"state_hash": "sha256:forged"})
    bus = authorized_bus(project)
    assert bus.state_hash == compute_state_hash(bus.project)
    assert bus.state_hash != project.state_hash
    # A forged precondition against a stale state hash cannot authorize a write.
    from nexus_ai_agent.creative.studio.models import PreconditionError

    with pytest.raises(PreconditionError, match="stale state_hash"):
        bus.dispatch(_command(bus, preconditions={"state_hash": project.state_hash}))
    assert bus.state_revision == 0
    assert bus._idempotency == {}


def test_typed_envelope_json_round_trip_for_api_and_local_engine() -> None:
    bus = authorized_bus(_project())
    snapshot = build_wave1_registry().describe("timeline.mark").snapshot()
    command = command_for(
        bus,
        command_id="cmd-json",
        operation="timeline.mark",
        input={"at": {"kind": "absolute", "timecode_us": 250}, "label": "درود"},
        input_refs=[
            {
                "ref_type": "asset",
                "project_id": "p-1",
                "ref_id": "source",
                "metadata": {"media_kind": "video", "content_sha256": HASH},
            }
        ],
        capability_snapshot=snapshot,
        trace_id="trace-001",
        idempotency_key="json-key",
    )
    serialized = command.model_dump_json()
    assert TypedCommand.model_validate_json(serialized) == command
    assert json.loads(serialized)["schema_version"] == 2
    assert json.loads(serialized)["target"]["project_id"] == "p-1"
    assert bus.dispatch(serialized).status == "applied"
    assert (
        bus.dispatch(command.model_copy(update={"command_id": "new-transport-id"})).state_revision
        == 1
    )
    assert bus.state_revision == 1


def test_registry_contract_is_json_and_does_not_trust_a_snapshot() -> None:
    registry = build_runtime_registry()
    for op in registry.list_operations():
        descriptor = registry.describe(op)
        assert op in descriptor.operations
        assert descriptor.operation_schema_version == registry.get_spec(op).schema_version
        assert descriptor.operation_schema["additionalProperties"] is False
        assert descriptor.available
        assert descriptor.execution_modes == ("local",)
        assert descriptor.pack_provider == (descriptor.required_packs or ("nagar.core",))[0]
        assert "project:read" in descriptor.required_permissions or "project:write" in (
            descriptor.required_permissions
        )
        assert json.loads(descriptor.model_dump_json())["capability_id"] == descriptor.capability_id
    assert "project:write" in registry.describe("system.undo").required_permissions


# Parse / envelope schema: bad bytes, missing identity/provenance, invalid versions,
# wrong shapes and unbounded/unsafe metadata fail BEFORE authorization or handler.
@pytest.mark.parametrize(
    ("modify", "expected"),
    [
        (lambda data: data.update(schema_version=999), "schema_version"),
        (lambda data: data.update(protocol_version="nagar.command.v3"), "protocol_version"),
        (lambda data: data.pop("actor"), "actor"),
        (lambda data: data.pop("provenance"), "provenance"),
        (lambda data: data.update(provenance={"source": "agent"}), "source_id"),
        (lambda data: data.update(target={}), "project_id"),
        (lambda data: data.update(execution_policy={"mode": "shell"}), "execution_policy"),
        (lambda data: data.update(execution_policy={"network_access": True}), "network_access"),
        (lambda data: data.update(shell_command="rm -rf /"), "extra_forbidden"),
        (
            lambda data: data.update(
                input_refs=[
                    {
                        "ref_type": "file",
                        "project_id": "p-1",
                        "ref_id": "source",
                    }
                ]
            ),
            "ref_type",
        ),
        (
            lambda data: data.update(
                input_refs=[
                    {
                        "ref_type": "asset",
                        "project_id": "p-1",
                        "ref_id": "../other",
                    }
                ]
            ),
            "reference id",
        ),
        (
            lambda data: data.update(
                input_refs=[
                    {
                        "ref_type": "asset",
                        "project_id": "p-1",
                        "ref_id": "https://example.test/a",
                    }
                ]
            ),
            "reference id",
        ),
        (
            lambda data: data.update(
                input_refs=[{"ref_type": "asset", "project_id": "p-1", "ref_id": "%2e%2e%2fsecret"}]
            ),
            "reference id",
        ),
        (
            lambda data: data.update(
                input_refs=[{"ref_type": "asset", "project_id": "p-1", "ref_id": "file:///secret"}]
            ),
            "reference id",
        ),
        (
            lambda data: data.update(
                input_refs=[
                    {
                        "ref_type": "asset",
                        "project_id": "p-1",
                        "ref_id": "source",
                        "metadata": {"uri": "file:///secret"},
                    }
                ]
            ),
            "extra_forbidden",
        ),
        (
            lambda data: data.update(
                input_refs=[
                    {
                        "ref_type": "asset",
                        "project_id": "p-1",
                        "ref_id": "source",
                        "metadata": {"content_sha256": "x" * 1000},
                    }
                ]
            ),
            "content_sha256",
        ),
    ],
    ids=[
        "unsupported-envelope-version",
        "unsupported-protocol",
        "missing-actor",
        "missing-provenance",
        "incomplete-provenance",
        "missing-project",
        "shell-mode",
        "network-policy",
        "raw-shell-field",
        "unknown-reference-type",
        "path-traversal",
        "external-url",
        "encoded-traversal",
        "file-url",
        "unsafe-metadata",
        "oversized-metadata",
    ],
)
def test_envelope_schema_refuses_before_execution(modify: Any, expected: str) -> None:
    bus, calls = _spy_bus()
    data = _command(bus)
    modify(data)
    with pytest.raises(CommandValidationError, match=expected):
        bus.dispatch(data)
    assert calls == []
    assert bus.state_revision == 0


@pytest.mark.parametrize(
    "raw", [b"\xff", "{garbage", "[]", '{"a": 1, "a": 2}', {"input": {"bad": object()}}, 42, "NaN"]
)
def test_parse_refuses_invalid_serialization(raw: Any) -> None:
    bus, calls = _spy_bus()
    with pytest.raises(CommandValidationError, match="invalid command envelope"):
        bus.dispatch(raw)
    assert calls == []


def test_tampered_typed_object_cannot_bypass_parse_validation() -> None:
    bus, calls = _spy_bus()
    command = TypedCommand.model_validate(_command(bus))
    tampered = command.model_copy(update={"actor": None})  # model_copy does NOT validate
    with pytest.raises(CommandValidationError):
        bus.dispatch(tampered)
    assert calls == []


def test_unknown_operation_and_unsupported_operation_schema_fail_at_schema() -> None:
    bus, calls = _spy_bus()
    with pytest.raises(UnknownOperationError):
        bus.dispatch(_command(bus, operation="system.run_shell"))
    with pytest.raises(CommandValidationError, match="unsupported operation schema_version"):
        bus.dispatch(_command(bus, operation_schema_version=9))
    with pytest.raises(CommandValidationError, match="invalid input"):
        bus.dispatch(_command(bus, input={"at": "start", "label": "one", "ui_click": True}))
    assert calls == []


# Actor / project authorization BEFORE capability/policy/refs/idempotency.
def test_actor_project_and_operation_permissions_are_trusted_grants() -> None:
    project = _project()
    bus, calls = _spy_bus(project)
    with pytest.raises(AuthorizationError, match="not authorized"):
        bus.dispatch(_command(bus, actor={"kind": "user", "actor_id": "attacker"}))
    with pytest.raises(AuthorizationError, match="different project"):
        bus.dispatch(_command(bus, target={"project_id": "other"}))

    wrong_owner = CommandBus(
        project,
        registry=build_wave1_registry(),
        authorizer=ProjectAccess(
            actor=TEST_ACTOR, project_id="other", permissions=frozenset({"project:write"})
        ),
    )
    with pytest.raises(AuthorizationError, match="not authorized"):
        wrong_owner.dispatch(_command(bus))
    with pytest.raises(AuthorizationError, match="no trusted project authorizer"):
        CommandBus(project).dispatch(_command(bus))

    read_only = CommandBus(
        project,
        registry=build_wave1_registry(),
        authorizer=ProjectAccess(
            actor=TEST_ACTOR, project_id="p-1", permissions=frozenset({"project:read"})
        ),
    )
    with pytest.raises(AuthorizationError, match="project permissions"):
        read_only.dispatch(_command(bus))
    assert read_only.dispatch(_command(bus, operation="media.pause", input={})).status == "applied"
    with pytest.raises(AuthorizationError, match="project permissions"):
        read_only.dispatch(_command(bus, operation="system.undo", input={}))
    assert calls == []
    assert bus.state_revision == 0


# Authoritative capability/version/availability check; snapshot cannot install a capability.
def test_capability_missing_unknown_incompatible_or_forged_refuses() -> None:
    bus, calls = _spy_bus()
    snapshot = bus._registry.describe("timeline.mark").snapshot().model_dump(mode="json")
    with pytest.raises(UnknownCapabilityError):
        bus.dispatch(_command(bus, capability_snapshot=snapshot | {"capability_id": "new.cap"}))
    with pytest.raises(CapabilityVersionError):
        bus.dispatch(_command(bus, capability_snapshot=snapshot | {"version": "2.0.0"}))
    with pytest.raises(CapabilityVersionError):
        bus.dispatch(_command(bus, capability_snapshot=snapshot | {"version": "1.1.0"}))
    with pytest.raises(CapabilityVersionError):
        bus.dispatch(_command(bus, capability_snapshot=snapshot | {"operation_schema_version": 2}))
    with pytest.raises(CapabilityError, match="forged provider"):
        bus.dispatch(_command(bus, capability_snapshot=snapshot | {"pack_provider": "evil.pack"}))
    with pytest.raises(CapabilityError, match="does not match"):
        bus.dispatch(_command(bus, capability_snapshot=snapshot | {"operation": "media.play"}))
    bus._registry.get_capability("timeline.marking").available = False
    with pytest.raises(CapabilityError, match="unavailable"):
        bus.dispatch(_command(bus))
    assert calls == []


def test_policy_refuses_unsupported_mode_and_level_c_or_d() -> None:
    bus, calls = _spy_bus()
    with pytest.raises(ExecutionPolicyError, match="unavailable execution mode"):
        bus.dispatch(_command(bus, execution_policy={"mode": "preview"}))
    registry = CapabilityRegistry()
    spec = bus._registry.get_spec("timeline.mark")
    for suffix, level in (("c", PermissionLevel.CONFIRMATION), ("d", PermissionLevel.DENIED)):
        registry.register_operation(
            "timeline",
            suffix,
            replace(spec, operation_id=f"timeline.{suffix}", permission_level=level),
        )
    policy_bus = authorized_bus(_project(), registry)
    for operation, confirmed in (("timeline.c", False), ("timeline.d", True)):
        with pytest.raises(ExecutionPolicyError, match="level [CD]"):
            policy_bus.dispatch(_command(policy_bus, operation=operation, confirmed=confirmed))
    assert calls == []
    assert policy_bus.state_revision == 0


# Asset/clip/timeline existence, ownership and typed metadata are validated
# against Project state, not against the client's reference or file path.
@pytest.mark.parametrize(
    ("ref", "message"),
    [
        ({"ref_type": "asset", "ref_id": "missing", "project_id": "p-1"}, "not registered"),
        ({"ref_type": "asset", "ref_id": "source", "project_id": "p-2"}, "crosses"),
        ({"ref_type": "clip", "ref_id": "missing", "project_id": "p-1"}, "not in"),
        ({"ref_type": "timeline", "ref_id": "wrong", "project_id": "p-1"}, "not in"),
        (
            {
                "ref_type": "asset",
                "ref_id": "source",
                "project_id": "p-1",
                "metadata": {"media_kind": "image"},
            },
            "wrong media kind",
        ),
        (
            {
                "ref_type": "asset",
                "ref_id": "source",
                "project_id": "p-1",
                "metadata": {"content_sha256": "sha256:" + "ff" * 32},
            },
            "wrong content digest",
        ),
        (
            {
                "ref_type": "timeline",
                "ref_id": "timeline-1",
                "project_id": "p-1",
                "metadata": {"media_kind": "video"},
            },
            "cannot carry",
        ),
    ],
    ids=[
        "nonexistent-asset",
        "cross-project-asset",
        "nonexistent-clip",
        "wrong-timeline",
        "wrong-kind",
        "wrong-hash",
        "forged-timeline-metadata",
    ],
)
def test_input_reference_boundary_refuses_before_reservation(
    ref: dict[str, object], message: str
) -> None:
    bus, calls = _spy_bus()
    with pytest.raises(InputReferenceError, match=message):
        bus.dispatch(_command(bus, input_refs=[ref]))
    assert calls == []
    assert bus.state_revision == 0
    assert bus._idempotency == {}


def test_every_ref_is_checked_even_after_a_valid_one() -> None:
    bus, calls = _spy_bus()
    refs = [
        {"ref_type": "asset", "project_id": "p-1", "ref_id": "source"},
        {"ref_type": "asset", "project_id": "p-1", "ref_id": "other-projects-asset"},
    ]
    with pytest.raises(InputReferenceError, match="not registered"):
        bus.dispatch(_command(bus, input_refs=refs))
    assert calls == []
    assert bus._idempotency == {}


def test_valid_clip_and_timeline_refs_and_temporal_bounds() -> None:
    bus, calls = _spy_bus()
    data = _command(
        bus,
        input_refs=[
            {"ref_type": "clip", "project_id": "p-1", "ref_id": "clip-1"},
            {"ref_type": "timeline", "project_id": "p-1", "ref_id": "timeline-1"},
        ],
    )
    assert bus.dispatch(data).output == {"calls": 1}
    assert calls == ["cmd-1"]
    from nexus_ai_agent.creative.studio.models import ReferenceResolutionError

    with pytest.raises(ReferenceResolutionError, match="outside project bounds"):
        bus.dispatch(
            _command(
                bus,
                input={
                    "at": {
                        "kind": "absolute",
                        "timecode_us": 6_000_000,
                    },
                    "label": "outside",
                },
                idempotency_key="different-key",
            )
        )
    assert calls == ["cmd-1"]


def test_idempotency_replays_original_result_and_conflict_cannot_overwrite() -> None:
    bus = authorized_bus(_project())
    data = _command(bus)
    first = bus.dispatch(data)
    assert first.status == "applied"
    assert first.state_revision == 1
    assert len(bus.project.timeline.markers) == 1
    first.output["marker_id"] = "mutated-response"  # caller cannot corrupt cached result

    replay = bus.dispatch(data | {"command_id": "second-http-attempt", "trace_id": "trace-2"})
    assert replay.transaction_id == first.transaction_id
    assert replay.state_hash == first.state_hash
    assert replay.output["marker_id"] == bus.project.timeline.markers[0].marker_id
    assert bus.state_revision == 1
    with pytest.raises(IdempotencyConflictError, match="different payload"):
        bus.dispatch(data | {"input": {"at": "start", "label": "changed"}})
    # Authorization must precede even a cached replay. Other *authorized*
    # semantic changes collide with the reservation instead of overwriting it.
    with pytest.raises(AuthorizationError, match="not authorized"):
        bus.dispatch(data | {"actor": {"kind": "service", "actor_id": "attacker"}})
    with pytest.raises(IdempotencyConflictError, match="different payload"):
        bus.dispatch(data | {"confirmed": True})
    assert len(bus.project.timeline.markers) == 1


def test_key_scope_is_project_and_operation_not_global() -> None:
    bus = authorized_bus(_project())
    bus.dispatch(_command(bus))
    assert bus.dispatch(_command(bus, operation="media.pause", input={})).status == "applied"
    other = authorized_bus(_project("p-2"))
    assert other.dispatch(_command(other)).state_revision == 1
    assert bus.state_revision == 2


def test_concurrent_redelivery_reserves_before_handler_once() -> None:
    bus, calls = _spy_bus()
    raw = _command(bus)
    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(
            pool.map(bus.dispatch, [raw | {"command_id": f"retry-{n}"} for n in range(32)])
        )
    assert len(calls) == 1
    assert len({r.transaction_id for r in outcomes}) == 1
    assert bus.state_revision == 1


def test_failed_execution_releases_reservation_and_does_not_mutate_project() -> None:
    project = _project()
    original_hash = project.state_hash
    calls = 0

    def bad(state: Project, _context: OperationContext) -> OperationOutcome:
        nonlocal calls
        calls += 1
        state.timeline.markers.clear()
        raise RuntimeError("failed after mutating the input snapshot")

    registry = CapabilityRegistry()
    registry.register_operation(
        "timeline",
        "marking",
        OperationSpec(
            operation_id="timeline.mark",
            description="spy",
            permission_level=PermissionLevel.REVERSIBLE,
            input_model=MarkCommandInput,
            handler=bad,
            reference_fields=("at",),
        ),
    )
    bus = authorized_bus(project, registry)
    for _ in range(2):
        with pytest.raises(CommandExecutionError, match="failed"):
            bus.dispatch(_command(bus))
    assert calls == 2
    assert bus.state_revision == 0
    assert bus.state_hash == original_hash
    assert bus._idempotency == {}
