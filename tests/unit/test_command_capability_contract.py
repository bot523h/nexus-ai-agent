"""Gate 2 canonical command + capability contract (D-0013).

Behavioural proof for the reconciled contract: versioning/identity, trusted
actor/project authorization, capability authorization, execution policy,
project-scoped reference validation, scoped payload-bound idempotency,
revision preconditions, and the canonical pipeline order. Each class maps to
one section of ``docs/architecture/COMMAND_CAPABILITY_CONTRACT.md``.
"""

from __future__ import annotations

import json
import threading
from typing import Any

import pytest
from pydantic import ValidationError

from nexus_ai_agent.creative.studio import (
    ActorIdentity,
    AuthorizationError,
    CapabilityError,
    CapabilityRegistry,
    CapabilityVersionError,
    Clip,
    CommandBus,
    CommandExecutionError,
    CommandProvenance,
    CommandResult,
    CommandValidationError,
    ExecutionPolicy,
    ExecutionPolicyError,
    IdempotencyConflictError,
    InputRef,
    InputReferenceError,
    MediaRef,
    OperationContext,
    OperationOutcome,
    OperationSpec,
    PermissionDeniedError,
    PermissionLevel,
    Playhead,
    PreconditionError,
    Project,
    ProjectAccess,
    TimeBase,
    Timeline,
    TimeRangeUS,
    Track,
    TypedCommand,
    UnknownCapabilityError,
    UnknownOperationError,
    build_wave1_registry,
    new_project,
)
from nexus_ai_agent.creative.studio.capabilities import UndoCommandInput
from nexus_ai_agent.creative.studio.models import Preconditions, TargetRef

TIMEBASE = TimeBase(numerator=30, denominator=1)
DIGEST = "sha256:" + "ab" * 32


def make_project(project_id: str = "project_01") -> Project:
    media = MediaRef(
        asset_id="asset_01",
        content_sha256=DIGEST,
        media_kind="video",
        duration_us=10_000_000,
        timebase=TIMEBASE,
    )
    clip = Clip(
        clip_id="clip_01",
        media_ref=media,
        source_range=TimeRangeUS(start_us=0, end_us=10_000_000),
        timeline_range=TimeRangeUS(start_us=0, end_us=10_000_000),
    )
    track = Track(track_id="video_01", name="Video 1", kind="video", clips=[clip])
    timeline = Timeline(
        timeline_id="tl_01",
        duration_us=12_000_000,
        tracks=[track],
        playhead=Playhead(timecode_us=2_500_000, frame_number=75, timebase=TIMEBASE),
    )
    project = new_project(project_id, "Contract Demo", timeline)
    return Project.model_validate(
        {
            **project.model_dump(mode="json"),
            "assets": [
                {
                    "asset_id": "asset_01",
                    "media_kind": "video",
                    "content_sha256": DIGEST,
                    "duration_us": 10_000_000,
                    "parent_asset_ids": [],
                    "provenance": {"origin": "test"},
                }
            ],
        }
    )


def make_actor(actor_id: str = "alice") -> ActorIdentity:
    return ActorIdentity(kind="user", actor_id=actor_id)


def make_provenance() -> CommandProvenance:
    return CommandProvenance(source="local", source_id="contract-test")


def make_command(
    operation: str = "media.play",
    command_id: str = "cmd_01",
    *,
    input: dict[str, Any] | None = None,  # noqa: A002 - envelope field name
    schema_version: int = 1,
    actor: ActorIdentity | None = None,
    project_id: str | None = None,
    provenance: CommandProvenance | None = None,
    idempotency_key: str | None = None,
    confirmed: bool = False,
    preconditions: Preconditions | None = None,
    extra: dict[str, Any] | None = None,
) -> TypedCommand:
    payload: dict[str, Any] = {
        "command_id": command_id,
        "operation": operation,
        "schema_version": schema_version,
        "input": input if input is not None else {},
        "confirmed": confirmed,
    }
    if actor is not None:
        payload["actor"] = actor.model_dump(mode="json")
    if project_id is not None:
        payload["target"] = {"project_id": project_id}
    if provenance is not None:
        payload["provenance"] = provenance.model_dump(mode="json")
    if idempotency_key is not None:
        payload["idempotency_key"] = idempotency_key
    if preconditions is not None:
        payload["preconditions"] = preconditions.model_dump(mode="json")
    if extra:
        payload.update(extra)
    return TypedCommand.model_validate(payload)


def make_claimed_command(
    operation: str = "media.play",
    command_id: str = "cmd_01",
    project_id: str = "project_01",
    **kwargs: Any,
) -> TypedCommand:
    """A schema-2 command with the full claim set."""
    return make_command(
        operation,
        command_id,
        schema_version=2,
        actor=kwargs.pop("actor", make_actor()),
        project_id=kwargs.pop("project_id", project_id),
        provenance=kwargs.pop("provenance", make_provenance()),
        **kwargs,
    )


class StaticAuthorizer:
    """Test double: grants are fixed up front, never derived from commands."""

    def __init__(self, *grants: ProjectAccess) -> None:
        self._grants = {(grant.actor, grant.project_id): grant for grant in grants}

    def authorize(self, actor: ActorIdentity, project_id: str) -> ProjectAccess:
        try:
            return self._grants[(actor, project_id)]
        except KeyError:
            raise AuthorizationError("actor is not authorized for this project") from None


def grant(
    actor: ActorIdentity | None = None,
    project_id: str = "project_01",
    permissions: frozenset[str] = frozenset({"project:read", "project:write"}),
) -> ProjectAccess:
    return ProjectAccess(
        actor=actor if actor is not None else make_actor(),
        project_id=project_id,
        permissions=permissions,
    )


@pytest.fixture()
def project() -> Project:
    return make_project()


@pytest.fixture()
def bus(project: Project) -> CommandBus:
    return CommandBus(state=project)


@pytest.fixture()
def authed_bus(project: Project) -> CommandBus:
    return CommandBus(state=project, authorizer=StaticAuthorizer(grant()))


# ---------------------------------------------------------------------------
# A. Versioning and identity (the v1/v2 reconciliation)
# ---------------------------------------------------------------------------


class TestVersioning:
    def test_protocol_v1_is_the_canonical_external_identifier(self, bus: CommandBus) -> None:
        result = bus.dispatch(make_command())
        assert result.diagnostics["protocol_version"] == "nagar.command.v1"

    def test_protocol_v2_value_is_rejected(self, bus: CommandBus) -> None:
        # Gate 2 reconciliation: "nagar.command.v2" was never a protocol.
        # Agent 2's branch invented the identifier without merged code, a PR,
        # or bus integration; the canonical contract refuses it at parse.
        with pytest.raises(CommandValidationError, match="protocol_version"):
            bus.dispatch(
                {
                    "command_id": "c1",
                    "operation": "media.play",
                    "protocol_version": "nagar.command.v2",
                }
            )
        assert bus.state_revision == 0

    def test_agent2_envelope_shape_is_rejected(self, bus: CommandBus) -> None:
        # The parallel envelope's discriminator field is unknown here.
        with pytest.raises(CommandValidationError, match="envelope"):
            bus.dispatch(
                {
                    "command_id": "c1",
                    "operation": "media.play",
                    "envelope_version": "nagar.command.v2",
                }
            )
        with pytest.raises(CommandValidationError, match="envelope"):
            bus.dispatch(
                {
                    "command_id": "c1",
                    "operation_id": "media.play",
                    "capability_id": "media.playback",
                }
            )
        assert bus.state_revision == 0

    def test_schema_version_defaults_to_legacy_one(self) -> None:
        assert make_command().schema_version == 1

    @pytest.mark.parametrize("bad", [0, 3, 99, -1])
    def test_unknown_schema_versions_rejected(self, bus: CommandBus, bad: int) -> None:
        with pytest.raises(CommandValidationError, match="schema_version"):
            bus.dispatch({"command_id": "c1", "operation": "media.play", "schema_version": bad})
        assert bus.state_revision == 0

    def test_schema2_requires_actor_claim(self) -> None:
        with pytest.raises(ValidationError, match="actor"):
            make_command(schema_version=2, project_id="project_01", provenance=make_provenance())

    def test_schema2_requires_project_claim(self) -> None:
        with pytest.raises(ValidationError, match="target.project_id"):
            make_command(schema_version=2, actor=make_actor(), provenance=make_provenance())

    def test_schema2_requires_provenance_claim(self) -> None:
        with pytest.raises(ValidationError, match="provenance"):
            make_command(schema_version=2, actor=make_actor(), project_id="project_01")

    def test_schema2_with_full_claims_parses(self) -> None:
        command = make_claimed_command()
        assert command.schema_version == 2
        assert command.actor == make_actor()

    def test_unknown_operation_schema_version_rejected(self, bus: CommandBus) -> None:
        with pytest.raises(CommandValidationError, match="operation schema_version"):
            bus.dispatch(make_command(extra={"operation_schema_version": 2}))
        assert bus.state_revision == 0

    def test_registry_operation_schema_version_is_authoritative(self) -> None:
        registry = build_wave1_registry()
        for operation in registry.list_operations():
            assert registry.get_spec(operation).schema_version == 1
            assert registry.describe(operation).operation_schema_version == 1


# ---------------------------------------------------------------------------
# B. Actor / project authorization
# ---------------------------------------------------------------------------


class TestAuthorization:
    def test_actor_claim_without_authorizer_is_refused(self, bus: CommandBus) -> None:
        with pytest.raises(AuthorizationError, match="no trusted project authorizer"):
            bus.dispatch(make_claimed_command())
        assert bus.state_revision == 0

    def test_claim_less_legacy_command_uses_implicit_local_trust(self, bus: CommandBus) -> None:
        result = bus.dispatch(make_command())
        assert result.state_revision == 1

    def test_authorizer_requires_an_actor_claim(self, project: Project) -> None:
        authed = CommandBus(state=project, authorizer=StaticAuthorizer(grant()))
        with pytest.raises(AuthorizationError, match="no actor claim"):
            authed.dispatch(make_command())
        assert authed.state_revision == 0

    def test_wrong_actor_is_denied(self, project: Project) -> None:
        authed = CommandBus(state=project, authorizer=StaticAuthorizer(grant()))
        with pytest.raises(AuthorizationError, match="not authorized"):
            authed.dispatch(make_claimed_command(actor=make_actor("mallory")))
        assert authed.state_revision == 0

    def test_target_project_mismatch_is_denied_without_authorizer(self, bus: CommandBus) -> None:
        with pytest.raises(AuthorizationError, match="different project"):
            bus.dispatch(make_command(project_id="project_02"))
        assert bus.state_revision == 0

    def test_target_project_mismatch_is_denied_with_authorizer(
        self, authed_bus: CommandBus
    ) -> None:
        with pytest.raises(AuthorizationError, match="different project"):
            authed_bus.dispatch(make_claimed_command(project_id="project_02"))
        assert authed_bus.state_revision == 0

    def test_matching_project_claim_is_accepted(self, authed_bus: CommandBus) -> None:
        result = authed_bus.dispatch(make_claimed_command())
        assert result.state_revision == 1

    def test_foreign_grant_from_authorizer_is_denied(self, project: Project) -> None:
        class _ConfusedDeputy:
            def authorize(self, actor: ActorIdentity, project_id: str) -> ProjectAccess:
                return grant(make_actor("mallory"), project_id)

        bus = CommandBus(state=project, authorizer=_ConfusedDeputy())  # type: ignore[arg-type]
        with pytest.raises(AuthorizationError, match="another actor or project"):
            bus.dispatch(make_claimed_command())
        assert bus.state_revision == 0

    def test_missing_permissions_are_denied(self, project: Project) -> None:
        read_only = StaticAuthorizer(grant(permissions=frozenset({"project:read"})))
        bus = CommandBus(state=project, authorizer=read_only)
        with pytest.raises(AuthorizationError, match="project permissions"):
            bus.dispatch(make_claimed_command("timeline.mark", input={"at": "اینجا", "label": "x"}))
        assert bus.state_revision == 0
        # ... while a read-level operation passes on the same grant.
        assert bus.dispatch(make_claimed_command("media.play")).state_revision == 1

    def test_undo_requires_write_permission(self, project: Project) -> None:
        full = CommandBus(state=project, authorizer=StaticAuthorizer(grant()))
        full.dispatch(
            make_claimed_command("timeline.mark", "cmd_mark", input={"at": "اینجا", "label": "x"})
        )
        read_only = CommandBus(
            state=full.project,
            authorizer=StaticAuthorizer(grant(permissions=frozenset({"project:read"}))),
        )
        with pytest.raises(AuthorizationError, match="project permissions"):
            read_only.dispatch(make_claimed_command("system.undo", "cmd_undo"))


# ---------------------------------------------------------------------------
# C. Capability authorization
# ---------------------------------------------------------------------------


class TestCapability:
    def test_unavailable_capability_is_denied(self, project: Project) -> None:
        registry = CapabilityRegistry()
        spec = build_wave1_registry().get_spec("media.play")
        registry.register_operation("media", "playback", spec, available=False)
        bus = CommandBus(state=project, registry=registry)
        with pytest.raises(CapabilityError, match="unavailable"):
            bus.dispatch(make_command())
        assert bus.state_revision == 0

    def test_unknown_snapshot_capability_is_denied(self, authed_bus: CommandBus) -> None:
        descriptor = build_wave1_registry().describe("media.play")
        snapshot = descriptor.snapshot().model_copy(update={"capability_id": "media.nope"})
        with pytest.raises(UnknownCapabilityError, match="unknown capability"):
            authed_bus.dispatch(make_claimed_command(extra={"capability_snapshot": snapshot}))
        assert authed_bus.state_revision == 0

    def test_mismatched_snapshot_is_denied(self, authed_bus: CommandBus) -> None:
        # A snapshot taken for another operation/capability must not authorize
        # this command, even when it is otherwise well-formed.
        snapshot = build_wave1_registry().describe("timeline.mark").snapshot()
        with pytest.raises(CapabilityError, match="does not match"):
            authed_bus.dispatch(make_claimed_command(extra={"capability_snapshot": snapshot}))
        assert authed_bus.state_revision == 0

    def test_forged_snapshot_provider_is_denied(self, authed_bus: CommandBus) -> None:
        descriptor = build_wave1_registry().describe("media.play")
        snapshot = descriptor.snapshot().model_copy(update={"pack_provider": "evil.pack"})
        with pytest.raises(CapabilityError, match="forged provider"):
            authed_bus.dispatch(make_claimed_command(extra={"capability_snapshot": snapshot}))
        assert authed_bus.state_revision == 0

    @pytest.mark.parametrize("version", ["2.0.0", "1.1.0"])
    def test_incompatible_snapshot_version_is_denied(
        self, authed_bus: CommandBus, version: str
    ) -> None:
        descriptor = build_wave1_registry().describe("media.play")
        snapshot = descriptor.snapshot().model_copy(update={"version": version})
        with pytest.raises(CapabilityVersionError, match="not compatible"):
            authed_bus.dispatch(make_claimed_command(extra={"capability_snapshot": snapshot}))
        assert authed_bus.state_revision == 0

    def test_older_minor_snapshot_version_is_accepted(self, authed_bus: CommandBus) -> None:
        registry = build_wave1_registry()
        registry.get_capability("media.playback").version = "1.2.0"
        bus = CommandBus(
            state=authed_bus.project,
            registry=registry,
            authorizer=StaticAuthorizer(grant()),
        )
        snapshot = (
            registry.describe("media.play").snapshot().model_copy(update={"version": "1.1.0"})
        )
        result = bus.dispatch(make_claimed_command(extra={"capability_snapshot": snapshot}))
        assert result.state_revision == 1

    def test_snapshot_operation_schema_mismatch_is_denied(self, authed_bus: CommandBus) -> None:
        descriptor = build_wave1_registry().describe("media.play")
        snapshot = descriptor.snapshot().model_copy(update={"operation_schema_version": 7})
        with pytest.raises(CapabilityVersionError, match="operation schema"):
            authed_bus.dispatch(make_claimed_command(extra={"capability_snapshot": snapshot}))
        assert authed_bus.state_revision == 0

    def test_valid_snapshot_dispatches(self, authed_bus: CommandBus) -> None:
        snapshot = build_wave1_registry().describe("media.play").snapshot()
        result = authed_bus.dispatch(make_claimed_command(extra={"capability_snapshot": snapshot}))
        assert result.state_revision == 1

    def test_operation_without_extra_forbid_schema_cannot_register(self) -> None:
        from pydantic import BaseModel

        class _Loose(BaseModel):
            pass

        registry = CapabilityRegistry()
        with pytest.raises(ValueError, match="must forbid extra fields"):
            registry.register_operation(
                "media",
                "loose",
                OperationSpec(
                    operation_id="media.loose",
                    description="loose",
                    permission_level=PermissionLevel.IMMEDIATE,
                    input_model=_Loose,
                    handler=lambda p, c: OperationOutcome(p, c.history, {}),
                ),
            )

    def test_conflicting_capability_version_cannot_register(self) -> None:
        registry = CapabilityRegistry()
        play = build_wave1_registry().get_spec("media.play")
        pause = build_wave1_registry().get_spec("media.pause")
        registry.register_operation("media", "playback", play, capability_version="1.0.0")
        with pytest.raises(ValueError, match="conflicting capability"):
            registry.register_operation("media", "playback", pause, capability_version="2.0.0")

    def test_live_runtime_registry_reconciles_every_operation(self) -> None:
        # Executable capability reconciliation: derived from the live
        # registry builders, never from a hardcoded snapshot list.
        from nexus_ai_agent.creative.packs.runtime import build_runtime_registry

        registry = build_runtime_registry()
        operations = registry.list_operations()
        assert len(operations) > 5
        for wave1_op in (
            "media.play",
            "media.pause",
            "timeline.mark",
            "timeline.split_at_playhead",
            "system.undo",
        ):
            assert wave1_op in operations
        for operation in operations:
            descriptor = registry.describe(operation)
            assert descriptor.operation == operation
            assert descriptor.available is True
            assert (
                descriptor.capability_id
                in {
                    f"{domain}.{cap}"
                    for domain in registry.list_domains()
                    for cap in ("playback", "marking", "splitting", "history")
                }
                or registry.get_capability(descriptor.capability_id) is not None
            )
            spec = registry.get_spec(operation)
            expected_provider = spec.required_packs[0] if spec.required_packs else "nagar.core"
            assert descriptor.pack_provider == expected_provider
            assert operation in descriptor.operations


# ---------------------------------------------------------------------------
# D. Execution policy
# ---------------------------------------------------------------------------


class TestPolicy:
    def test_unadvertised_preview_mode_is_denied(self, bus: CommandBus) -> None:
        command = make_command()
        preview = command.model_copy(update={"execution_policy": ExecutionPolicy(mode="preview")})
        with pytest.raises(ExecutionPolicyError, match="unavailable execution mode"):
            bus.dispatch(preview)
        assert bus.state_revision == 0

    def test_level_d_denial_is_a_permission_error(self, project: Project) -> None:
        registry = build_wave1_registry()

        def _never(_: Project, context: OperationContext) -> OperationOutcome:
            raise AssertionError("must never execute")

        registry.register_operation(
            "system",
            "danger",
            OperationSpec(
                operation_id="system.shell_exec",
                description="shell escape",
                permission_level=PermissionLevel.DENIED,
                input_model=UndoCommandInput,
                handler=_never,
            ),
        )
        bus = CommandBus(state=project, registry=registry)
        with pytest.raises(ExecutionPolicyError):
            bus.dispatch(make_command("system.shell_exec"))
        with pytest.raises(PermissionDeniedError):
            bus.dispatch(make_command("system.shell_exec"))
        assert bus.state_revision == 0

    def test_level_c_requires_confirmation(self, project: Project) -> None:
        registry = CapabilityRegistry()
        registry.register_operation(
            "timeline",
            "export",
            OperationSpec(
                operation_id="timeline.export_master",
                description="heavy export",
                permission_level=PermissionLevel.CONFIRMATION,
                input_model=UndoCommandInput,
                handler=lambda p, c: OperationOutcome(p, c.history, {"ok": True}),
            ),
        )
        bus = CommandBus(state=project, registry=registry)
        with pytest.raises(ExecutionPolicyError, match="confirmation"):
            bus.dispatch(make_command("timeline.export_master"))
        result = bus.dispatch(make_command("timeline.export_master", confirmed=True))
        assert result.output == {"ok": True}

    def test_network_access_cannot_be_requested(self) -> None:
        with pytest.raises(ValidationError):
            ExecutionPolicy(mode="local", network_access=True)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# E. Reference validation
# ---------------------------------------------------------------------------


class TestReferences:
    def test_unknown_asset_ref_is_denied(self, bus: CommandBus) -> None:
        ref = InputRef(ref_type="asset", project_id="project_01", ref_id="ghost")
        command = make_command().model_copy(update={"input_refs": (ref,)})
        with pytest.raises(InputReferenceError, match="not registered"):
            bus.dispatch(command)
        assert bus.state_revision == 0

    def test_cross_project_ref_is_denied(self, bus: CommandBus) -> None:
        ref = InputRef(ref_type="asset", project_id="project_02", ref_id="asset_01")
        command = make_command().model_copy(update={"input_refs": (ref,)})
        with pytest.raises(InputReferenceError, match="project boundary"):
            bus.dispatch(command)
        assert bus.state_revision == 0

    def test_wrong_ref_metadata_is_denied(self, bus: CommandBus) -> None:
        from nexus_ai_agent.creative.studio.models import InputRefMetadata

        kind_ref = InputRef(
            ref_type="asset",
            project_id="project_01",
            ref_id="asset_01",
            metadata=InputRefMetadata(media_kind="audio"),
        )
        with pytest.raises(InputReferenceError, match="wrong media kind"):
            bus.dispatch(make_command().model_copy(update={"input_refs": (kind_ref,)}))
        digest_ref = InputRef(
            ref_type="asset",
            project_id="project_01",
            ref_id="asset_01",
            metadata=InputRefMetadata(content_sha256="sha256:" + "00" * 32),
        )
        with pytest.raises(InputReferenceError, match="wrong content digest"):
            bus.dispatch(make_command().model_copy(update={"input_refs": (digest_ref,)}))
        assert bus.state_revision == 0

    def test_unknown_clip_and_timeline_refs_are_denied(self, bus: CommandBus) -> None:
        clip_ref = InputRef(ref_type="clip", project_id="project_01", ref_id="clip_99")
        with pytest.raises(InputReferenceError, match="not in this project's timeline"):
            bus.dispatch(make_command().model_copy(update={"input_refs": (clip_ref,)}))
        tl_ref = InputRef(ref_type="timeline", project_id="project_01", ref_id="tl_99")
        with pytest.raises(InputReferenceError, match="not in this project"):
            bus.dispatch(make_command().model_copy(update={"input_refs": (tl_ref,)}))
        assert bus.state_revision == 0

    def test_path_and_url_refs_rejected_at_parse(self) -> None:
        for ref_id in ("../secret", "/etc/passwd", "https://x/y", "a%2fb", ".."):
            with pytest.raises(ValidationError):
                InputRef(ref_type="asset", project_id="project_01", ref_id=ref_id)

    def test_valid_refs_dispatch(self, authed_bus: CommandBus) -> None:
        refs = (
            InputRef(ref_type="asset", project_id="project_01", ref_id="asset_01"),
            InputRef(ref_type="clip", project_id="project_01", ref_id="clip_01"),
            InputRef(ref_type="timeline", project_id="project_01", ref_id="tl_01"),
        )
        command = make_claimed_command().model_copy(update={"input_refs": refs})
        assert authed_bus.dispatch(command).state_revision == 1


# ---------------------------------------------------------------------------
# F. Idempotency: same key + same payload = same result; else conflict
# ---------------------------------------------------------------------------


class TestIdempotency:
    def test_same_key_same_payload_replays(self, bus: CommandBus) -> None:
        first = bus.dispatch(
            make_command(
                "timeline.mark",
                "cmd_a",
                input={"at": "اینجا", "label": "a"},
                idempotency_key="key-1",
            )
        )
        second = bus.dispatch(
            make_command(
                "timeline.mark",
                "cmd_b",
                input={"at": "اینجا", "label": "a"},
                idempotency_key="key-1",
            )
        )
        assert first.transaction_id == second.transaction_id
        assert len(bus.project.timeline.markers) == 1
        assert bus.state_revision == 1

    def test_same_key_different_payload_is_a_conflict(self, bus: CommandBus) -> None:
        bus.dispatch(
            make_command(
                "timeline.mark",
                "cmd_a",
                input={"at": "اینجا", "label": "a"},
                idempotency_key="key-1",
            )
        )
        with pytest.raises(IdempotencyConflictError, match="different payload"):
            bus.dispatch(
                make_command(
                    "timeline.mark",
                    "cmd_b",
                    input={"at": "اینجا", "label": "CHANGED"},
                    idempotency_key="key-1",
                )
            )
        assert len(bus.project.timeline.markers) == 1
        assert bus.state_revision == 1

    def test_same_key_different_confirmed_is_a_conflict(self, bus: CommandBus) -> None:
        bus.dispatch(make_command(idempotency_key="key-1"))
        with pytest.raises(IdempotencyConflictError):
            bus.dispatch(
                make_command("media.play", "cmd_b", idempotency_key="key-1", confirmed=True)
            )
        assert bus.state_revision == 1

    def test_key_scope_includes_operation(self, bus: CommandBus) -> None:
        bus.dispatch(make_command("media.play", "cmd_a", idempotency_key="key-1"))
        result = bus.dispatch(make_command("media.pause", "cmd_b", idempotency_key="key-1"))
        assert result.state_revision == 2

    def test_key_scope_includes_project(self) -> None:
        first = CommandBus(state=make_project("project_01"))
        second = CommandBus(state=make_project("project_02"))
        first.dispatch(make_command(idempotency_key="key-1"))
        result = second.dispatch(make_command(idempotency_key="key-1"))
        assert result.state_revision == 1

    def test_transport_ids_may_rotate_on_redelivery(self, bus: CommandBus) -> None:
        first = bus.dispatch(make_command("media.play", "cmd_a", idempotency_key="key-1"))
        rotated = make_command("media.play", "cmd_zzz", idempotency_key="key-1").model_copy(
            update={"trace_id": "trace_retry"}
        )
        second = bus.dispatch(rotated)
        assert first.transaction_id == second.transaction_id
        assert bus.state_revision == 1

    def test_snapshot_is_verified_on_every_attempt(self, authed_bus: CommandBus) -> None:
        valid = build_wave1_registry().describe("media.play").snapshot()
        authed_bus.dispatch(
            make_claimed_command(idempotency_key="key-1", extra={"capability_snapshot": valid})
        )
        # Dropping the advisory hint still replays: it is not the payload.
        replay = authed_bus.dispatch(
            make_claimed_command("media.play", "cmd_b", idempotency_key="key-1")
        )
        assert replay.state_revision == 1
        # ... but a forged hint on retry is revoked instead of replayed.
        forged = valid.model_copy(update={"pack_provider": "evil.pack"})
        with pytest.raises(CapabilityError, match="forged provider"):
            authed_bus.dispatch(
                make_claimed_command(
                    "media.play",
                    "cmd_c",
                    idempotency_key="key-1",
                    extra={"capability_snapshot": forged},
                )
            )

    def test_replay_returns_original_result_after_state_advanced(self, bus: CommandBus) -> None:
        first = bus.dispatch(
            make_command(
                "timeline.mark",
                "cmd_a",
                input={"at": "اینجا", "label": "a"},
                idempotency_key="key-1",
            )
        )
        bus.dispatch(make_command("media.pause", "cmd_other"))
        assert bus.state_revision == 2
        replay = bus.dispatch(
            make_command(
                "timeline.mark",
                "cmd_retry",
                input={"at": "اینجا", "label": "a"},
                idempotency_key="key-1",
            )
        )
        assert replay.transaction_id == first.transaction_id
        assert replay.state_revision == 1
        assert len(bus.project.timeline.markers) == 1
        assert bus.state_revision == 2

    def test_failed_precondition_releases_the_key(self, bus: CommandBus) -> None:
        bus.dispatch(make_command("media.play", "cmd_a"))
        stale = Preconditions(state_revision=0)
        with pytest.raises(PreconditionError):
            bus.dispatch(
                make_command("media.pause", "cmd_b", idempotency_key="key-1", preconditions=stale)
            )
        retry = bus.dispatch(make_command("media.pause", "cmd_c", idempotency_key="key-1"))
        assert retry.state_revision == 2

    def test_failed_handler_releases_the_key(self, project: Project) -> None:
        registry = CapabilityRegistry()
        calls = {"n": 0}

        def _flaky(p: Project, context: OperationContext) -> OperationOutcome:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom")
            return OperationOutcome(p, context.history, {"ok": True})

        registry.register_operation(
            "media",
            "flaky",
            OperationSpec(
                operation_id="media.flaky",
                description="flaky",
                permission_level=PermissionLevel.IMMEDIATE,
                input_model=UndoCommandInput,
                handler=_flaky,
            ),
        )
        bus = CommandBus(state=project, registry=registry)
        with pytest.raises(CommandExecutionError):
            bus.dispatch(make_command("media.flaky", "cmd_a", idempotency_key="key-1"))
        assert bus.state_revision == 0
        retry = bus.dispatch(make_command("media.flaky", "cmd_b", idempotency_key="key-1"))
        assert retry.output == {"ok": True}
        assert bus.state_revision == 1

    def test_concurrent_same_key_applies_once(self, bus: CommandBus) -> None:
        results: list[CommandResult] = []
        errors: list[BaseException] = []

        def _attempt(index: int) -> None:
            try:
                results.append(
                    bus.dispatch(
                        make_command(
                            "timeline.mark",
                            f"cmd_{index}",
                            input={"at": "اینجا", "label": "a"},
                            idempotency_key="key-race",
                        )
                    )
                )
            except BaseException as exc:  # noqa: BLE001 - collected, then asserted
                errors.append(exc)

        threads = [threading.Thread(target=_attempt, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert not errors
        assert len(results) == 8
        assert {result.transaction_id for result in results} == {results[0].transaction_id}
        assert len(bus.project.timeline.markers) == 1
        assert bus.state_revision == 1

    def test_blank_or_empty_keys_rejected_at_parse(self) -> None:
        with pytest.raises(ValidationError):
            make_command(idempotency_key="")
        with pytest.raises(ValidationError):
            make_command(idempotency_key="has space")
        with pytest.raises(ValidationError):
            make_command(idempotency_key="has\ttab")

    def test_oversized_and_non_json_input_rejected_at_parse(self) -> None:
        with pytest.raises(ValidationError, match="512 KiB"):
            make_command(input={"blob": "x" * (513 * 1024)})
        with pytest.raises(ValidationError, match="finite JSON"):
            TypedCommand.model_validate(
                {"command_id": "c1", "operation": "media.play", "input": {"s": {1, 2}}}
            )


# ---------------------------------------------------------------------------
# G. Revision preconditions: old commands cannot apply silently
# ---------------------------------------------------------------------------


class TestRevision:
    def test_stale_revision_rejected(self, bus: CommandBus) -> None:
        bus.dispatch(make_command("media.play", "cmd_a"))
        with pytest.raises(PreconditionError, match="stale state_revision"):
            bus.dispatch(
                make_command("media.pause", "cmd_b", preconditions=Preconditions(state_revision=0))
            )
        assert bus.state_revision == 1

    def test_stale_hash_rejected(self, bus: CommandBus) -> None:
        bus.dispatch(make_command("media.play", "cmd_a"))
        with pytest.raises(PreconditionError, match="stale state_hash"):
            bus.dispatch(
                make_command(
                    "media.pause",
                    "cmd_b",
                    preconditions=Preconditions(state_hash="sha256:deadbeef"),
                )
            )
        assert bus.state_revision == 1

    def test_matching_preconditions_accepted(self, bus: CommandBus) -> None:
        current_hash = bus.state_hash
        result = bus.dispatch(
            make_command(
                "media.play",
                "cmd_a",
                preconditions=Preconditions(state_revision=0, state_hash=current_hash),
            )
        )
        assert result.state_revision == 1

    def test_old_command_cannot_apply_on_new_state(self, bus: CommandBus) -> None:
        before_hash = bus.state_hash
        bus.dispatch(make_command("media.play", "cmd_advance"))
        stale = make_command(
            "timeline.mark",
            "cmd_stale",
            input={"at": "اینجا", "label": "stale"},
            preconditions=Preconditions(state_revision=0, state_hash=before_hash),
        )
        with pytest.raises(PreconditionError):
            bus.dispatch(stale)
        assert bus.state_revision == 1
        assert bus.project.timeline.markers == []

    def test_undo_restores_hash_for_preconditions(self, bus: CommandBus) -> None:
        before_hash = bus.state_hash
        bus.dispatch(make_command("timeline.mark", "cmd_mark", input={"at": "اینجا", "label": "x"}))
        bus.dispatch(make_command("system.undo", "cmd_undo"))
        assert bus.state_hash == before_hash
        result = bus.dispatch(
            make_command(
                "media.play",
                "cmd_after",
                preconditions=Preconditions(state_hash=before_hash),
            )
        )
        assert result.state_revision == 3


# ---------------------------------------------------------------------------
# H. Canonical pipeline order (each probe pins a stage boundary)
# ---------------------------------------------------------------------------


class TestPipelineOrder:
    def test_schema_before_authorization(self, bus: CommandBus) -> None:
        # Unknown operation AND smuggled actor claim: schema wins.
        with pytest.raises(UnknownOperationError):
            bus.dispatch(make_claimed_command("timeline.nope"))
        assert bus.state_revision == 0

    def test_authorization_before_capability(self, project: Project) -> None:
        authed = CommandBus(state=project, authorizer=StaticAuthorizer(grant()))
        forged = (
            build_wave1_registry()
            .describe("media.play")
            .snapshot()
            .model_copy(update={"pack_provider": "evil.pack"})
        )
        with pytest.raises(AuthorizationError):
            authed.dispatch(
                make_claimed_command(
                    actor=make_actor("mallory"),
                    extra={"capability_snapshot": forged},
                )
            )
        assert authed.state_revision == 0

    def test_capability_before_policy(self, project: Project) -> None:
        read_only = CommandBus(
            state=project,
            authorizer=StaticAuthorizer(grant(permissions=frozenset({"project:read"}))),
        )
        preview = make_claimed_command("timeline.mark", input={"at": "اینجا", "label": "x"})
        preview = preview.model_copy(update={"execution_policy": ExecutionPolicy(mode="preview")})
        # Missing write permission (stage 4) beats forbidden mode (stage 5).
        with pytest.raises(AuthorizationError, match="project permissions"):
            read_only.dispatch(preview)
        assert read_only.state_revision == 0

    def test_policy_before_references(self, bus: CommandBus) -> None:
        command = make_command().model_copy(
            update={
                "execution_policy": ExecutionPolicy(mode="preview"),
                "input_refs": (
                    InputRef(ref_type="asset", project_id="project_01", ref_id="ghost"),
                ),
            }
        )
        with pytest.raises(ExecutionPolicyError, match="unavailable execution mode"):
            bus.dispatch(command)
        assert bus.state_revision == 0

    def test_references_before_idempotency(self, bus: CommandBus) -> None:
        bus.dispatch(make_command(idempotency_key="key-1"))
        retry = make_command("media.play", "cmd_b", idempotency_key="key-1").model_copy(
            update={
                "input_refs": (InputRef(ref_type="asset", project_id="project_01", ref_id="ghost"),)
            }
        )
        # Same key but an unresolvable ref: the ref gate fires, not a conflict.
        with pytest.raises(InputReferenceError, match="not registered"):
            bus.dispatch(retry)
        assert bus.state_revision == 1

    def test_idempotency_before_revision(self, bus: CommandBus) -> None:
        bus.dispatch(
            make_command(
                "timeline.mark",
                "cmd_a",
                input={"at": "اینجا", "label": "a"},
                idempotency_key="key-1",
            )
        )
        conflict = make_command(
            "timeline.mark",
            "cmd_b",
            input={"at": "اینجا", "label": "CHANGED"},
            idempotency_key="key-1",
            preconditions=Preconditions(state_revision=999),
        )
        with pytest.raises(IdempotencyConflictError):
            bus.dispatch(conflict)
        assert bus.state_revision == 1

    def test_handler_mutation_cannot_corrupt_central_state(self, project: Project) -> None:
        registry = CapabilityRegistry()
        before_markers = len(project.timeline.markers)

        def _dirty(p: Project, context: OperationContext) -> OperationOutcome:
            p.timeline.markers.append(  # type: ignore[union-attr]
                {"marker_id": "dirty", "timecode_us": 0, "label": "dirty"}  # type: ignore[arg-type]
            )
            raise RuntimeError("blew up after mutating")

        registry.register_operation(
            "media",
            "dirty",
            OperationSpec(
                operation_id="media.dirty",
                description="dirty",
                permission_level=PermissionLevel.IMMEDIATE,
                input_model=UndoCommandInput,
                handler=_dirty,
            ),
        )
        bus = CommandBus(state=project, registry=registry)
        with pytest.raises(CommandExecutionError):
            bus.dispatch(make_command("media.dirty"))
        assert len(bus.project.timeline.markers) == before_markers
        assert bus.state_revision == 0

    def test_handler_cannot_change_project_identity(self, project: Project) -> None:
        registry = CapabilityRegistry()

        def _renaming(p: Project, context: OperationContext) -> OperationOutcome:
            moved = p.model_copy(update={"project_id": "project_02"})
            return OperationOutcome(moved, context.history, {})

        registry.register_operation(
            "media",
            "renaming",
            OperationSpec(
                operation_id="media.renaming",
                description="renaming",
                permission_level=PermissionLevel.IMMEDIATE,
                input_model=UndoCommandInput,
                handler=_renaming,
            ),
        )
        bus = CommandBus(state=project, registry=registry)
        with pytest.raises(CommandExecutionError, match="project identity"):
            bus.dispatch(make_command("media.renaming"))
        assert bus.project.project_id == "project_01"
        assert bus.state_revision == 0


# ---------------------------------------------------------------------------
# I. Parse robustness: every input shape goes through the same gates
# ---------------------------------------------------------------------------


class TestParsing:
    def test_json_text_dispatches(self, bus: CommandBus) -> None:
        payload = json.dumps({"command_id": "c1", "operation": "media.play"})
        assert bus.dispatch(payload).state_revision == 1
        assert bus.dispatch(payload.encode("utf-8")).state_revision == 2

    def test_duplicate_json_keys_rejected(self, bus: CommandBus) -> None:
        with pytest.raises(CommandValidationError, match="duplicate command JSON field"):
            bus.dispatch('{"command_id": "c1", "command_id": "c2", "operation": "media.play"}')
        assert bus.state_revision == 0

    def test_non_finite_json_rejected(self, bus: CommandBus) -> None:
        with pytest.raises(CommandValidationError, match="invalid JSON number"):
            bus.dispatch('{"command_id": "c1", "operation": "media.play", "input": NaN}')
        assert bus.state_revision == 0

    def test_non_object_json_rejected(self, bus: CommandBus) -> None:
        with pytest.raises(CommandValidationError, match="JSON object"):
            bus.dispatch('["media.play"]')
        assert bus.state_revision == 0

    def test_initial_state_snapshot_is_revalidated(self) -> None:
        project = make_project()
        tampered = project.model_copy(update={"state_hash": "sha256:tampered"})
        assert tampered.state_hash == "sha256:tampered"
        bus = CommandBus(state=tampered)
        assert bus.state_hash == project.state_hash

    def test_nested_dispatch_is_forbidden(self, project: Project) -> None:
        registry = CapabilityRegistry()
        holder: dict[str, CommandBus] = {}

        def _reentrant(p: Project, context: OperationContext) -> OperationOutcome:
            holder["bus"].dispatch(make_command("media.play", "cmd_inner"))
            return OperationOutcome(p, context.history, {})  # pragma: no cover

        registry.register_operation(
            "media",
            "reentrant",
            OperationSpec(
                operation_id="media.reentrant",
                description="reentrant",
                permission_level=PermissionLevel.IMMEDIATE,
                input_model=UndoCommandInput,
                handler=_reentrant,
            ),
        )
        bus = CommandBus(state=project, registry=registry)
        holder["bus"] = bus
        with pytest.raises(CommandExecutionError, match="nested command dispatch"):
            bus.dispatch(make_command("media.reentrant"))
        assert bus.state_revision == 0


class TestTargetRefCompatibility:
    def test_legacy_target_without_project_id_parses(self) -> None:
        command = TypedCommand.model_validate(
            {
                "command_id": "c1",
                "operation": "timeline.split_at_playhead",
                "target": {"track_id": "video_01", "clip_id": "clip_01"},
                "input": {"at": "اینجا"},
            }
        )
        assert command.schema_version == 1
        assert command.target.project_id is None

    def test_render_jobs_call_shape_dispatches(self) -> None:
        # creative/render_jobs.py::_dispatch builds exactly this shape with a
        # fresh bus per job and no authorizer; the gate must not break it.
        from nexus_ai_agent.creative.packs.runtime import build_runtime_registry

        project = make_project()
        bus = CommandBus(state=project, registry=build_runtime_registry())
        command = TypedCommand(
            command_id="cmd-key-timeline.trim",
            operation="timeline.mark",
            input={"at": "اینجا", "label": "render"},
            target=TargetRef(project_id=project.project_id, track_id="main"),
            idempotency_key="key-render",
        )
        assert bus.dispatch(command).state_revision == 1
