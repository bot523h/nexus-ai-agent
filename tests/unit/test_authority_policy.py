from __future__ import annotations

from typing import Any

from nexus_ai_agent.creative.packs.slideshow.models import AssetEvidence
from nexus_ai_agent.creative.render_jobs import _dispatch as dispatch_render_job
from nexus_ai_agent.creative.slideshow import service as slideshow_service
from nexus_ai_agent.creative.slideshow import upscale as slideshow_upscale
from nexus_ai_agent.creative.slideshow.ffmpeg import UpscaleArtifact
from nexus_ai_agent.creative.slideshow.service import PlanningRequest
from nexus_ai_agent.creative.studio.authorization import ProjectAccess
from nexus_ai_agent.creative.studio.bus import CommandBus
from nexus_ai_agent.creative.studio.models import (
    ActorIdentity,
    AssetRecord,
    Timeline,
    TypedCommand,
    new_project,
)


def test_slideshow_service_dispatches_with_project_scoped_service_authority(
    monkeypatch: Any,
    slideshow_media: Any,
) -> None:
    buses: list[tuple[CommandBus, list[TypedCommand]]] = []

    class ObservedCommandBus(CommandBus):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.observed_commands: list[TypedCommand] = []
            super().__init__(*args, **kwargs)
            buses.append((self, self.observed_commands))

        def dispatch(self, command: Any) -> Any:
            typed = (
                command
                if isinstance(command, TypedCommand)
                else TypedCommand.model_validate(command)
            )
            self.observed_commands.append(typed)
            return super().dispatch(typed)

    monkeypatch.setattr(slideshow_service, "CommandBus", ObservedCommandBus)
    request = PlanningRequest(
        images=tuple(slideshow_media.images[:2]),
        target_duration_us=30_000_000,
        mode="manual",
        shot_seconds=(15.0, 15.0),
    )

    outcome = slideshow_service.plan_from_files(request)

    assert outcome.commands == (
        "slideshow.scan_assets",
        "slideshow.suggest_tone",
        "slideshow.compose",
    )
    assert len(buses) == 1
    bus, commands = buses[0]
    assert isinstance(bus._authorizer, ProjectAccess)
    assert bus._authorizer.actor == ActorIdentity(
        kind="service", actor_id="nexus.slideshow.service"
    )
    assert bus._authorizer.project_id == bus.project.project_id
    assert bus._authorizer.permissions == frozenset({"project:read", "project:write"})
    assert [command.operation for command in commands] == list(outcome.commands)
    assert all(command.actor == bus._authorizer.actor for command in commands)
    assert all(command.schema_version == 2 for command in commands)
    assert all(command.target.project_id == bus.project.project_id for command in commands)
    assert all(command.provenance is not None for command in commands)


def test_render_worker_dispatches_through_the_same_project_authorization_boundary(
    monkeypatch: Any,
) -> None:
    import nexus_ai_agent.creative.studio.bus as bus_module

    project = new_project(
        "worker-project",
        "Worker authority test",
        Timeline(timeline_id="timeline", duration_us=1_000_000),
    )
    project = project.model_copy(
        update={
            "assets": [
                AssetRecord(
                    asset_id="src",
                    media_kind="video",
                    content_sha256="sha256:" + "ab" * 32,
                    duration_us=1_000_000,
                    parent_asset_ids=(),
                    provenance={"origin": "authority-test"},
                )
            ]
        }
    )
    observations: list[dict[str, Any]] = []
    real_bus = bus_module.CommandBus

    class ObservedCommandBus(real_bus):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            record: dict[str, Any] = {"authorizer": kwargs.get("authorizer"), "commands": []}
            observations.append(record)
            self._authority_observation = record
            super().__init__(*args, **kwargs)

        def dispatch(self, command: Any) -> Any:
            typed = (
                command
                if isinstance(command, TypedCommand)
                else TypedCommand.model_validate(command)
            )
            self._authority_observation["commands"].append(typed)
            return super().dispatch(typed)

    monkeypatch.setattr(bus_module, "CommandBus", ObservedCommandBus)
    result = dispatch_render_job(
        project,
        operation="delivery.make_proxy_480p",
        input_data={"video_asset_id": "src", "output_asset_id": "proxy"},
        idempotency_key="authority-test",
    )

    assert result["asset_id"] == "proxy"
    assert len(observations) == 1
    authorizer = observations[0]["authorizer"]
    assert isinstance(authorizer, ProjectAccess)
    assert authorizer.actor == ActorIdentity(
        kind="service", actor_id="nexus.creative.render-worker"
    )
    assert authorizer.project_id == project.project_id
    assert authorizer.permissions == frozenset({"project:read"})
    [command] = observations[0]["commands"]
    assert command.actor == authorizer.actor
    assert command.schema_version == 2
    assert command.target.project_id == project.project_id
    assert command.provenance is not None


def test_upscale_root_dispatches_with_narrow_project_scoped_authority(
    monkeypatch: Any,
    tmp_path: Any,
) -> None:
    import nexus_ai_agent.creative.studio.bus as bus_module

    input_path = tmp_path / "source.png"
    output_path = tmp_path / "upscaled.png"
    source = AssetEvidence(
        evidence_id="source-image",
        path=str(input_path),
        content_sha256="sha256:" + "11" * 32,
        media_kind="image",
        width=8,
        height=6,
    )
    measured = AssetEvidence(
        evidence_id="upscaled-image",
        path=str(output_path),
        content_sha256="sha256:" + "22" * 32,
        media_kind="image",
        width=16,
        height=12,
    )
    monkeypatch.setattr(
        slideshow_upscale,
        "probe_image",
        lambda path: source if str(path) == str(input_path) else measured,
    )
    monkeypatch.setattr(
        slideshow_upscale,
        "upscale_image",
        lambda *_args, **_kwargs: UpscaleArtifact(
            path=str(output_path),
            sha256=measured.content_sha256,
            size_bytes=128,
            width=16,
            height=12,
            binary="test-ffmpeg",
        ),
    )

    observations: list[dict[str, Any]] = []
    real_bus = bus_module.CommandBus

    class ObservedCommandBus(real_bus):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            record = {"authorizer": kwargs.get("authorizer"), "commands": []}
            observations.append(record)
            self._authority_observation = record
            super().__init__(*args, **kwargs)

        def dispatch(self, command: Any) -> Any:
            typed = (
                command
                if isinstance(command, TypedCommand)
                else TypedCommand.model_validate(command)
            )
            self._authority_observation["commands"].append(typed)
            return super().dispatch(typed)

    monkeypatch.setattr(slideshow_upscale, "CommandBus", ObservedCommandBus)
    result = slideshow_upscale.upscale_from_file(input_path, output_path, scale_factor=2.0)

    assert result.asset_id.startswith("derived_")
    assert len(observations) == 1
    authorizer = observations[0]["authorizer"]
    assert isinstance(authorizer, ProjectAccess)
    assert authorizer.actor == ActorIdentity(kind="service", actor_id="nexus.slideshow.upscale")
    assert authorizer.project_id
    assert authorizer.permissions == frozenset({"project:write"})
    commands = observations[0]["commands"]
    assert [command.operation for command in commands] == [
        "slideshow.scan_assets",
        "slideshow.upscale",
    ]
    assert all(command.actor == authorizer.actor for command in commands)
    assert all(command.schema_version == 2 for command in commands)
    assert all(command.target.project_id == authorizer.project_id for command in commands)
    assert all(command.provenance is not None for command in commands)
