"""Schema-2 durable trim execution, fail-closed identity and recovery evidence."""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import sqlite3
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

import pytest

from nexus_ai_agent.adapters.in_process_job_queue import InProcessJobQueue
from nexus_ai_agent.adapters.sql_application_directory import (
    DEFAULT_PROJECT_PERMISSIONS,
    SQLApplicationDirectory,
    channel_binding_id,
)
from nexus_ai_agent.application.creative_intent import CanonicalCreativeIntent
from nexus_ai_agent.application.ports.job_queue import JobStatus
from nexus_ai_agent.application.principal import ChannelBinding, PrincipalId
from nexus_ai_agent.config import settings as settings_module
from nexus_ai_agent.config.settings import get_settings
from nexus_ai_agent.creative.render_jobs import CREATIVE_RENDER_JOB_TYPE, creative_render_job
from nexus_ai_agent.creative.slideshow.ffmpeg import FfmpegUnavailableError, resolve_ffmpeg_bin
from nexus_ai_agent.jobs.creative_passport import CreativePassportError
from nexus_ai_agent.storage.migrations import ensure_startup_schema
from nexus_ai_agent.worker import default_job_handlers, job_queue_db_path

PROJECT_ID = "canonical-integration-project"


@pytest.fixture()
def canonical_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("NEXUS_DB_PATH", str(tmp_path / "app.sqlite"))
    monkeypatch.setenv("CREATIVE_TEMP_DIR", str(tmp_path / "creative"))
    monkeypatch.delenv("NEXUS_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    (tmp_path / "creative").mkdir()
    settings_module.get_settings.cache_clear()
    ensure_startup_schema()
    yield tmp_path
    settings_module.get_settings.cache_clear()


def _payload(
    workspace: Path,
    *,
    principal_id: str,
    binding_id: str,
    project_id: str = PROJECT_ID,
    key: str = "canonical:trim:1",
    args: list[str] | None = None,
) -> dict[str, Any]:
    input_data = {"clip_asset_id": "src", "in_point_us": 0, "out_point_us": 1_000_000}
    intent = CanonicalCreativeIntent.create(
        project_id=project_id,
        proposal_id="proposal_" + hashlib.sha256(b"validated-proposal").hexdigest(),
        source_text_sha256="sha256:" + hashlib.sha256(b"trim the first second").hexdigest(),
        operation="timeline.trim",
        input_data=input_data,
    )
    return {
        "command": "edit",
        "operation": "trim",
        "args": args or ["0", "1"],
        "workspace_dir": str(workspace),
        "input_path": str(workspace / "input.mp4"),
        "media_duration_us": 2_000_000,
        "user_id": 0,
        "chat_id": 0,
        "lang": "en",
        "idempotency_key": key,
        "schema_version": 2,
        "principal_id": principal_id,
        "project_id": project_id,
        "channel_binding_id": binding_id,
        "intent": intent.model_dump(mode="json"),
    }


def _clip(path: Path) -> None:
    try:
        binary = resolve_ffmpeg_bin()
    except FfmpegUnavailableError as exc:
        pytest.skip(f"FFmpeg unavailable: {exc}")
    subprocess.run(
        [
            binary,
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=2:size=320x240:rate=15",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=2",
            "-pix_fmt",
            "yuv420p",
            "-y",
            str(path),
        ],
        check=True,
        timeout=120,
    )


@pytest.mark.parametrize(
    "mode",
    ["unknown", "nonmember", "wrong_project", "read_only", "wrong_binding", "diverged_intent"],
)
def test_schema2_refusals_happen_before_any_media_work(
    canonical_env: Path,
    mode: str,
) -> None:
    directory = SQLApplicationDirectory()
    owner_binding = ChannelBinding("cli", "owner")
    other_binding = ChannelBinding("cli", "other")
    owner = directory.resolve_or_create(owner_binding)
    other = directory.resolve_or_create(other_binding)
    if mode not in {"unknown", "nonmember"}:
        directory.ensure_project(
            PROJECT_ID,
            owner,
            permissions=(
                frozenset({"project:read"}) if mode == "read_only" else DEFAULT_PROJECT_PERMISSIONS
            ),
        )
    if mode == "wrong_project":
        directory.ensure_project("another-project", other)

    principal = PrincipalId.new() if mode == "unknown" else owner
    binding_id = channel_binding_id(other_binding if mode == "wrong_binding" else owner_binding)
    project_id = "another-project" if mode == "wrong_project" else PROJECT_ID
    workspace = canonical_env / "creative" / f"creative_{mode}"
    workspace.mkdir()
    (workspace / "input.mp4").write_bytes(b"not-probed")
    payload = _payload(
        workspace,
        principal_id=str(principal),
        binding_id=binding_id,
        project_id=project_id,
        args=["0", "0.5"] if mode == "diverged_intent" else None,
    )

    # Import the renderer before patching the probe: its module holds a
    # probe alias for post-render measurement, which must not capture this
    # test double and pollute the following real-render test.
    from nexus_ai_agent.creative.rendering import executor as _render_executor  # noqa: F401

    probe = Mock(side_effect=AssertionError("media work was reached before authorization"))
    with patch("nexus_ai_agent.creative.slideshow.ffmpeg.probe_video", probe):
        result = asyncio.run(creative_render_job(payload))
    assert result["success"] is False
    assert result["error_code"] == "invalid_request"
    assert "unreadable media" not in str(result.get("error_detail"))
    probe.assert_not_called()


@pytest.mark.asyncio
async def test_schema2_queue_duplicate_delivery_and_reopen_keep_actual_evidence(
    canonical_env: Path,
) -> None:
    directory = SQLApplicationDirectory()
    binding = ChannelBinding("cli", "durable-owner")
    principal = directory.resolve_or_create(binding)
    directory.ensure_project(PROJECT_ID, principal)
    workspace = canonical_env / "creative" / "creative_durable"
    workspace.mkdir()
    _clip(workspace / "input.mp4")
    payload = _payload(
        workspace,
        principal_id=str(principal),
        binding_id=channel_binding_id(binding),
    )

    queue_db = job_queue_db_path(get_settings().db_path)
    queue = InProcessJobQueue(queue_db)
    for job_type, handler in default_job_handlers().items():
        queue.register_handler(job_type, handler)
    job_id = await queue.enqueue(
        job_type=CREATIVE_RENDER_JOB_TYPE,
        idempotency_key=payload["idempotency_key"],
        payload=payload,
    )
    terminal = {JobStatus.COMPLETED, JobStatus.FAILED_RETRYABLE, JobStatus.FAILED_TERMINAL}
    for _ in range(300):
        status = await queue.get_status(job_id)
        if status in terminal:
            break
        await asyncio.sleep(0.1)
    assert status is JobStatus.COMPLETED, await queue.get_result(job_id)

    result = await queue.get_result(job_id)
    assert result is not None
    canonical = result["canonical_execution"]
    assert canonical["command"]["schema_version"] == 2
    assert canonical["command_result"]["status"] == "applied"
    assert canonical["command_result"]["state_revision"] == 1
    assert (
        canonical["edit_transaction"]["transaction_id"]
        == (canonical["command_result"]["transaction_id"])
    )
    assert result["artifact_verification"]["logical_identity"]["project_id"] == PROJECT_ID

    duplicate = await queue.enqueue(
        job_type=CREATIVE_RENDER_JOB_TYPE,
        idempotency_key=payload["idempotency_key"],
        payload=payload,
    )
    assert duplicate == job_id
    history = await queue.get_attempt_history(job_id)
    assert len(history) == 1
    passport = await queue.get_artifact_passport(job_id)
    assert passport["schema_version"] == 2
    assert (
        passport["transaction"]["canonical_commandbus_transaction_id"]
        == (canonical["command_result"]["transaction_id"])
    )
    assert (
        passport["revision"]["canonical_project_state_hash"]
        == (canonical["command_result"]["state_hash"])
    )
    assert passport["artifact"]["storage_key"].endswith("/staging/output.mp4")

    reopened = InProcessJobQueue(queue_db)
    assert await reopened.get_artifact_passport(job_id) == passport
    shutil.rmtree(workspace)
    assert await reopened.get_artifact_passport(job_id) == passport

    with sqlite3.connect(queue_db) as connection:
        row = connection.execute(
            "SELECT attempt_history_json FROM nexus_job_queue WHERE id = ?",
            (job_id,),
        ).fetchone()
        assert row is not None
        history = json.loads(row[0])
        evidence = json.loads(history[0]["execution_evidence_json"])
        evidence["canonical_execution"]["command_result"]["transaction_id"] = "tx_forged"
        history[0]["execution_evidence_json"] = json.dumps(
            evidence,
            sort_keys=True,
            separators=(",", ":"),
        )
        connection.execute(
            "UPDATE nexus_job_queue SET attempt_history_json = ? WHERE id = ?",
            (json.dumps(history, sort_keys=True, separators=(",", ":")), job_id),
        )
    with pytest.raises(CreativePassportError, match="disagree"):
        await reopened.get_artifact_passport(job_id)
    await queue.shutdown()
