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
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers or {}
        return httpx.Response(200, json=response_payload)

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    plan = await analyze_video_with_gemini("/tmp/input.mp4", "creative-key")

    assert plan.reasoning == "Focus on the strongest opening seconds."
    assert plan.cuts[0].end == 1.5
    assert captured["json"]["generationConfig"]["temperature"] == 0
    assert captured["json"]["generationConfig"]["responseMimeType"] == "application/json"
    assert "generateContent" in captured["url"]
    # S4: the API key must ride in the header, never in the URL.
    assert captured["headers"].get("x-goog-api-key") == "creative-key"
    assert "creative-key" not in captured["url"]
    assert "?key=" not in captured["url"]


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


def test_video_edit_retirement_has_no_runtime_side_effects(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import hashlib
    import hmac as hmac_module
    import subprocess
    import time

    from starlette.background import BackgroundTasks

    monkeypatch.setenv("NEXUS_CREATIVE_TEMP_DIR", str(tmp_path))
    monkeypatch.setenv("NEXUS_CREATIVE_GEMINI_API_KEY", "creative-key")
    monkeypatch.setenv("NEXUS_API_HMAC_KEY", "test-signing-key")
    settings_module.get_settings.cache_clear()

    job_creates: list[tuple[str, dict[str, Any]]] = []
    job_updates: list[tuple[Any, ...]] = []
    process_calls: list[tuple[Any, ...]] = []
    analysis_calls: list[tuple[Any, ...]] = []
    ffmpeg_calls: list[tuple[Any, ...]] = []
    background_tasks: list[str] = []
    subprocess_calls: list[list[str]] = []

    class SpyRegistry:
        async def create_job(self, job_type: str, input_data: dict[str, Any]) -> str:
            job_creates.append((job_type, input_data))
            return "job-1"

        async def update_job_status(self, *args: Any, **kwargs: Any) -> None:
            job_updates.append((*args, kwargs))

    monkeypatch.setattr(app_module, "get_creative_registry", lambda: SpyRegistry())

    async def fake_save_upload(upload: Any) -> str:
        path = tmp_path / "uploaded.mp4"
        path.write_bytes(b"video-bytes")
        return str(path)

    async def fake_analyze(video_path: str, api_key: str) -> VideoEditPlan:
        analysis_calls.append((video_path, api_key))
        return VideoEditPlan(
            cuts=[Cut(start=0.0, end=1.0)],
            zooms=[],
            captions=[],
            reasoning="test-only plan",
        )

    async def fake_execute(
        plan: VideoEditPlan,
        input_path: str,
        output_path: str,
    ) -> FFmpegResult:
        ffmpeg_calls.append((plan, input_path, output_path))
        return FFmpegResult(
            success=True,
            output_path=output_path,
            error_message=None,
            duration=1.0,
        )

    async def fake_process(*args: Any, **kwargs: Any) -> None:
        process_calls.append((*args, kwargs))

    monkeypatch.setattr(app_module, "_save_upload_to_temp", fake_save_upload, raising=False)
    monkeypatch.setattr(app_module, "analyze_video_with_gemini", fake_analyze, raising=False)
    monkeypatch.setattr(app_module, "execute_ffmpeg_commands", fake_execute, raising=False)
    monkeypatch.setattr(app_module, "_process_video_edit_job", fake_process, raising=False)

    original_add_task = BackgroundTasks.add_task

    def record_background_task(self: BackgroundTasks, func: Any, *args: Any, **kwargs: Any) -> None:
        background_tasks.append(getattr(func, "__name__", repr(func)))
        original_add_task(self, func, *args, **kwargs)

    monkeypatch.setattr(BackgroundTasks, "add_task", record_background_task)

    def record_subprocess(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        subprocess_calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", record_subprocess)

    client = TestClient(app_module.app)
    request = httpx.Request(
        "POST",
        "http://testserver/creative/video-edit",
        files={"file": ("clip.mp4", b"video-bytes", "video/mp4")},
    )
    request.read()
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

    assert response.status_code == 410
    assert response.json()["detail"]
    assert job_creates == []
    assert job_updates == []
    assert background_tasks == []
    assert process_calls == []
    assert analysis_calls == []
    assert ffmpeg_calls == []
    assert subprocess_calls == []
    assert not (tmp_path / "creative_jobs.sqlite3").exists()
    assert not (tmp_path / "uploaded.mp4").exists()
