"""``creative_render`` handler contract — the trust boundary of the creative chain.

Every test here drives :func:`creative_render_job` with a *hand-built* payload,
i.e. exactly what a corrupted queue row or a CLI drain would produce.  The
handler must never render from an arbitrary path, never accept an operation with
no lane primitive, and never report success for an artifact that failed
verification (owner directive §7/§8).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from nexus_ai_agent.adapters.creative_render_job import (
    CREATIVE_RENDER_JOB_TYPE,
    CreativeRenderError,
    CreativeRenderPayload,
    creative_render_job,
    verify_lane_artifact,
)
from nexus_ai_agent.creative.rendering.executor import LaneArtifact
from nexus_ai_agent.creative.slideshow import ffmpeg as ffmpeg_module

FAKE_BYTES = b"\x00\x00\x00\x18ftypmp42verified-master"


class _Info:
    """Stand-in for the canonical probe result (source and master differ)."""

    def __init__(self, duration_us: int = 4_000_000) -> None:
        self.duration_us = duration_us
        self.width = 1920
        self.height = 1080
        self.has_audio = True


def _probe_by_name(path: Path, *, binary: str) -> _Info:
    return _Info(2_000_000 if Path(path).name == "master.mp4" else 4_000_000)


@pytest.fixture()
def creative_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    from nexus_ai_agent.config import settings as settings_module

    root = tmp_path / "creative"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CREATIVE_TEMP_DIR", str(root))
    settings_module.get_settings.cache_clear()
    yield root
    settings_module.get_settings.cache_clear()


def _workspace(root: Path, name: str = "creative_deadbeef") -> Path:
    workspace = root / name
    workspace.mkdir(parents=True, exist_ok=True)
    return workspace


def _payload(workspace: Path, **overrides: Any) -> dict[str, object]:
    source = workspace / "source.mp4"
    if not source.exists():
        source.write_bytes(b"source-bytes")
    payload: dict[str, object] = {
        "command": "edit",
        "operation": "trim",
        "args": ["0", "2"],
        "workspace_dir": str(workspace),
        "input_path": str(source),
        "media_duration_us": 4_000_000,
        "user_id": 5,
        "chat_id": 9,
        "lang": "en",
        "idempotency_key": "creative:5:9:1:edit:trim",
    }
    payload.update(overrides)
    return payload


def _artifact(
    path: Path, *, duration_us: int = 2_000_000, sha256: str | None = None
) -> LaneArtifact:
    digest = sha256 or ffmpeg_module.sha256_file(path)
    return LaneArtifact(
        path=str(path),
        sha256=digest,
        size_bytes=path.stat().st_size,
        duration_us=duration_us,
        width=1280,
        height=720,
        has_audio=True,
        lane_ir_hash="sha256:deadbeef",
        ops=("trim",),
        binary="ffmpeg",
        encoder={"codec": "libx264"},
    )


@pytest.fixture()
def _fake_engine(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace the lane + probes: this suite never spawns a process."""
    seen: dict[str, Any] = {}

    monkeypatch.setattr(ffmpeg_module, "resolve_ffmpeg_bin", lambda explicit=None: "ffmpeg")

    def _probe(path: Path, *, binary: str) -> Any:
        seen.setdefault("probes", []).append(Path(path).name)
        return _probe_by_name(path, binary=binary)

    monkeypatch.setattr(ffmpeg_module, "probe_video", _probe)

    def _render(lane: Any, output_path: Path, **kwargs: Any) -> LaneArtifact:
        seen["lane"] = lane
        seen["kwargs"] = kwargs
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_bytes(FAKE_BYTES)
        artifact = _artifact(Path(output_path))
        seen["artifact"] = artifact
        return artifact

    import nexus_ai_agent.creative.rendering as rendering_pkg

    monkeypatch.setattr(rendering_pkg, "render_lane", _render)
    return seen


# ---------------------------------------------------------------------------
# payload + containment
# ---------------------------------------------------------------------------


async def test_rejects_unknown_payload_fields(creative_root: Path) -> None:
    workspace = _workspace(creative_root)
    payload = _payload(workspace, backdoor="rm -rf /")
    with pytest.raises(CreativeRenderError) as excinfo:
        await creative_render_job(payload)
    assert excinfo.value.code == "invalid_request"


async def test_rejects_workspace_outside_the_creative_root(
    creative_root: Path, tmp_path: Path
) -> None:
    outsider = tmp_path / "elsewhere" / "creative_evil"
    outsider.mkdir(parents=True)
    (outsider / "source.mp4").write_bytes(b"x")
    payload = _payload(outsider)
    with pytest.raises(CreativeRenderError) as excinfo:
        await creative_render_job(payload)
    assert excinfo.value.code == "invalid_request"


async def test_rejects_workspace_without_the_reserved_prefix(creative_root: Path) -> None:
    workspace = _workspace(creative_root, "not_reserved")
    payload = _payload(workspace)
    with pytest.raises(CreativeRenderError) as excinfo:
        await creative_render_job(payload)
    assert excinfo.value.code == "invalid_request"


async def test_rejects_input_outside_the_workspace(creative_root: Path, tmp_path: Path) -> None:
    workspace = _workspace(creative_root)
    victim = tmp_path / "victim.mp4"
    victim.write_bytes(b"not yours")
    payload = _payload(workspace, input_path=str(victim))
    with pytest.raises(CreativeRenderError) as excinfo:
        await creative_render_job(payload)
    assert excinfo.value.code == "invalid_request"
    assert victim.read_bytes() == b"not yours"


async def test_missing_source_is_reported_typed(creative_root: Path) -> None:
    workspace = _workspace(creative_root)
    payload = _payload(workspace, input_path=str(workspace / "gone.mp4"))
    with pytest.raises(CreativeRenderError) as excinfo:
        await creative_render_job(payload)
    assert excinfo.value.code == "media_missing"


async def test_operation_without_a_lane_primitive_is_refused(creative_root: Path) -> None:
    workspace = _workspace(creative_root)
    for command, operation in (("caption", "burnin"), ("grade", "lut"), ("grade", "proxy")):
        payload = _payload(workspace, command=command, operation=operation, args=[])
        with pytest.raises(CreativeRenderError) as excinfo:
            await creative_render_job(payload)
        assert excinfo.value.code == "unsupported_operation", (command, operation)


async def test_trim_arguments_are_validated(creative_root: Path) -> None:
    workspace = _workspace(creative_root)
    for args in (["nope", "2"], ["5", "1"]):
        payload = _payload(workspace, args=args)
        with pytest.raises(CreativeRenderError) as excinfo:
            await creative_render_job(payload)
        assert excinfo.value.code == "invalid_request"


# ---------------------------------------------------------------------------
# the happy path — canonical lane + verified artifact
# ---------------------------------------------------------------------------


async def test_success_goes_through_the_canonical_lane_and_verified_artifact(
    creative_root: Path, _fake_engine: dict[str, Any]
) -> None:
    workspace = _workspace(creative_root)
    result = await creative_render_job(_payload(workspace))

    lane = _fake_engine["lane"]
    assert lane.main.path == str(workspace / "source.mp4")
    assert lane.main.duration_us == 4_000_000
    assert [op.op for op in lane.ops] == ["trim"]
    assert _fake_engine["kwargs"]["binary"] == "ffmpeg"

    assert result["success"] is True
    assert result["verified"] is True
    assert result["canonical_operation"] == "timeline.trim"
    assert result["output_path"] == str(workspace / "master.mp4")
    assert result["output_sha256"] == ffmpeg_module.sha256_file(workspace / "master.mp4")
    assert result["size_bytes"] == len(FAKE_BYTES)
    assert result["ops"] == ["trim"]


async def test_trim_is_clamped_to_the_probed_duration(
    creative_root: Path, _fake_engine: dict[str, Any]
) -> None:
    workspace = _workspace(creative_root)
    await creative_render_job(_payload(workspace, args=["0", "99"]))
    trim = _fake_engine["lane"].ops[0]
    assert trim.out_us == 4_000_000


async def test_exposure_maps_to_its_lane_twin(
    creative_root: Path, _fake_engine: dict[str, Any]
) -> None:
    workspace = _workspace(creative_root)
    result = await creative_render_job(
        _payload(workspace, command="grade", operation="exposure", args=["1.5"])
    )
    op = _fake_engine["lane"].ops[0]
    assert op.op == "exposure" and op.exposure_ev == 1.5
    assert result["canonical_operation"] == "color.adjust_exposure"


# ---------------------------------------------------------------------------
# no fake success
# ---------------------------------------------------------------------------


class _Deleted:
    """A renderer that claims success but leaves nothing behind."""

    def __init__(self, artifact: LaneArtifact) -> None:
        self._artifact = artifact

    def __call__(self, lane: Any, output_path: Path, **kwargs: Any) -> Any:
        self._artifact.path = str(output_path)  # type: ignore[misc]
        return self._artifact


async def test_artifact_that_does_not_exist_fails_the_job(
    creative_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(creative_root)
    monkeypatch.setattr(ffmpeg_module, "resolve_ffmpeg_bin", lambda explicit=None: "ffmpeg")
    monkeypatch.setattr(ffmpeg_module, "probe_video", _probe_by_name)
    import nexus_ai_agent.creative.rendering as rendering_pkg

    ghost = LaneArtifact(
        path=str(workspace / "master.mp4"),
        sha256="sha256:" + "00" * 32,
        size_bytes=10,
        duration_us=1_000_000,
        width=1,
        height=1,
        has_audio=False,
        lane_ir_hash="sha256:x",
        ops=(),
        binary="ffmpeg",
        encoder={},
    )
    monkeypatch.setattr(rendering_pkg, "render_lane", lambda lane, out, **kw: ghost)

    with pytest.raises(CreativeRenderError) as excinfo:
        await creative_render_job(_payload(workspace))
    assert excinfo.value.code == "artifact_verification_failed"


async def test_digest_mismatch_fails_the_job(
    creative_root: Path, _fake_engine: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(creative_root)

    def _render(lane: Any, output_path: Path, **kwargs: Any) -> LaneArtifact:
        Path(output_path).write_bytes(FAKE_BYTES)
        return LaneArtifact(
            path=str(output_path),
            sha256="sha256:" + "ab" * 32,  # not the digest of FAKE_BYTES
            size_bytes=len(FAKE_BYTES),
            duration_us=2_000_000,
            width=1280,
            height=720,
            has_audio=True,
            lane_ir_hash="sha256:deadbeef",
            ops=("trim",),
            binary="ffmpeg",
            encoder={},
        )

    import nexus_ai_agent.creative.rendering as rendering_pkg

    monkeypatch.setattr(rendering_pkg, "render_lane", _render)
    with pytest.raises(CreativeRenderError) as excinfo:
        await creative_render_job(_payload(workspace))
    assert excinfo.value.code == "artifact_verification_failed"


async def test_verify_lane_artifact_rejects_a_zero_byte_master(
    creative_root: Path, tmp_path: Path
) -> None:
    empty = tmp_path / "master.mp4"
    empty.write_bytes(b"")
    artifact = LaneArtifact(
        path=str(empty),
        sha256=ffmpeg_module.sha256_file(empty),
        size_bytes=0,
        duration_us=1_000,
        width=None,
        height=None,
        has_audio=False,
        lane_ir_hash="sha256:x",
        ops=(),
        binary="ffmpeg",
        encoder={},
    )
    with pytest.raises(CreativeRenderError) as excinfo:
        verify_lane_artifact(
            artifact=artifact, output_path=empty, workspace=tmp_path, binary="ffmpeg"
        )
    assert excinfo.value.code == "artifact_verification_failed"


async def test_verify_lane_artifact_rejects_a_duration_that_does_not_probe_back(
    creative_root: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    master = tmp_path / "master.mp4"
    master.write_bytes(FAKE_BYTES)
    artifact = _artifact(master, duration_us=9_999_999)
    monkeypatch.setattr(ffmpeg_module, "probe_video", _probe_by_name)
    with pytest.raises(CreativeRenderError) as excinfo:
        verify_lane_artifact(
            artifact=artifact, output_path=master, workspace=tmp_path, binary="ffmpeg"
        )
    assert excinfo.value.code == "artifact_verification_failed"


# ---------------------------------------------------------------------------
# dependency / engine failures are typed
# ---------------------------------------------------------------------------


async def test_missing_ffmpeg_is_reported_as_a_dependency_failure(
    creative_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(creative_root)

    def _boom(explicit: str | None = None) -> str:
        raise ffmpeg_module.FfmpegUnavailableError("no binary")

    monkeypatch.setattr(ffmpeg_module, "resolve_ffmpeg_bin", _boom)
    with pytest.raises(CreativeRenderError) as excinfo:
        await creative_render_job(_payload(workspace))
    assert excinfo.value.code == "ffmpeg_unavailable"


async def test_unprobeable_source_is_reported_as_unusable(
    creative_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(creative_root)
    monkeypatch.setattr(ffmpeg_module, "resolve_ffmpeg_bin", lambda explicit=None: "ffmpeg")

    def _boom(path: Path, *, binary: str) -> Any:
        raise ffmpeg_module.RenderError("cannot read the stream table")

    monkeypatch.setattr(ffmpeg_module, "probe_video", _boom)
    with pytest.raises(CreativeRenderError) as excinfo:
        await creative_render_job(_payload(workspace))
    assert excinfo.value.code == "media_unusable"


async def test_engine_timeout_is_reported_as_a_timeout(
    creative_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(creative_root)
    monkeypatch.setattr(ffmpeg_module, "resolve_ffmpeg_bin", lambda explicit=None: "ffmpeg")
    monkeypatch.setattr(ffmpeg_module, "probe_video", _probe_by_name)
    import nexus_ai_agent.creative.rendering as rendering_pkg

    def _timeout(lane: Any, output_path: Path, **kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=1)

    monkeypatch.setattr(rendering_pkg, "render_lane", _timeout)
    with pytest.raises(CreativeRenderError) as excinfo:
        await creative_render_job(_payload(workspace))
    assert excinfo.value.code == "timeout"


async def test_failure_messages_carry_no_absolute_paths(creative_root: Path) -> None:
    workspace = _workspace(creative_root)
    payload = _payload(workspace, input_path=str(workspace / "missing.mp4"))
    with pytest.raises(CreativeRenderError) as excinfo:
        await creative_render_job(payload)
    assert str(creative_root) not in str(excinfo.value)


def test_job_type_constant_is_the_registered_one() -> None:
    from nexus_ai_agent.worker import default_job_handlers

    assert CREATIVE_RENDER_JOB_TYPE == "creative_render"
    assert default_job_handlers()[CREATIVE_RENDER_JOB_TYPE] is creative_render_job


def test_payload_model_forbids_extra_fields() -> None:
    with pytest.raises(ValidationError):
        CreativeRenderPayload.model_validate({"command": "edit", "lol": "nope"})
