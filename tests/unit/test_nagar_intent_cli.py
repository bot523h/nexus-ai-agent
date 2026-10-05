"""The ``nexus intent`` operator command: free text -> durable queue -> artifact.

Drives the real Typer app (the exact CLI wiring an operator hits).  Only the
*external model* is substituted, through the production composition root; the
real queue, worker, registry, CommandBus and FFmpeg lane run for real.

* With no model configured the command reports ``clarification_required`` and
  enqueues **nothing** (the Model Kill Test, observable from the CLI).
* With a model it persists exactly one durable job whose existing worker renders
  a real artifact that the registered verifier independently confirms.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from nexus_ai_agent.cli import app
from nexus_ai_agent.config import settings as settings_module
from nexus_ai_agent.creative.slideshow.ffmpeg import FfmpegUnavailableError, resolve_ffmpeg_bin
from nexus_ai_agent.nagar.composition import ProviderStatus


class ScriptedProvider:
    """A declared test double for the external model seam only."""

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, prompt: str, *, idempotency_key: str | None = None) -> str:
        self.calls += 1
        return json.dumps(
            {
                "schema_id": "nagar.gateway.proposal.v1",
                "schema_version": 1,
                "operation": "timeline.trim",
                "input": {"clip_asset_id": "src", "in_point_us": 0, "out_point_us": 1_000_000},
                "rationale": "scripted",
                "confidence": 1.0,
            }
        )


@pytest.fixture()
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("NEXUS_DB_PATH", str(tmp_path / "app.sqlite"))
    monkeypatch.setenv("CREATIVE_TEMP_DIR", str(tmp_path / "creative_tmp"))
    (tmp_path / "creative_tmp").mkdir(parents=True)
    monkeypatch.delenv("NEXUS_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    settings_module.get_settings.cache_clear()
    return tmp_path


def _clip(path: Path, seconds: float = 2.0) -> None:
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
            f"testsrc=duration={seconds}:size=320x240:rate=15",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={seconds}",
            "-pix_fmt",
            "yuv420p",
            "-y",
            str(path),
        ],
        check=True,
        timeout=120,
    )


def test_cli_intent_with_no_model_reports_clarification_and_enqueues_nothing(
    cli_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "nexus_ai_agent.nagar.composition.build_cognition_provider",
        lambda settings=None: (None, ProviderStatus.NOT_CONFIGURED),
    )
    result = CliRunner().invoke(app, ["intent", "trim from 1s to 8s", "--source", "unused.mp4"])
    assert result.exit_code == 0, result.output
    assert "status: clarification_required" in result.output
    assert "status: enqueued" not in result.output
    # Nothing was persisted: the durable queue holds zero jobs.
    from nexus_ai_agent.adapters.in_process_job_queue import InProcessJobQueue
    from nexus_ai_agent.worker import job_queue_db_path

    db = job_queue_db_path(settings_module.get_settings().db_path)
    assert InProcessJobQueue(db).job_ids() == []


def test_cli_intent_with_model_produces_a_verified_artifact(
    cli_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = ScriptedProvider()
    monkeypatch.setattr(
        "nexus_ai_agent.nagar.composition.build_cognition_provider",
        lambda settings=None: (provider, ProviderStatus.AVAILABLE),
    )
    source = cli_env / "real_source.mp4"
    _clip(source)

    result = CliRunner().invoke(
        app,
        [
            "intent",
            "trim the first second",
            "--source",
            str(source),
            "--duration-us",
            "2000000",
        ],
    )
    assert result.exit_code == 0, result.output
    assert provider.calls == 1
    assert "status: enqueued" in result.output
    assert "operation: timeline.trim" in result.output
    assert "durable_status: completed" in result.output
    assert "verified: True" in result.output
    assert "sha256: sha256:" in result.output
