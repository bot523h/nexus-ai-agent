"""WAVE 30 — tests for the capstone Olympus master render.

The end-to-end test executes a real FFmpeg encode when both binaries are on PATH
and is skipped otherwise. The BLOCKED path is exercised with a monkeypatched
``ffmpeg_available`` (a mock, justified: it cannot be produced with a working
install). The CPU-only stages always run for real.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nexus_ai_agent.creative.atlas import media_sine as media
from nexus_ai_agent.creative.atlas import olympus_master as om
from nexus_ai_agent.creative.atlas.showrunner_bible import (
    Character,
    Episode,
    Prop,
    ShowrunnerBible,
)
from nexus_ai_agent.tools.filesystem_policy import WorkspaceFilesystem

HAS_FFMPEG = media.ffmpeg_available()


@pytest.fixture()
def workspace(tmp_path: Path) -> WorkspaceFilesystem:
    return WorkspaceFilesystem(tmp_path)


def _bible() -> ShowrunnerBible:
    b = ShowrunnerBible()
    b.add_character(Character("a", "Alice"))
    b.add_character(Character("b", "Bob"))
    b.add_prop(Prop("sword", "Sword"))
    b.add_episode(Episode(1, "Pilot", transfers=(("sword", "a"),)))
    b.add_episode(Episode(2, "Fall", deaths=("a",), transfers=(("sword", "b"),)))
    return b


def test_render_master_runs_cpu_stages(workspace: WorkspaceFilesystem) -> None:
    ledger = om.render_master(_bible(), workspace, side=32, frames=6, fps=6)
    names = {s.name: s.status for s in ledger.stages}
    assert names["continuity"] == "COMPUTED"
    assert names["lighting_sdf"] == "COMPUTED"
    assert names["ocean_spectrum"] == "COMPUTED"
    assert names["pyro_blackbody"] == "COMPUTED"
    assert names["diegetic_audio"] == "COMPUTED"
    assert names["grade_frames"] == "COMPUTED"
    assert names["continuity_artifact"] == "COMPUTED"


def test_render_master_is_deterministic(workspace: WorkspaceFilesystem) -> None:
    a = om.render_master(_bible(), workspace, side=32, frames=4, fps=4)
    b = om.render_master(_bible(), workspace, side=32, frames=4, fps=4)
    assert a.canonical_bytes() == b.canonical_bytes()
    assert a.ledger_sha256 == b.ledger_sha256


def test_render_master_ledger_digest_changes_with_input(workspace: WorkspaceFilesystem) -> None:
    clean = om.render_master(_bible(), workspace, side=32, frames=4, fps=4)
    b = ShowrunnerBible()
    b.add_character(Character("a", "Alice"))
    b.add_episode(Episode(1, "Only"))
    other = om.render_master(b, workspace, side=32, frames=4, fps=4)
    assert clean.ledger_sha256 != other.ledger_sha256


def test_render_master_writes_real_artifacts(workspace: WorkspaceFilesystem) -> None:
    om.render_master(_bible(), workspace, side=32, frames=4, fps=4)
    assert (workspace.root / "olympus_master" / "master_audio.wav").is_file()
    assert (workspace.root / "olympus_master" / "continuity.json").is_file()
    assert (workspace.root / "olympus_master" / "frames" / "frame_0000.raw").is_file()


def test_render_master_audio_is_valid_wav(workspace: WorkspaceFilesystem) -> None:
    import wave

    om.render_master(_bible(), workspace, side=32, frames=8, fps=8)
    with wave.open(str(workspace.root / "olympus_master" / "master_audio.wav"), "rb") as h:
        assert h.getnchannels() == 1
        assert h.getsampwidth() == 2
        assert h.getframerate() == 48000
        assert h.getnframes() == 48000


def test_render_master_continuity_artifact_is_json(workspace: WorkspaceFilesystem) -> None:
    om.render_master(_bible(), workspace, side=32, frames=4, fps=4)
    payload = json.loads((workspace.root / "olympus_master" / "continuity.json").read_text())
    assert "findings" in payload


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg/ffprobe not on PATH")
def test_render_master_encodes_real_clip(workspace: WorkspaceFilesystem) -> None:
    ledger = om.render_master(_bible(), workspace, side=32, frames=6, fps=6)
    names = {s.name: s for s in ledger.stages}
    assert names["encode_clip"].status == "COMPUTED"
    assert (workspace.root / "olympus_master" / "master_clip.mp4").is_file()
    probe = media.probe_media(workspace, "olympus_master/master_clip.mp4")
    assert probe.nb_frames == 6


def test_render_master_reports_blocked_without_ffmpeg(
    workspace: WorkspaceFilesystem, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(om.media, "ffmpeg_available", lambda: False)
    ledger = om.render_master(_bible(), workspace, side=32, frames=4, fps=4)
    names = {s.name: s for s in ledger.stages}
    assert names["encode_clip"].status == "BLOCKED"
    assert ledger.blocked == 1
    # CPU stages still ran for real.
    assert names["continuity"].status == "COMPUTED"


def test_render_master_reports_blocked_on_media_error(
    workspace: WorkspaceFilesystem, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(*a: object, **k: object) -> object:
        raise media.MediaSineError("synthetic encode failure")

    monkeypatch.setattr(om.media, "ffmpeg_available", lambda: True)
    monkeypatch.setattr(om.media, "synthesise_test_clip", _boom)
    ledger = om.render_master(_bible(), workspace, side=32, frames=4, fps=4)
    names = {s.name: s for s in ledger.stages}
    assert names["encode_clip"].status == "BLOCKED"


def test_persist_ledger_round_trips(workspace: WorkspaceFilesystem) -> None:
    ledger = om.render_master(_bible(), workspace, side=32, frames=4, fps=4)
    digest = om.persist_ledger(workspace, ledger, "olympus_master/ledger.json")
    reopened = workspace.read_bytes("olympus_master/ledger.json", max_bytes=1 << 20)
    assert digest == om._digest(reopened)
    assert reopened == ledger.canonical_bytes()


def test_persist_ledger_detects_tampering(workspace: WorkspaceFilesystem) -> None:
    ledger = om.render_master(_bible(), workspace, side=32, frames=4, fps=4)
    workspace.write_bytes("olympus_master/ledger.json", b"tampered", create_parents=True)
    payload = ledger.canonical_bytes()
    workspace.write_bytes("olympus_master/ledger.json", payload, create_parents=True)
    digest = om.persist_ledger(workspace, ledger, "olympus_master/ledger.json")
    assert digest == om._digest(payload)


# --- adversarial / refusal --------------------------------------------------------


def test_refuse_bad_side(workspace: WorkspaceFilesystem) -> None:
    with pytest.raises(om.MasterError):
        om.render_master(_bible(), workspace, side=4)
    with pytest.raises(om.MasterError):
        om.render_master(_bible(), workspace, side=om.MAX_SIDE + 1)


def test_refuse_bad_frames(workspace: WorkspaceFilesystem) -> None:
    with pytest.raises(om.MasterError):
        om.render_master(_bible(), workspace, frames=0)
    with pytest.raises(om.MasterError):
        om.render_master(_bible(), workspace, frames=om.MAX_FRAMES + 1)


def test_refuse_unsafe_out_dir(workspace: WorkspaceFilesystem) -> None:
    with pytest.raises(om.MasterError):
        om.render_master(_bible(), workspace, out_dir="/etc")
    with pytest.raises(om.MasterError):
        om.render_master(_bible(), workspace, out_dir="../escape")
    with pytest.raises(om.MasterError):
        om.render_master(_bible(), workspace, out_dir="")


def test_refuse_persist_tampered_bytes(workspace: WorkspaceFilesystem) -> None:
    ledger = om.render_master(_bible(), workspace, side=32, frames=4, fps=4)
    # Make the write produce different bytes than canonical by patching read.
    original = workspace.read_bytes

    def _bad_read(raw: str, *, max_bytes: int | None = None) -> bytes:
        return original(raw, max_bytes=max_bytes) + b"x"

    workspace.read_bytes = _bad_read  # type: ignore[method-assign]
    with pytest.raises(om.MasterError):
        om.persist_ledger(workspace, ledger, "olympus_master/ledger.json")


def test_render_master_detects_continuity_tamper(
    workspace: WorkspaceFilesystem, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = workspace.read_bytes

    def _corrupt(raw: str, *, max_bytes: int | None = None) -> bytes:
        data = original(raw, max_bytes=max_bytes)
        if raw.endswith("continuity.json"):
            return data + b"corrupt"
        return data

    monkeypatch.setattr(workspace, "read_bytes", _corrupt)
    with pytest.raises(om.MasterError):
        om.render_master(_bible(), workspace, side=32, frames=4, fps=4)
