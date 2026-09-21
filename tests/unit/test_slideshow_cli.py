"""Wave 2b — the ``nexus slideshow`` / ``nexus packs`` CLI surface."""

from __future__ import annotations

import json
from pathlib import Path

from slideshow_media import SlideshowMedia
from typer.testing import CliRunner

from nexus_ai_agent.cli import app

RUNNER = CliRunner()


def _plan_args(media: SlideshowMedia, *extra: str) -> list[str]:
    args = ["slideshow", "plan"]
    for path in media.images:
        args.extend(["--image", str(path)])
    args.extend(["--audio", str(media.audio), *extra])
    return args


def test_templates_command_lists_the_library() -> None:
    result = RUNNER.invoke(app, ["slideshow", "templates"])
    assert result.exit_code == 0, result.output
    assert "14 tone template(s): 12 primary, 2 alternate" in result.output
    assert "travel_documentary" in result.output


def test_templates_command_is_machine_readable() -> None:
    result = RUNNER.invoke(app, ["slideshow", "templates", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert len(payload) == 14
    ids = {entry["template_id"] for entry in payload}
    assert {"calm_reflective", "cinematic_epic", "tech_product"} <= ids
    assert all(entry["render"]["fps"] >= 12 for entry in payload)


def test_plan_command_shows_a_human_summary(slideshow_media: SlideshowMedia) -> None:
    result = RUNNER.invoke(app, _plan_args(slideshow_media, "--duration-min", "1"))
    assert result.exit_code == 0, result.output
    assert "shots ·" in result.output
    assert "alignment=detected" in result.output


def test_plan_command_emits_the_plan_as_json(slideshow_media: SlideshowMedia) -> None:
    result = RUNNER.invoke(app, _plan_args(slideshow_media, "--duration-min", "1", "--json"))
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["shot_count"] == 12
    assert payload["plan"]["shots"][-1]["slot"]["end_us"] == 60_000_000
    assert payload["commands"][-1] == "slideshow.compose"
    assert payload["state_hash"].startswith("sha256:")


def test_plan_command_writes_the_plan_to_a_file(
    slideshow_media: SlideshowMedia, tmp_path: Path
) -> None:
    target = tmp_path / "plan.json"
    result = RUNNER.invoke(
        app, _plan_args(slideshow_media, "--duration-min", "1", "--out", str(target))
    )
    assert result.exit_code == 0, result.output
    written = json.loads(target.read_text(encoding="utf-8"))
    assert written["shot_count"] == 12


def test_plan_command_rejects_an_unsupported_duration(slideshow_media: SlideshowMedia) -> None:
    result = RUNNER.invoke(app, _plan_args(slideshow_media, "--duration-min", "3"))
    assert result.exit_code == 2
    assert "must be 1, 2 or 5" in result.output


def test_plan_command_reports_manual_mismatches(slideshow_media: SlideshowMedia) -> None:
    args = ["slideshow", "plan"]
    for path in slideshow_media.images[:3]:
        args.extend(["--image", str(path)])
    args.extend(["--duration-min", "1", "--mode", "manual", "--shot-seconds", "10,10,10"])
    result = RUNNER.invoke(app, args)
    assert result.exit_code == 1
    assert "more than one frame" in result.output


def test_plan_command_needs_shot_seconds_in_manual_mode(
    slideshow_media: SlideshowMedia,
) -> None:
    args = ["slideshow", "plan"]
    for path in slideshow_media.images[:3]:
        args.extend(["--image", str(path)])
    args.extend(["--duration-min", "1", "--mode", "manual"])
    result = RUNNER.invoke(app, args)
    assert result.exit_code == 1
    assert "--shot-seconds" in result.output


def test_plan_command_is_fail_closed_for_hosted_analysis(
    slideshow_media: SlideshowMedia,
) -> None:
    result = RUNNER.invoke(
        app,
        _plan_args(
            slideshow_media,
            "--duration-min",
            "1",
            "--provider",
            "gemini",
            "--json",
        ),
    )
    assert result.exit_code == 1
    assert "NEXUS_SLIDESHOW_ALLOW_IMAGE_UPLOAD" in result.output


def test_packs_list_reports_the_pack_as_activatable() -> None:
    result = RUNNER.invoke(app, ["packs", "list", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    pack = next(p for p in payload if p["package_id"] == "nexus.slideshow.compose")
    assert pack["package_id"] == "nexus.slideshow.compose"
    assert pack["pending_capabilities"] == []
    assert pack["active"] is False
    assert pack["external_binaries"] == ["ffmpeg"]


def test_packs_list_human_output_mentions_all_capabilities() -> None:
    result = RUNNER.invoke(app, ["packs", "list"])
    assert result.exit_code == 0, result.output
    assert "nexus.slideshow.compose" in result.output
    assert "capabilities=6" in result.output
    slideshow_line = next(line for line in result.output.splitlines() if "capabilities=6" in line)
    assert "pending=" not in slideshow_line


def test_packs_activate_turns_the_pack_on() -> None:
    result = RUNNER.invoke(app, ["packs", "activate", "nexus.slideshow.compose", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["active"] is True
    assert len(payload["capabilities"]) == 6


def test_packs_activate_rejects_an_unknown_pack() -> None:
    result = RUNNER.invoke(app, ["packs", "activate", "nexus.ghost.pack"])
    assert result.exit_code == 1
    assert "not registered" in result.output


def test_packs_verify_now_accepts_the_builtin_manifest() -> None:
    """The runtime knows the pack's operations in Wave 2b, so verification passes."""
    manifest = (
        Path(__file__).parents[2]
        / "src"
        / "nexus_ai_agent"
        / "creative"
        / "packs"
        / "slideshow"
        / "pack.manifest.json"
    )
    result = RUNNER.invoke(app, ["packs", "verify", str(manifest), "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["pending_capabilities"] == []
    assert payload["signature_state"] == "placeholder"
