"""``nexus analyze`` -- evidence on stdout as JSON, typed failures as exit 1."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from nexus_ai_agent.cli import app
from nexus_ai_agent.creative.packs.perception.models import (
    LoudnessEvidence,
    SceneCutEvidence,
    SilenceEvidence,
)

RUNNER = CliRunner()
FFMPEG = shutil.which("ffmpeg")
pytestmark = pytest.mark.skipif(FFMPEG is None, reason="system ffmpeg required")


def _make(tmp_path: Path, name: str, *args: str) -> Path:
    out = tmp_path / name
    assert FFMPEG is not None
    subprocess.run(  # noqa: S603 - fixed fixture generation
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", *args, str(out)],
        check=True,
        timeout=60,
    )
    return out


def test_silences_loudness_and_scenes_emit_valid_evidence(tmp_path: Path) -> None:
    wav = _make(tmp_path, "t.wav", "-f", "lavfi", "-i", "sine=frequency=440:duration=2")
    video = _make(
        tmp_path, "v.mp4", "-f", "lavfi", "-i", "color=c=red:s=64x64:r=5:d=1", "-pix_fmt", "yuv420p"
    )

    silences = RUNNER.invoke(app, ["analyze", "silences", str(wav), "--min-silence-s", "0.2"])
    assert silences.exit_code == 0, silences.output
    assert SilenceEvidence.model_validate(json.loads(silences.stdout)).spans == ()

    loud = RUNNER.invoke(app, ["analyze", "loudness", str(wav)])
    assert loud.exit_code == 0, loud.output
    assert LoudnessEvidence.model_validate(json.loads(loud.stdout)).integrated_lufs is not None

    scenes = RUNNER.invoke(app, ["analyze", "scenes", str(video), "--threshold", "0.4"])
    assert scenes.exit_code == 0, scenes.output
    assert SceneCutEvidence.model_validate(json.loads(scenes.stdout)).cuts_us == ()


def test_typed_failures_exit_one_with_a_message(tmp_path: Path) -> None:
    result = RUNNER.invoke(app, ["analyze", "loudness", str(tmp_path / "missing.wav")])
    assert result.exit_code == 1
    assert "media not found" in result.output
