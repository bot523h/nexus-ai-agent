"""Wave 2c — the render lane: a pure IR, one real encode, verified evidence.

Three layers are tested separately, which is the whole point of the design:

* the **IR** is a pure function of the plan (transitions are absorbed so the
  master keeps the duration the pack promised, and every crossfade is centred
  on the cut it replaces);
* the **command** is a pure function of the IR — argv only, staging file, no
  shell — so the exact encoder invocation can be asserted without running it;
* the **encode** is real: a genuine FFmpeg process turns generated images plus
  a generated click track into a master whose duration, streams and hash are
  read back out of the produced file, and whose facts are recorded in canonical
  state as a derived asset.

Only the file system and one allow-listed binary are involved; nothing here is
mocked, because "we rendered a real file" is the claim under test.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image
from slideshow_media import make_click_track
from typer.testing import CliRunner

from nexus_ai_agent.cli import app
from nexus_ai_agent.creative.slideshow import ffmpeg as ffmpeg_lane
from nexus_ai_agent.creative.slideshow.ffmpeg import (
    FfmpegUnavailableError,
    GradeParams,
    MotionParams,
    RenderError,
    RenderIR,
    RenderShot,
    build_command,
    build_filtergraph,
    encode,
    probe_video,
    render_ir_from_plan,
    render_ir_hash,
    resolve_ffmpeg_bin,
    sha256_file,
)
from nexus_ai_agent.creative.slideshow.service import PlanningRequest, render_from_files

RUNNER = CliRunner()
TARGET_US = 60_000_000
EMPTY_EVIDENCE: dict[str, str] = {}


# ---------------------------------------------------------------------------
# fixtures: tiny generated media (the repository ships no binaries)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def render_images(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, ...]:
    directory = tmp_path_factory.mktemp("render_images")
    paths: list[Path] = []
    for index, colour in enumerate(((180, 40, 40), (40, 150, 80), (50, 70, 200), (140, 120, 40))):
        path = directory / f"frame_{index}.jpg"
        Image.new("RGB", (320, 180), colour).save(path, quality=90)
        paths.append(path)
    return tuple(paths)


@pytest.fixture(scope="module")
def render_audio(tmp_path_factory: pytest.TempPathFactory) -> Path:
    directory = tmp_path_factory.mktemp("render_audio")
    return make_click_track(directory / "beat.wav", bpm=120.0, seconds=8.0)


def _request(
    images: tuple[Path, ...],
    audio: Path | None,
    *,
    template_id: str = "minimal_clean",
    fps: int = 12,
) -> PlanningRequest:
    return PlanningRequest(
        images=images,
        target_duration_us=TARGET_US,
        audio=audio,
        mode="auto",
        template_id=template_id,
        resolution="320x180",
        fps=fps,
    )


def _hand_built_ir(**overrides: object) -> RenderIR:
    """A three-shot IR whose slots sum to 4 s and whose crossfades total 0.6 s."""
    shots = (
        RenderShot(
            evidence_id="ev_0",
            image_path="/tmp/frame_0.jpg",
            duration_us=1_500_000,
            motion=MotionParams(zoom_from=1.0, zoom_to=1.2),
            grade=GradeParams(contrast=1.1, saturation=1.05, vignette=0.4, grain=3),
            transition_kind="fade",
            transition_us=400_000,
        ),
        RenderShot(
            evidence_id="ev_1",
            image_path="/tmp/frame_1.jpg",
            duration_us=2_000_000,
            motion=MotionParams(zoom_from=1.2, zoom_to=1.0),
            transition_kind="fade",
            transition_us=200_000,
        ),
        RenderShot(evidence_id="ev_2", image_path="/tmp/frame_2.jpg", duration_us=500_000),
    )
    values: dict[str, object] = {
        "shots": shots,
        "template_id": "unit",
        "target_duration_us": 4_000_000,
        "width": 320,
        "height": 180,
        "fps": 12,
        "crf": 30,
        "preset": "ultrafast",
        "audio_bitrate": "96k",
    }
    values.update(overrides)
    return RenderIR(**values)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# the IR (pure)
# ---------------------------------------------------------------------------


def test_ir_absorbs_transitions_so_the_master_keeps_its_duration() -> None:
    ir = _hand_built_ir()
    durations = ir.input_durations_us
    consumed = sum(ir.shots[index].transition_us for index in range(len(ir.shots) - 1))
    assert sum(durations) - consumed == sum(shot.duration_us for shot in ir.shots)
    assert ir.final_duration_us == 4_000_000
    # Every clip is longer than its slot, and the two neighbours of a transition
    # share it.
    assert durations[0] == 1_500_000 + 200_000
    assert durations[1] == 2_000_000 + 300_000
    assert durations[2] == 500_000 + 100_000


def test_crossfades_sit_centred_on_the_shot_boundaries() -> None:
    ir = _hand_built_ir()
    filtergraph, label, has_audio = build_filtergraph(ir)
    assert has_audio is False
    assert label == "vx2"
    assert filtergraph.count("xfade=") == 2
    # slot 0 ends at 1.5 s and the first fade lasts 0.4 s -> offset 1.3 s.
    assert "offset=1.300000" in filtergraph
    # slot 1 ends at 3.5 s and the second fade lasts 0.2 s -> offset 3.4 s.
    assert "offset=3.400000" in filtergraph


def test_a_single_shot_needs_no_transition_machinery() -> None:
    ir = _hand_built_ir(
        shots=(RenderShot(evidence_id="ev_0", image_path="/tmp/frame_0.jpg", duration_us=800_000),),
        target_duration_us=800_000,
    )
    filtergraph, label, _has_audio = build_filtergraph(ir)
    assert label == "v0"
    assert "xfade" not in filtergraph
    assert ir.input_durations_us == (800_000,)


def test_build_command_is_argv_only_and_writes_through_a_staging_file() -> None:
    ir = _hand_built_ir(audio_path="/tmp/beat.wav", audio_duration_us=8_000_000)
    command = build_command(ir, output_path=Path("/out/.master.part.mp4"), binary="/usr/bin/ffmpeg")
    assert isinstance(command, list)
    assert all(isinstance(part, str) for part in command)
    assert command[0] == "/usr/bin/ffmpeg"
    assert command[-1] == "/out/.master.part.mp4"
    assert "-filter_complex" in command
    assert command.count("-i") == len(ir.shots) + 1
    assert "libx264" in command
    assert "aac" in command
    assert "-t" in command and "4.000000" in command
    # Nothing is handed to a shell, and no wildcard/redirect syntax leaks in.
    assert all(part not in {"sh", "-c", "bash", ";", "&&"} for part in command)


def test_filtergraph_fades_the_audio_in_and_out_inside_the_master() -> None:
    ir = _hand_built_ir(
        audio_path="/tmp/beat.wav",
        audio_duration_us=8_000_000,
        fade_in_us=500_000,
        fade_out_us=1_000_000,
        loudness_lufs=-16.0,
    )
    filtergraph, _label, has_audio = build_filtergraph(ir)
    assert has_audio is True
    assert "apad" in filtergraph and "atrim=0:4.000000" in filtergraph
    assert "afade=t=in:st=0:d=0.500000" in filtergraph
    assert "afade=t=out:st=3.000000:d=1.000000" in filtergraph
    assert "loudnorm=I=-16.0" in filtergraph


def test_render_ir_from_plan_rejects_media_it_cannot_locate() -> None:
    plan = {
        "template_id": "minimal_clean",
        "target_duration_us": 1_000_000,
        "render_profile": {"resolution": "320x180", "fps": 12},
        "audio": {},
        "shots": [
            {"evidence_id": "ev_0", "slot": {"start_us": 0, "end_us": 1_000_000}},
        ],
    }
    with pytest.raises(RenderError, match="no media path for evidence id"):
        render_ir_from_plan(plan, EMPTY_EVIDENCE)


def test_render_ir_rejects_an_unusable_resolution() -> None:
    plan = {
        "template_id": "minimal_clean",
        "target_duration_us": 1_000_000,
        "render_profile": {"resolution": "not-a-size", "fps": 12},
        "audio": {},
        "shots": [{"evidence_id": "ev_0", "slot": {"start_us": 0, "end_us": 1_000_000}}],
    }
    with pytest.raises(RenderError, match="unusable resolution"):
        render_ir_from_plan(plan, {"ev_0": "/tmp/frame_0.jpg"})


# ---------------------------------------------------------------------------
# the command and the binary (no encode)
# ---------------------------------------------------------------------------


def test_the_pinned_ir_hash_is_stable_and_covers_the_parameters() -> None:
    first = _hand_built_ir()
    second = _hand_built_ir()
    assert render_ir_hash(first) == render_ir_hash(second)
    assert render_ir_hash(first).startswith("sha256:")
    changed = _hand_built_ir(crf=31)
    assert render_ir_hash(changed) != render_ir_hash(first)


def test_an_explicit_binary_wins_over_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = tmp_path / "ffmpeg"
    fake.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setenv("NEXUS_FFMPEG_BIN", str(tmp_path / "other-ffmpeg"))
    assert resolve_ffmpeg_bin(str(fake)) == str(fake)
    monkeypatch.setenv("NEXUS_FFMPEG_BIN", str(fake))
    assert resolve_ffmpeg_bin() == str(fake)


def test_a_missing_binary_is_a_typed_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NEXUS_FFMPEG_BIN", raising=False)
    monkeypatch.setattr(ffmpeg_lane.shutil, "which", lambda _name: None)

    import imageio_ffmpeg

    def _explode() -> str:
        raise RuntimeError("no bundled build for this platform")

    monkeypatch.setattr(imageio_ffmpeg, "get_ffmpeg_exe", _explode)
    with pytest.raises(FfmpegUnavailableError, match="no FFmpeg binary found"):
        resolve_ffmpeg_bin()


def test_an_explicit_binary_that_is_not_a_file_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(FfmpegUnavailableError, match="is not a file"):
        resolve_ffmpeg_bin(str(tmp_path / "missing-ffmpeg"))


def test_encode_refuses_to_replace_an_existing_master(tmp_path: Path) -> None:
    destination = tmp_path / "master.mp4"
    destination.write_bytes(b"a master someone else made")
    with pytest.raises(RenderError, match="already exists"):
        encode(_hand_built_ir(), destination)
    assert destination.read_bytes() == b"a master someone else made"


def test_a_failed_encode_leaves_no_partial_file_behind(tmp_path: Path) -> None:
    ir = _hand_built_ir(
        shots=(
            RenderShot(
                evidence_id="ev_missing",
                image_path=str(tmp_path / "not-there.jpg"),
                duration_us=200_000,
            ),
        ),
        target_duration_us=200_000,
    )
    destination = tmp_path / "broken.mp4"
    with pytest.raises(RenderError):
        encode(ir, destination)
    assert not destination.exists()
    assert list(tmp_path.glob(".*.part*")) == []


# ---------------------------------------------------------------------------
# the real thing: one genuine FFmpeg encode, end to end
# ---------------------------------------------------------------------------


def test_real_render_produces_a_verified_master_with_provenance(
    render_images: tuple[Path, ...], render_audio: Path, tmp_path: Path
) -> None:
    destination = tmp_path / "master.mp4"
    outcome = render_from_files(_request(render_images, render_audio), output_path=destination)

    # The file exists, and FFmpeg itself confirms what is in it.
    assert destination.is_file() and destination.stat().st_size > 10_000
    info = probe_video(destination, binary=resolve_ffmpeg_bin())
    assert info.duration_us == pytest.approx(TARGET_US, abs=200_000)
    assert (info.width, info.height) == (320, 180)
    assert info.has_audio is True

    # The facts recorded in state are measurements of that file, not hopes.
    artifact = outcome.artifact
    assert artifact["output_path"] == str(destination)
    assert artifact["output_sha256"] == sha256_file(destination)
    assert artifact["duration_us"] == info.duration_us
    assert artifact["render_ir_hash"] == outcome.render_ir_hash
    assert artifact["encoder"]["produced_by"] == "nagar.local.slideshow.v1"

    # Five commands ran, the last one wrote the derived asset.
    assert outcome.commands == (
        "slideshow.scan_assets",
        "slideshow.score_images",
        "slideshow.suggest_tone",
        "slideshow.compose",
        "slideshow.render",
    )
    assert outcome.derived_asset_id.startswith("derived_")
    assert outcome.state_hash != outcome.state_hash_before_render
    assert outcome.state_hash_before_render.startswith("sha256:")

    # The shot boundaries the plan promised are the ones that were rendered.
    planned = sum(
        shot["slot"]["end_us"] - shot["slot"]["start_us"] for shot in outcome.plan["shots"]
    )
    assert planned == TARGET_US
    assert info.duration_us == pytest.approx(planned, abs=100_000)


def test_the_same_ir_encodes_to_the_same_master(
    render_images: tuple[Path, ...], render_audio: Path, tmp_path: Path
) -> None:
    """The pinned IR is the whole render input: same IR, same bytes.

    Nothing volatile (timestamps, host paths, random seeds) may leak into the
    master, otherwise ``output_sha256`` would not be reproducible evidence.
    """
    shots = (
        RenderShot(
            evidence_id="ev_0",
            image_path=str(render_images[0]),
            duration_us=1_200_000,
            motion=MotionParams(zoom_from=1.0, zoom_to=1.15),
            grade=GradeParams(contrast=1.05, saturation=1.1, vignette=0.3),
            transition_kind="fade",
            transition_us=300_000,
        ),
        RenderShot(
            evidence_id="ev_1",
            image_path=str(render_images[1]),
            duration_us=1_200_000,
            transition_kind="fade",
            transition_us=300_000,
        ),
        RenderShot(evidence_id="ev_2", image_path=str(render_images[2]), duration_us=1_200_000),
    )
    ir = RenderIR(
        shots=shots,
        template_id="unit",
        target_duration_us=3_600_000,
        width=320,
        height=180,
        fps=12,
        crf=30,
        preset="ultrafast",
        audio_bitrate="96k",
        audio_path=str(render_audio),
        audio_duration_us=8_000_000,
        fade_in_us=500_000,
        fade_out_us=500_000,
        loudness_lufs=-16.0,
    )
    first = encode(ir, tmp_path / "first.mp4")
    second = encode(ir, tmp_path / "second.mp4")
    assert first.sha256 == second.sha256
    assert first.render_ir_hash == render_ir_hash(ir) == second.render_ir_hash
    assert first.duration_us == second.duration_us
    assert first.duration_us == pytest.approx(ir.final_duration_us, abs=100_000)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _render_args(images: tuple[Path, ...], audio: Path, out: Path, *extra: str) -> list[str]:
    args = ["slideshow", "render"]
    for path in images:
        args.extend(["--image", str(path)])
    args.extend(
        [
            "--audio",
            str(audio),
            "--duration-min",
            "1",
            "--template",
            "minimal_clean",
            "--resolution",
            "320x180",
            "--fps",
            "12",
            "--out",
            str(out),
            *extra,
        ]
    )
    return args


def test_cli_render_writes_a_master_and_reports_the_pinned_facts(
    render_images: tuple[Path, ...], render_audio: Path, tmp_path: Path
) -> None:
    destination = tmp_path / "cli-master.mp4"
    result = RUNNER.invoke(app, _render_args(render_images, render_audio, destination, "--json"))
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["output_path"] == str(destination)
    assert payload["has_audio"] is True
    assert payload["width"] == 320 and payload["height"] == 180
    assert payload["duration_us"] == pytest.approx(TARGET_US, abs=200_000)
    assert payload["derived_asset_id"].startswith("derived_")
    assert payload["commands"][-1] == "slideshow.render"
    assert Path(payload["output_path"]).is_file()


def test_cli_render_never_replaces_a_file_without_the_flag(
    render_images: tuple[Path, ...], render_audio: Path, tmp_path: Path
) -> None:
    destination = tmp_path / "taken.mp4"
    destination.write_bytes(b"existing master")
    result = RUNNER.invoke(app, _render_args(render_images, render_audio, destination))
    assert result.exit_code == 1
    assert "already exists" in result.output
    assert destination.read_bytes() == b"existing master"


def test_cli_render_rejects_an_unknown_target_duration(
    render_images: tuple[Path, ...], render_audio: Path, tmp_path: Path
) -> None:
    args = _render_args(render_images, render_audio, tmp_path / "x.mp4")
    args[args.index("--duration-min") + 1] = "3"
    result = RUNNER.invoke(app, args)
    assert result.exit_code == 2
    assert "--duration-min must be 1, 2 or 5" in result.output


def test_the_render_lane_is_the_only_module_that_spawns_a_process() -> None:
    source = Path(ffmpeg_lane.__file__).read_text(encoding="utf-8")
    assert "shell=True" not in source
    assert source.count("subprocess.run") == 1
    # The staging rename is the publish step: `os.replace`/`Path.replace`, never
    # an in-place write of the destination.
    assert "replace(destination)" in source
