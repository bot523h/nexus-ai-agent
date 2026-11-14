from __future__ import annotations

import copy
import hashlib
import json
import shutil
import statistics
import time
import wave
from pathlib import Path
from typing import Any, cast

import numpy as np
import pytest

from nexus_ai_agent.creative.chronos import color_science, merkle_ledger, real_media_continuity
from nexus_ai_agent.creative.chronos.contracts import KernelError
from nexus_ai_agent.creative.chronos.real_media_continuity import (
    CharacterIdentityAnchor,
    RealMediaShot,
    align_pcm_to_sample_count,
    bresenham_sample_counts,
    canonical_sample_horizon,
    execute_real_media_season_continuity,
)
from nexus_ai_agent.tools.filesystem_policy import WorkspaceFilesystem

FPS_NUM = 24_000
FPS_DEN = 1001
SAMPLE_RATE = 48_000


def test_real_four_episode_sixteen_shot_pixel_pcm_and_disk_evidence(tmp_path: Path) -> None:
    workspace = tmp_path / "real-media-season"
    result = execute_real_media_season_continuity(
        workspace,
        episode_count=4,
        shots_per_episode=4,
        frame_count_per_shot=12,
        frame_size=(16, 16),
    )

    assert result.episode_count == 4
    assert result.shot_count == 16
    assert result.frame_count == 192
    expected_samples = result.frame_count * SAMPLE_RATE * FPS_DEN // FPS_NUM
    assert result.exact_sample_horizon == expected_samples == 384_384
    assert result.sample_count == expected_samples
    assert result.anchored_sample_clock_drift == 0
    assert result.naive_sample_clock_drift >= 8
    assert result.naive_audio_sample_count < expected_samples
    assert len(result.merkle_root_sha256) == 64
    assert result.muxed_artifact_count in (0, 16)

    fs = WorkspaceFilesystem(workspace)
    manifest = json.loads(fs.read_text(result.manifest_path))
    assert manifest["merkle_root_sha256"] == result.merkle_root_sha256
    assert len(manifest["receipts"]) == 16
    for shot in result.shots:
        assert min(shot.unanchored_delta_e00_by_frame) > 2.5
        assert max(shot.restored_delta_e00_by_frame) < 0.25
        assert shot.unanchored_scale_drift > 1e-3
        assert shot.restored_scale_drift < 1e-9
        assert shot.downweighted_landmark_count >= 1
        assert shot.audio_samples == shot.sample_end - shot.sample_start
        with wave.open(str(fs.resolve(shot.audio_path)), "rb") as reader:
            assert reader.getframerate() == SAMPLE_RATE
            assert reader.getnframes() == shot.audio_samples
            assert reader.getnchannels() == 1
        assert len(shot.frame_paths) == shot.frame_count == len(shot.frame_sha256s)
        for path, expected_digest in zip(shot.frame_paths, shot.frame_sha256s, strict=True):
            actual_digest, _ = fs.hash_file(path)
            assert actual_digest == expected_digest
            decoded = color_science.read_rgb8_png(fs.resolve(path))
            assert decoded.shape == (16, 16, 3)
        if shot.mux_path is not None:
            assert shot.mux_sha256 is not None
            assert fs.hash_file(shot.mux_path)[0] == shot.mux_sha256
        assert merkle_ledger.verify_rfc6962_bundle(shot.inclusion_bundle)
        assert shot.inclusion_bundle["root_sha256"] == result.merkle_root_sha256
    assert all(
        merkle_ledger.verify_rfc6962_bundle(bundle)
        for bundle in cast(list[dict[str, Any]], manifest["inclusion_bundles"])
    )


def test_explicit_real_buffers_are_restored_and_pcm_is_exactly_sliced(tmp_path: Path) -> None:
    anchor = CharacterIdentityAnchor(
        "test-character",
        np.array(
            [
                [-0.06, -0.05, 0.0],
                [0.06, -0.05, 0.0],
                [-0.05, 0.05, 0.0],
                [0.05, 0.05, 0.0],
                [-0.025, 0.0, 0.015],
                [0.025, 0.0, -0.015],
            ]
        ),
    )
    frames = np.full((2, 8, 8, 3), (0.24, 0.34, 0.42), dtype=np.float64)
    audio = np.linspace(-0.1, 0.1, 3990, dtype=np.float64)
    shot = RealMediaShot(1, 1, frames, anchor.cranial_landmarks_m.copy(), audio)
    result = execute_real_media_season_continuity(
        tmp_path / "explicit-inputs",
        shots=[shot],
        show_bible_anchor=anchor,
        episode_count=1,
        shots_per_episode=1,
        frame_count_per_shot=2,
        frame_size=(8, 8),
    )
    shot_receipt = result.shots[0]
    assert shot_receipt.input_audio_samples == 3990
    assert shot_receipt.audio_padded_samples == 14
    assert shot_receipt.audio_trimmed_samples == 0
    assert result.sample_count == 4004
    assert max(shot_receipt.restored_delta_e00_by_frame) < 0.25
    assert shot_receipt.restored_scale_drift < 1e-9


def test_real_media_stereo_pcm_persistence_is_sample_exact_and_merkle_bound(
    tmp_path: Path,
) -> None:
    anchor = CharacterIdentityAnchor(
        "stereo-character",
        np.array(
            [
                [-0.06, -0.05, 0.0],
                [0.06, -0.05, 0.0],
                [-0.05, 0.05, 0.0],
                [0.05, 0.05, 0.0],
                [-0.025, 0.0, 0.015],
                [0.025, 0.0, -0.015],
            ]
        ),
    )
    frames = np.full((2, 8, 8, 3), (0.24, 0.34, 0.42), dtype=np.float64)
    stereo = np.column_stack((np.linspace(-0.1, 0.1, 3998), np.linspace(0.1, -0.1, 3998)))
    result = execute_real_media_season_continuity(
        tmp_path / "stereo-input",
        shots=[
            RealMediaShot(
                1,
                1,
                frames,
                anchor.cranial_landmarks_m,
                audio_pcm_stereo_float=stereo,
            )
        ],
        show_bible_anchor=anchor,
        episode_count=1,
        shots_per_episode=1,
        frame_count_per_shot=2,
        frame_size=(8, 8),
        ffmpeg_path=None,
    )
    receipt = result.shots[0]
    assert receipt.audio_channel_count == 2
    assert receipt.input_audio_samples == 3998
    assert receipt.audio_padded_samples == 6
    assert receipt.audio_trimmed_samples == 0
    with wave.open(str(tmp_path / "stereo-input" / receipt.audio_path), "rb") as reader:
        assert reader.getnchannels() == 2
        assert reader.getnframes() == receipt.audio_samples == result.exact_sample_horizon
    manifest = json.loads((tmp_path / "stereo-input" / result.manifest_path).read_text())
    assert manifest["receipts"][0]["audio_channel_count"] == 2
    assert merkle_ledger.verify_rfc6962_bundle(receipt.inclusion_bundle)
    with pytest.raises(KernelError, match="either mono or stereo"):
        execute_real_media_season_continuity(
            tmp_path / "conflicting-audio-input",
            shots=[
                RealMediaShot(
                    1,
                    1,
                    frames,
                    anchor.cranial_landmarks_m,
                    np.zeros(4004),
                    stereo,
                )
            ],
            show_bible_anchor=anchor,
            episode_count=1,
            shots_per_episode=1,
            frame_count_per_shot=2,
            frame_size=(8, 8),
            ffmpeg_path=None,
        )


def test_bresenham_clock_conserves_remainders_and_pcm_alignment_reports_trim_pad() -> None:
    counts = bresenham_sample_counts(
        [1, 1, 1, 1], sample_rate_hz=44_100, frame_rate_num=24, frame_rate_den=1
    )
    assert counts == (1837, 1838, 1837, 1838)
    assert sum(counts) == canonical_sample_horizon(
        4, sample_rate_hz=44_100, frame_rate_num=24, frame_rate_den=1
    )
    aligned, trimmed, padded = align_pcm_to_sample_count(np.array([0.1, 0.2, 0.3]), 2)
    assert np.array_equal(aligned, [0.1, 0.2]) and (trimmed, padded) == (1, 0)
    aligned, trimmed, padded = align_pcm_to_sample_count(np.array([0.1]), 3)
    assert np.array_equal(aligned, [0.1, 0.0, 0.0]) and (trimmed, padded) == (0, 2)
    with pytest.raises(KernelError):
        bresenham_sample_counts([0])
    with pytest.raises(KernelError):
        canonical_sample_horizon(-1)


def test_real_media_validation_and_optional_ffmpeg_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    frames = np.full((1, 8, 8, 3), (0.24, 0.34, 0.42), dtype=np.float64)
    with pytest.raises(KernelError):
        bresenham_sample_counts([1], sample_rate_hz=0)
    with pytest.raises(KernelError):
        align_pcm_to_sample_count(np.array([np.nan]), 1)
    with pytest.raises(KernelError):
        align_pcm_to_sample_count(np.array([0.1]), -1)
    with pytest.raises(KernelError):
        real_media_continuity.apply_illuminant_exposure_drift(
            frames,
            source_illuminant_kelvin=6500.0,
            drifted_illuminant_kelvin=1000.0,
            exposure_multiplier=1.0,
        )
    with pytest.raises(KernelError):
        real_media_continuity._remove_known_color_drift(
            frames,
            drifted_illuminant_kelvin=6500.0,
            reference_illuminant_kelvin=6500.0,
            exposure_multiplier=0.0,
        )
    with pytest.raises(KernelError):
        real_media_continuity.restore_anchored_color_frames(
            frames,
            drifted_illuminant_kelvin=6500.0,
            reference_illuminant_kelvin=6500.0,
            exposure_multiplier=1.0,
            reference_patch_linear_rgb=(float("nan"), 0.2, 0.3),
            calibration_roi=(0, 4, 0, 4),
        )
    with pytest.raises(KernelError):
        real_media_continuity.restore_anchored_color_frames(
            np.zeros_like(frames),
            drifted_illuminant_kelvin=6500.0,
            reference_illuminant_kelvin=6500.0,
            exposure_multiplier=1.0,
            reference_patch_linear_rgb=(0.2, 0.3, 0.4),
            calibration_roi=(0, 4, 0, 4),
        )
    with pytest.raises(KernelError):
        real_media_continuity.measure_patch_delta_e00_by_frame(
            frames, (0.2, 0.3, 0.4), (0, 10, 0, 4)
        )
    with pytest.raises(KernelError):
        real_media_continuity.measure_patch_delta_e00_by_frame(
            frames, (float("nan"), 0.3, 0.4), (0, 4, 0, 4)
        )
    with pytest.raises(KernelError):
        execute_real_media_season_continuity(
            tmp_path / "bad-controls", episode_count=0, frame_size=(8, 8)
        )
    with pytest.raises(KernelError):
        execute_real_media_season_continuity(
            tmp_path / "bad-anchor",
            episode_count=1,
            shots_per_episode=1,
            frame_count_per_shot=1,
            frame_size=(8, 8),
            show_bible_anchor=CharacterIdentityAnchor("", np.ones((4, 3))),
        )
    with pytest.raises(KernelError):
        execute_real_media_season_continuity(
            tmp_path / "bad-order",
            shots=[RealMediaShot(1, 2), RealMediaShot(1, 1)],
            episode_count=1,
            shots_per_episode=2,
            frame_count_per_shot=1,
            frame_size=(8, 8),
        )
    with pytest.raises(KernelError):
        execute_real_media_season_continuity(
            tmp_path / "bad-frame-size",
            shots=[RealMediaShot(1, 1, np.zeros((1, 7, 8, 3)))],
            episode_count=1,
            shots_per_episode=1,
            frame_count_per_shot=1,
            frame_size=(8, 8),
        )
    with pytest.raises(KernelError):
        execute_real_media_season_continuity(
            tmp_path / "bad-ffmpeg",
            episode_count=1,
            shots_per_episode=1,
            frame_count_per_shot=1,
            frame_size=(8, 8),
            ffmpeg_path=str(tmp_path / "missing-ffmpeg"),
        )

    monkeypatch.setattr(shutil, "which", lambda _name: "/system/ffmpeg")
    assert real_media_continuity.discover_ffmpeg_executable() == "/system/ffmpeg"


def test_d65_bradford_identity_and_png_integrity_contract(tmp_path: Path) -> None:
    d65 = np.array([0.95047, 1.0, 1.08883], dtype=np.float64)
    assert np.array_equal(color_science.canonical_d65_xyz(), d65)
    assert np.array_equal(
        color_science.BRADFORD_MATRIX,
        np.array(
            [[0.8951, 0.2664, -0.1614], [-0.7502, 1.7135, 0.0367], [0.0389, -0.0685, 1.0296]],
            dtype=np.float64,
        ),
    )
    adaptation = color_science.bradford_adaptation(d65, d65)
    assert float(np.max(np.abs(adaptation - np.eye(3)))) <= 1e-15
    with pytest.raises(KernelError):
        color_science.bradford_adaptation(np.array([1.0, np.nan, 1.0]), d65)

    rgb = np.arange(18, dtype=np.uint8).reshape(2, 3, 3)
    png = color_science.encode_rgb8_png(rgb)
    path = tmp_path / "roundtrip.png"
    path.write_bytes(png)
    assert np.array_equal(color_science.read_rgb8_png(path), rgb)
    with pytest.raises(KernelError):
        color_science.encode_rgb8_png(np.zeros((2, 3), dtype=np.uint8))
    with pytest.raises(KernelError):
        color_science.encode_rgb8_png(np.zeros((0, 3, 3), dtype=np.uint8))
    bad_signature = tmp_path / "bad-signature.png"
    bad_signature.write_bytes(b"not a png")
    with pytest.raises(KernelError):
        color_science.read_rgb8_png(bad_signature)
    truncated = tmp_path / "truncated.png"
    truncated.write_bytes(b"\x89PNG\r\n\x1a\n\x00")
    with pytest.raises(KernelError):
        color_science.read_rgb8_png(truncated)
    corrupt_crc = bytearray(png)
    corrupt_crc[-10] ^= 0x01
    crc_path = tmp_path / "corrupt-crc.png"
    crc_path.write_bytes(corrupt_crc)
    with pytest.raises(KernelError):
        color_science.read_rgb8_png(crc_path)


def test_rfc6962_bundles_validate_tree_shape_and_malformed_inputs() -> None:
    receipts = [{"shot": index} for index in range(5)]
    root = merkle_ledger.merkle_root(receipts)
    root_hex = root.hex()
    for index, receipt in enumerate(receipts):
        proof = merkle_ledger.build_inclusion_proof(receipts, index)
        assert merkle_ledger.verify_inclusion_proof(receipt, proof, root)
        bundle = merkle_ledger.to_rfc6962_bundle(receipt, proof, root)
        assert (
            bundle["leaf_sha256"]
            == hashlib.sha256(b"\x00" + merkle_ledger.canonical_receipt_bytes(receipt)).hexdigest()
        )
        assert merkle_ledger.verify_rfc6962_bundle(bundle)

    sample_proof = merkle_ledger.build_inclusion_proof(receipts, 1)
    sample_bundle = merkle_ledger.to_rfc6962_bundle(receipts[1], sample_proof, root)
    wrong_sides = tuple((not left, digest) for left, digest in sample_proof.audit_path)
    malformed_proofs = (
        merkle_ledger.InclusionProof(1, 5, wrong_sides),
        merkle_ledger.InclusionProof(1, 5, sample_proof.audit_path[:-1]),
        merkle_ledger.InclusionProof(1, 5, ((False, b"x"),) + sample_proof.audit_path[1:]),
        merkle_ledger.InclusionProof(True, 5, sample_proof.audit_path),
        merkle_ledger.InclusionProof(1, True, sample_proof.audit_path),
    )
    for proof in malformed_proofs:
        assert not merkle_ledger.verify_inclusion_proof(receipts[1], proof, root)
    with pytest.raises(ValueError):
        merkle_ledger.to_rfc6962_bundle(receipts[1], malformed_proofs[0], root)
    assert not merkle_ledger.verify_inclusion_proof(
        {"bad": float("nan")}, merkle_ledger.InclusionProof(0, 1, ()), b"0" * 32
    )

    bad_bundles: list[dict[str, Any]] = []
    for key, value in (
        ("format", "not-rfc6962"),
        ("leaf_prefix_hex", "01"),
        ("node_prefix_hex", "00"),
        ("receipt", ["not", "a", "mapping"]),
        ("proof", []),
        ("root_sha256", 123),
        ("leaf_sha256", None),
        ("root_sha256", "0"),
        ("root_sha256", "g" * 64),
    ):
        malformed = copy.deepcopy(sample_bundle)
        malformed[key] = value
        bad_bundles.append(malformed)

    for index_field, invalid_value in (("leaf_index", True), ("tree_size", True)):
        malformed = copy.deepcopy(sample_bundle)
        malformed["proof"][index_field] = invalid_value
        bad_bundles.append(malformed)
    malformed_path = copy.deepcopy(sample_bundle)
    malformed_path["proof"]["audit_path"] = None
    bad_bundles.append(malformed_path)
    malformed_node = copy.deepcopy(sample_bundle)
    malformed_node["proof"]["audit_path"][0] = []
    bad_bundles.append(malformed_node)
    malformed_node = copy.deepcopy(sample_bundle)
    malformed_node["proof"]["audit_path"][0]["sibling_is_left"] = 1
    bad_bundles.append(malformed_node)
    malformed_node = copy.deepcopy(sample_bundle)
    malformed_node["proof"]["audit_path"][0]["sha256"] = "z" * 64
    bad_bundles.append(malformed_node)
    malformed_receipt = copy.deepcopy(sample_bundle)
    malformed_receipt["receipt"] = {"bad": float("nan")}
    malformed_receipt["leaf_sha256"] = "0" * 64
    bad_bundles.append(malformed_receipt)
    for malformed in bad_bundles:
        assert not merkle_ledger.verify_rfc6962_bundle(malformed)
    assert not merkle_ledger.verify_rfc6962_bundle({"root_sha256": root_hex})


def test_three_run_real_media_microbenchmark(tmp_path: Path) -> None:
    durations: list[float] = []
    roots: list[str] = []
    for run_index in range(3):
        start = time.perf_counter()
        result = execute_real_media_season_continuity(
            tmp_path / f"benchmark-{run_index}",
            episode_count=4,
            shots_per_episode=4,
            frame_count_per_shot=12,
            frame_size=(16, 16),
        )
        durations.append((time.perf_counter() - start) * 1000.0)
        roots.append(result.merkle_root_sha256)
        assert result.shot_count == 16 and result.sample_count == 384_384
    assert len(durations) == 3
    assert len(set(roots)) == 1
    print(
        "CHRONOS_APEX_BENCHMARK_N3 shots=16 frames=192 frame_size=16x16 "
        f"median_ms={statistics.median(durations):.3f} min_ms={min(durations):.3f} "
        f"max_ms={max(durations):.3f} muxed_per_run={result.muxed_artifact_count}"
    )
    assert all(len(root) == 64 for root in roots)
