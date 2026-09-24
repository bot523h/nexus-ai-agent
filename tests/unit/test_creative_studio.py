from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import aiosqlite
import httpx
import pytest
from fastapi.testclient import TestClient

from nexus_ai_agent.api import app as app_module
from nexus_ai_agent.config import settings as settings_module
from nexus_ai_agent.creative.ffmpeg_executor import FFmpegResult, execute_ffmpeg_commands
from nexus_ai_agent.creative.job_registry import JobRegistry
from nexus_ai_agent.creative.video_director import (
    Caption,
    Cut,
    VideoEditPlan,
    Zoom,
    analyze_video_with_gemini,
)


@pytest.mark.asyncio
async def test_video_director_calls_gemini(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    response_payload = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {
                            "text": json.dumps(
                                {
                                    "cuts": [{"start": 0.0, "end": 1.5}],
                                    "zooms": [],
                                    "captions": [{"start": 0.0, "end": 1.5, "text": "Hook"}],
                                    "reasoning": "Focus on the strongest opening seconds.",
                                }
                            )
                        }
                    ]
                }
            }
        ]
    }

    async def fake_post(
        self: httpx.AsyncClient,
        url: str,
        *,
        json: dict[str, Any],
    ) -> httpx.Response:
        captured["url"] = url
        captured["json"] = json
        return httpx.Response(200, json=response_payload)

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    plan = await analyze_video_with_gemini("/tmp/input.mp4", "creative-key")

    assert plan.reasoning == "Focus on the strongest opening seconds."
    assert plan.cuts[0].end == 1.5
    assert captured["json"]["generationConfig"]["temperature"] == 0
    assert captured["json"]["generationConfig"]["responseMimeType"] == "application/json"
    assert "creative-key" in captured["url"]
    assert "generateContent" in captured["url"]


@pytest.mark.asyncio
async def test_ffmpeg_executor_builds_correct_command(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    commands: list[list[str]] = []
    input_path = tmp_path / "input.mp4"
    output_path = tmp_path / "output.mp4"
    input_path.write_bytes(b"fake-video")
    plan = VideoEditPlan(
        cuts=[Cut(start=1.0, end=2.5)],
        zooms=[Zoom(start=1.0, end=2.0, scale=1.5)],
        captions=[Caption(start=1.0, end=2.0, text="Caption")],
        reasoning="Trim the strongest moment.",
    )

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        destination = Path(command[-1])
        if destination.suffix == ".mp4":
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"generated")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = await execute_ffmpeg_commands(plan, str(input_path), str(output_path))

    assert result.success is True
    assert result.output_path == str(output_path)
    assert result.duration == 1.5
    assert any("-ss" in command for command in commands)
    assert any("concat" in " ".join(command) for command in commands)
    filter_commands = [command for command in commands if "-vf" in command]
    assert filter_commands
    assert "zoompan" in filter_commands[0][filter_commands[0].index("-vf") + 1]
    assert "drawtext" in filter_commands[0][filter_commands[0].index("-vf") + 1]


@pytest.mark.asyncio
async def test_job_registry_crud(tmp_path: Path) -> None:
    registry = JobRegistry(tmp_path / "creative_jobs.sqlite3")
    job_id = await registry.create_job("video_edit", {"path": "/tmp/input.mp4"})

    created = await registry.get_job(job_id)
    assert created is not None
    assert created["status"] == "pending"
    assert created["input_data"]["path"] == "/tmp/input.mp4"

    await registry.update_job_status(
        job_id,
        "done",
        result={"output_path": "/tmp/output.mp4"},
    )
    updated = await registry.get_job(job_id)
    assert updated is not None
    assert updated["status"] == "done"
    assert updated["result"]["output_path"] == "/tmp/output.mp4"

    async with aiosqlite.connect(tmp_path / "creative_jobs.sqlite3") as db:
        await db.execute(
            """
            UPDATE creative_jobs
            SET created_at = datetime('now', '-10 day'),
                updated_at = datetime('now', '-10 day')
            WHERE id = ?
            """,
            (job_id,),
        )
        await db.commit()

    await registry.cleanup_old_jobs(days=7)
    assert await registry.get_job(job_id) is None


def test_background_task_flow(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import hashlib
    import hmac as hmac_module
    import time

    monkeypatch.setenv("NEXUS_CREATIVE_GEMINI_API_KEY", "creative-key")
    monkeypatch.setenv("CREATIVE_TEMP_DIR", str(tmp_path / "creative"))
    # The endpoint is fail-closed: without a signing key it answers 503.
    monkeypatch.setenv("NEXUS_API_HMAC_KEY", "test-signing-key")
    settings_module.get_settings.cache_clear()
    monkeypatch.setattr(app_module, "_creative_registry", None)

    async def fake_analyze(video_path_or_url: str, api_key: str) -> VideoEditPlan:
        assert api_key == "creative-key"
        assert Path(video_path_or_url).suffix == ".mp4"
        return VideoEditPlan(
            cuts=[Cut(start=0.0, end=1.0)],
            zooms=[],
            captions=[],
            reasoning="Keep the opening beat.",
        )

    async def fake_execute(
        plan: VideoEditPlan,
        input_path: str,
        output_path: str,
    ) -> FFmpegResult:
        assert plan.reasoning == "Keep the opening beat."
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_bytes(b"output")
        return FFmpegResult(
            success=True,
            output_path=output_path,
            error_message=None,
            duration=1.0,
        )

    monkeypatch.setattr(app_module, "analyze_video_with_gemini", fake_analyze)
    monkeypatch.setattr(app_module, "execute_ffmpeg_commands", fake_execute)

    client = TestClient(app_module.app)
    # Build the multipart body once so the HMAC signature covers exactly
    # the raw bytes the server will receive.
    request = httpx.Request(
        "POST",
        "http://testserver/creative/video-edit",
        files={"file": ("clip.mp4", b"video-bytes", "video/mp4")},
    )
    request.read()  # materialize the streaming multipart body
    timestamp = str(int(time.time()))
    signature = hmac_module.new(
        b"test-signing-key",
        f"{timestamp}:".encode() + request.content,
        hashlib.sha256,
    ).hexdigest()
    response = client.post(
        "/creative/video-edit",
        content=request.content,
        headers={
            "Content-Type": request.headers["Content-Type"],
            "X-NEXUS-Timestamp": timestamp,
            "X-NEXUS-Signature": signature,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "pending"

    # GET is the same HMAC gate as the POST (task-165): timestamp over an
    # empty body, constant-time verified by the app.
    read_timestamp = str(int(time.time()))
    read_signature = hmac_module.new(
        b"test-signing-key",
        f"{read_timestamp}:".encode() + b"",
        hashlib.sha256,
    ).hexdigest()
    job_response = client.get(
        f"/creative/jobs/{payload['job_id']}",
        headers={
            "X-NEXUS-Timestamp": read_timestamp,
            "X-NEXUS-Signature": read_signature,
        },
    )
    assert job_response.status_code == 200
    job = job_response.json()
    assert job["status"] == "done"
    assert job["result"]["output_path"].endswith(f"{payload['job_id']}.mp4")
    assert not Path(job["input_data"]["path"]).exists()
