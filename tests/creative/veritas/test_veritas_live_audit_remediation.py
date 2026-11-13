"""Live audit regression checks: independent containers, retention, durable lineage."""

from __future__ import annotations

import hashlib
import importlib
import json
import subprocess
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from nexus_ai_agent.creative.veritas import omnia_cinema_physics as physics
from nexus_ai_agent.creative.veritas import veritas_spine_adapter as spine
from nexus_ai_agent.creative.veritas.real_media_veritas_compiler import (
    compile_and_heal_real_media_shot,
)


def request(root: Path) -> spine.VeritasSpinePublishRequest:
    return spine.VeritasSpinePublishRequest(
        "project",
        "revision",
        "shot",
        "attempt",
        7,
        root,
        root / "published",
        input_frames_rgb=np.zeros((2, 8, 8, 3)),
        input_audio_pcm=np.zeros(100),
    )


@pytest.mark.parametrize("m", [1, 2, 6])
def test_angular_harmonic_bound_and_independent_coordinates(m: int) -> None:
    reference = np.random.default_rng(31).random((8, 8, 3))
    sequence = physics.synthesize_rotary_subframes(
        reference,
        angular_velocity_rad_s=200,
        fps=24,
        angular_harmonic_m=m,
    )
    q = int(np.ceil(m * 200 / (0.85 * np.pi * 24)))
    assert sequence.output_fps == 24 * q
    assert len(sequence.frames) == 2 * q
    assert not physics.validate_rotary_subframes(sequence)
    spine._verify_rotary_coordinates(sequence)
    # A forged low harmonic cannot retroactively legalize under-sampled spokes.
    undersampled = replace(sequence, angular_harmonic_m=100)
    assert "nyquist_rotary_aliasing" in physics.validate_rotary_subframes(undersampled)
    with pytest.raises(ValueError, match="Nyquist"):
        spine._verify_rotary_coordinates(undersampled)


@pytest.mark.parametrize("m", [0, -1, True, 1.5])
def test_invalid_harmonic_refusal(m: Any) -> None:
    with pytest.raises(physics.StroboscopicAliasingError, match="harmonic"):
        physics.synthesize_rotary_subframes(
            np.ones((4, 4)), angular_velocity_rad_s=200, fps=24, angular_harmonic_m=m
        )
    sequence = physics.synthesize_rotary_subframes(
        np.ones((4, 4)), angular_velocity_rad_s=200, fps=24
    )
    assert "invalid_angular_harmonic" in physics.validate_rotary_subframes(
        replace(sequence, angular_harmonic_m=m)
    )
    with pytest.raises(ValueError, match="harmonic"):
        spine._verify_rotary_coordinates(replace(sequence, angular_harmonic_m=m))


def test_independent_pixel_invariants_and_transform_refuse() -> None:
    sequence = physics.synthesize_rotary_subframes(
        np.random.default_rng(9).random((8, 8, 3)), angular_velocity_rad_s=200, fps=24
    )
    with pytest.raises(ValueError, match="range"):
        spine._verify_rotary_coordinates(replace(sequence, frames=sequence.frames + 2))
    with pytest.raises(ValueError, match="coordinate"):
        spine._verify_rotary_coordinates(replace(sequence, frames=sequence.frames[::-1]))
    with pytest.raises(ValueError, match="phase step"):
        spine._verify_rotary_coordinates(replace(sequence, phases_rad=sequence.phases_rad**2))


@pytest.mark.parametrize("kind", ["tamper", "stale"])
def test_forensic_quarantine_retains_failed_bytes(tmp_path: Path, kind: str) -> None:
    token = [7]
    expected: dict[str, str] = {}

    def fault(stage: Path) -> None:
        if kind == "tamper":
            (stage / "frame_0000.png").write_bytes(b"tampered")
        else:
            token[0] = 8
        expected["frame_0000.png"] = hashlib.sha256(
            (stage / "frame_0000.png").read_bytes()
        ).hexdigest()

    with pytest.raises(spine.VeritasSpineVerificationError):
        spine.VeritasStagedSpineAdapter(lambda: token[0], fault).execute(request(tmp_path))
    assert not (tmp_path / "published").exists()
    quarantine = tmp_path / ".quarantine" / "attempt_7"
    receipt = json.loads((quarantine / "quarantine_receipt.json").read_text())
    assert receipt["attempt_id"] == "attempt" and receipt["fencing_token"] == 7
    assert receipt["failure_reason"]
    assert receipt["observed_digests"]["frame_0000.png"] == expected["frame_0000.png"]
    assert (quarantine / "frame_0000.png").is_file()
    assert not (tmp_path / ".staging" / "attempt_7").exists()


def test_ledger_restart_exact_retry_and_stale_cas(tmp_path: Path) -> None:
    req = request(tmp_path)
    result = spine.VeritasStagedSpineAdapter(lambda: 7).execute(req)
    root = req.published_root / "_lineage"
    assert json.loads((root / "asset_versions.json").read_text()) == [asdict(result.asset_version)]
    assert json.loads((root / "artifact_passports.json").read_text()) == [
        asdict(result.artifact_passport)
    ]
    assert result.artifact_passport.law_pass_count == 15
    # A brand new adapter and store read committed disk state, not an in-memory cache.
    retry = spine.VeritasStagedSpineAdapter(lambda: 7).execute(req)
    assert retry.asset_version == result.asset_version
    assert len(json.loads((root / "asset_versions.json").read_text())) == 1
    with pytest.raises(spine.VeritasStaleFencingTokenError, match="durable"):
        spine.VeritasStagedSpineAdapter(lambda: 6).execute(replace(req, fencing_token=6))
    store = spine.VeritasLineageStore(req.published_root, tmp_path / "lock")
    with store.transaction():
        with pytest.raises(spine.VeritasStaleFencingTokenError, match="durable"):
            store.commit(replace(result.asset_version, fencing_token=6), result.artifact_passport)
        with pytest.raises(spine.VeritasSpineVerificationError, match="conflicting"):
            store.commit(
                replace(result.asset_version, revision_id="other"), result.artifact_passport
            )
    assert len(store.records()[0]) == 1
    # Different input is NOT an idempotent retry of an existing publication.
    with pytest.raises(spine.VeritasSpineVerificationError, match="exists"):
        spine.VeritasStagedSpineAdapter(lambda: 7).execute(
            replace(req, input_frames_rgb=np.ones((2, 8, 8, 3)) * 0.4)
        )


def test_command_bus_authorization_before_staging(tmp_path: Path) -> None:
    seen: list[str] = []

    def authorize(req: spine.VeritasSpinePublishRequest) -> bool:
        seen.append(req.attempt_id)
        return False

    with pytest.raises(spine.VeritasSpineVerificationError, match="CommandBus"):
        spine.VeritasStagedSpineAdapter(lambda: 7, command_bus_authorizer=authorize).execute(
            request(tmp_path)
        )
    assert seen == ["attempt"]
    assert not (tmp_path / ".staging").exists()
    assert not (tmp_path / "published").exists()


@pytest.mark.parametrize("fault", ["count", "rate", "corrupt"])
def test_independent_real_container_refuses_anchored_wrong_stream(
    tmp_path: Path, fault: str
) -> None:
    bundle = compile_and_heal_real_media_shot(
        tmp_path, input_frames=np.zeros((2, 8, 8, 3)), input_audio_pcm=np.zeros(100)
    )
    if bundle.mp4_path is None:
        pytest.skip("local FFmpeg unavailable")
    binary = importlib.import_module("imageio_ffmpeg").get_ffmpeg_exe()
    path = Path(bundle.mp4_path)
    if fault == "corrupt":
        path.write_bytes(b"invalid-stream")
    else:
        count, fps = (1, 24) if fault == "count" else (2, 12)
        subprocess.run(
            [
                binary,
                "-v",
                "error",
                "-y",
                "-framerate",
                str(fps),
                "-i",
                str(tmp_path / "frame_%04d.png"),
                "-frames:v",
                str(count),
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                str(path),
            ],
            check=True,
        )
    # Anchor the NEW container hash, so this tests its actual stream, not the digest guard.
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest_path = tmp_path / "veritas_merkle_bundle.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["output_mp4_sha256"] = digest
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(spine.VeritasMediaVerificationError, match="MP4"):
        spine.verify_compiled_veritas_bundle_on_disk(replace(bundle, output_mp4_sha256=digest))


def test_mp4_metadata_rate_and_timestamps_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    # Byte count alone is insufficient even when decode exits successfully.
    completed = SimpleNamespace(
        returncode=0, stdout=bytes(2 * 8 * 8 * 3), stderr=b"pts_time:0 pts_time:0.04"
    )
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: completed)
    with pytest.raises(ValueError, match="rate or duration"):
        spine._verify_mp4_stream("ffmpeg", Path("video.mp4"), 2, 8, 8, 24)


def test_invalid_compiler_harmonic_fails_before_writing(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="harmonic"):
        compile_and_heal_real_media_shot(tmp_path / "not-created", angular_harmonic_m=0)
    assert not (tmp_path / "not-created").exists()


def test_injected_store_failure_rolls_back_media_and_retains_evidence(tmp_path: Path) -> None:
    from collections.abc import Iterator
    from contextlib import contextmanager

    class RefusingStore:
        @contextmanager
        def transaction(self) -> Iterator[None]:
            yield

        def records(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
            return [], []

        def commit(
            self,
            asset: spine.VeritasAssetVersionRecord,
            passport: spine.VeritasArtifactPassportRecord,
        ) -> None:
            raise OSError("injected storage failure")

    root = tmp_path / "published"
    root.mkdir()
    sentinel = root / "sentinel"
    sentinel.write_bytes(b"prior publication")
    with pytest.raises(spine.VeritasSpineVerificationError, match="storage failure"):
        spine.VeritasStagedSpineAdapter(lambda: 7, lineage_store=RefusingStore()).execute(
            request(tmp_path)
        )
    assert list(root.iterdir()) == [sentinel]
    assert sentinel.read_bytes() == b"prior publication"
    assert (tmp_path / ".quarantine/attempt_7/quarantine_receipt.json").is_file()


def test_invalid_lineage_pointer_refused(tmp_path: Path) -> None:
    published = tmp_path / "published"
    published.mkdir()
    (published / "_lineage").symlink_to(tmp_path, target_is_directory=True)
    store = spine.VeritasLineageStore(published, tmp_path / "lock")
    with pytest.raises(spine.VeritasSpineVerificationError, match="generation"):
        store.records()


def test_quarantine_collision_retains_both_attempts(tmp_path: Path) -> None:
    def tamper(stage: Path) -> None:
        (stage / "dialogue.wav").write_bytes(b"bad")

    adapter = spine.VeritasStagedSpineAdapter(lambda: 7, tamper)
    for _ in range(2):
        with pytest.raises(spine.VeritasSpineVerificationError):
            adapter.execute(request(tmp_path))
    assert len(list((tmp_path / ".quarantine").rglob("quarantine_receipt.json"))) == 2


def test_mp4_last_frame_hold_is_not_accepted_as_nominal_rate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completed = SimpleNamespace(
        returncode=0,
        stdout=bytes(2 * 8 * 8 * 3),
        stderr=(
            b"config in time_base: 1/12288, frame_rate: 24/1 "
            b"pts_time:0 duration_time:0.0416667 "
            b"pts_time:0.0416667 duration_time:0.5"
        ),
    )
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: completed)
    with pytest.raises(ValueError, match="frame duration"):
        spine._verify_mp4_stream("ffmpeg", Path("video.mp4"), 2, 8, 8, 24)
