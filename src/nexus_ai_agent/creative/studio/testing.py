"""Shared test fixtures and helpers for creative studio tests."""

from __future__ import annotations

from typing import Any

from nexus_ai_agent.creative.studio.authorization import ProjectAccess, ProjectAuthorizer
from nexus_ai_agent.creative.studio.models import (
    ActorIdentity,
    Clip,
    CommandProvenance,
    MediaRef,
    Playhead,
    Preconditions,
    Project,
    TimeBase,
    Timeline,
    TimeRangeUS,
    Track,
    TypedCommand,
    new_project,
)

TIMEBASE = TimeBase(numerator=30, denominator=1)
DIGEST = "sha256:" + "ab" * 32
DEFAULT_STUDIO_ACTOR = ActorIdentity(kind="user", actor_id="alice")


class _DefaultProjectAuthorizer:
    def __init__(self, project_id: str) -> None:
        self._project_id = project_id

    def authorize(self, actor: ActorIdentity, project_id: str) -> ProjectAccess:
        return ProjectAccess(
            actor=actor,
            project_id=self._project_id,
            permissions=frozenset({"project:read", "project:write"}),
        )


def default_authorizer(project_id: str = "project_01") -> ProjectAuthorizer:
    return _DefaultProjectAuthorizer(project_id)


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


def make_command(
    operation: str = "media.play",
    command_id: str = "cmd_01",
    *,
    input: dict[str, Any] | None = None,  # noqa: A002
    schema_version: int = 1,
    actor: ActorIdentity | None = DEFAULT_STUDIO_ACTOR,
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
