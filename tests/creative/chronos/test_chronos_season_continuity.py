from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path
from typing import cast

import numpy as np
import pytest

from nexus_ai_agent.creative.chronos import geometry, merkle_ledger, timeline
from nexus_ai_agent.creative.chronos.contracts import KernelError, RationalTimestamp
from nexus_ai_agent.creative.chronos.episode_pipeline import (
    ShowBibleCharacterBible,
    ShowBibleRegistry,
    ShowBibleShotObservation,
    verify_cross_episode_show_bible,
)
from nexus_ai_agent.creative.chronos.identity_lock import verify_cranial_baseline
from nexus_ai_agent.tools.filesystem_policy import WorkspaceFilesystem

EPISODE_COUNT = 12
SHOTS_PER_EPISODE = 10
SAMPLE_RATE_HZ = 44_100
FRAME_RATE = Fraction(24, 1)
COLOR_DELTA_E_TOLERANCE = 2.0
EXPECTED_SEASON_MERKLE_ROOT = "a6dd549235928acccd271c6d8b4606b5388c39afd31a2c267fb4f9df0d8b2a16"


def _show_bible() -> ShowBibleCharacterBible:
    return ShowBibleCharacterBible(
        "mira",
        (
            (-0.06, -0.05, 0.0),
            (0.06, -0.05, 0.0),
            (-0.05, 0.05, 0.0),
            (0.05, 0.05, 0.0),
            (-0.025, 0.0, 0.015),
            (0.025, 0.0, -0.015),
        ),
        1.8,
        (0.1, 0.2, 0.3, 0.4),
        (55.0, 15.0, 20.0),
    )


def _season_evidence() -> tuple[
    list[dict[str, int | float | str]],
    list[ShowBibleShotObservation],
    dict[str, float | int],
]:
    """Build deterministic 12x10 continuity receipts and a deliberately drifting control."""
    bible = _show_bible()
    registry = ShowBibleRegistry()
    registry.register(bible)
    bible = registry.get("mira")
    canonical_landmarks = np.asarray(bible.cranial_landmarks_m, dtype=np.float64)
    canonical_lab = np.asarray(bible.skin_tone_lab, dtype=np.float64)
    exact_frame_period = Fraction(1, 1) / FRAME_RATE

    anchored_observations: list[ShowBibleShotObservation] = []
    season_receipts: list[dict[str, int | float | str]] = []
    naive_scale = 1.0
    naive_lab = canonical_lab.copy()
    naive_sample_cursor = 0
    max_naive_scale_drift = 0.0
    max_naive_color_delta_e = 0.0
    max_anchored_scale_drift = 0.0
    max_anchored_color_delta_e = 0.0
    max_anchored_sample_drift = 0
    total_shots = EPISODE_COUNT * SHOTS_PER_EPISODE
    exact_boundaries = [
        round(Fraction(frame * SAMPLE_RATE_HZ, FRAME_RATE)) for frame in range(total_shots + 1)
    ]
    for global_shot in range(total_shots + 1):
        timestamp = RationalTimestamp(global_shot, exact_frame_period)
        exact_sample = timestamp.seconds * SAMPLE_RATE_HZ
        # Chronos keeps the global timestamp rational and rounds only its absolute
        # position onto the integer sample clock; it never rounds each shot length.
        anchored_sample = round(exact_sample)
        expected_sample = round(Fraction(global_shot * SAMPLE_RATE_HZ, FRAME_RATE))
        max_anchored_sample_drift = max(
            max_anchored_sample_drift, abs(anchored_sample - expected_sample)
        )
        if global_shot == total_shots:
            break

        episode_index, shot_index = divmod(global_shot, SHOTS_PER_EPISODE)
        # Control: independently round every floating-point shot duration, then
        # accumulate that rounded integer duration instead of preserving the
        # rational global frame clock.  At 44.1 kHz / 24 fps, each exact shot is
        # 1837.5 samples, so repeated per-shot rounding creates a clear drift.
        naive_sample_cursor += round(float(exact_frame_period) * SAMPLE_RATE_HZ)
        naive_scale *= 1.0002
        naive_lab = naive_lab + np.array([0.025, 0.0, 0.0], dtype=np.float64)
        naive_scale_drift = abs(naive_scale - 1.0)
        naive_color_delta_e = float(np.linalg.norm(naive_lab - canonical_lab))
        max_naive_scale_drift = max(max_naive_scale_drift, naive_scale_drift)
        max_naive_color_delta_e = max(max_naive_color_delta_e, naive_color_delta_e)

        observed_landmarks = tuple(
            cast(tuple[float, float, float], tuple(float(value) for value in row))
            for row in canonical_landmarks
        )
        observed_lab = cast(
            tuple[float, float, float], tuple(float(value) for value in canonical_lab)
        )
        anchored_observations.append(
            ShowBibleShotObservation(
                episode_index=episode_index + 1,
                shot_index=shot_index,
                cranial_landmarks_m=observed_landmarks,
                metric_height_m=bible.metric_height_m,
                cepstral_envelope=bible.cepstral_reference_envelope,
                skin_tone_lab=observed_lab,
            )
        )
        baseline = verify_cranial_baseline(
            canonical_landmarks,
            canonical_landmarks,
            left_index=0,
            right_index=1,
            relative_tolerance=1e-9,
        )
        color_delta_e = float(np.linalg.norm(np.asarray(observed_lab) - canonical_lab))
        height_drift_m = abs(bible.metric_height_m - 1.8)
        max_anchored_scale_drift = max(max_anchored_scale_drift, baseline.relative_drift)
        max_anchored_color_delta_e = max(max_anchored_color_delta_e, color_delta_e)
        expected_start, expected_end = anchored_sample, exact_boundaries[global_shot + 1]
        season_receipts.append(
            {
                "episode": episode_index + 1,
                "shot": shot_index,
                "sample_start": expected_start,
                "sample_end": expected_end,
                "cranial_scale_drift": baseline.relative_drift,
                "height_drift_m": height_drift_m,
                "color_delta_e": color_delta_e,
            }
        )

    expected_total_samples = round(Fraction(total_shots * SAMPLE_RATE_HZ, FRAME_RATE))
    naive_clock_drift = abs(naive_sample_cursor - expected_total_samples)
    metrics: dict[str, float | int] = {
        "episode_count": EPISODE_COUNT,
        "shots_per_episode": SHOTS_PER_EPISODE,
        "shot_count": len(season_receipts),
        "naive_sample_clock_drift_samples": naive_clock_drift,
        "anchored_sample_clock_drift_samples": max_anchored_sample_drift,
        "naive_max_cranial_scale_drift": max_naive_scale_drift,
        "anchored_max_cranial_scale_drift": max_anchored_scale_drift,
        "naive_max_color_delta_e": max_naive_color_delta_e,
        "anchored_max_color_delta_e": max_anchored_color_delta_e,
        "sample_clock_total_samples": expected_total_samples,
    }
    return season_receipts, anchored_observations, metrics


def _persist_season_root(workspace_root: Path) -> tuple[str, dict[str, object]]:
    workspace_root.mkdir(parents=True, exist_ok=True)
    fs = WorkspaceFilesystem(workspace_root)
    receipts, observations, metrics = _season_evidence()
    show_bible_receipt = verify_cross_episode_show_bible(
        _show_bible(),
        observations,
        cranial_rms_tolerance_m=0.005,
        height_tolerance_m=0.02,
        cepstral_rms_tolerance=2.0,
        skin_delta_e_tolerance=COLOR_DELTA_E_TOLERANCE,
    )
    root_hex = merkle_ledger.merkle_root(receipts).hex()
    for index, receipt in enumerate(receipts):
        proof = merkle_ledger.build_inclusion_proof(receipts, index)
        assert merkle_ledger.verify_inclusion_proof(receipt, proof, bytes.fromhex(root_hex))

    manifest: dict[str, object] = {
        "schema": "nagar-chronos-season-continuity-v1",
        "episode_count": EPISODE_COUNT,
        "shots_per_episode": SHOTS_PER_EPISODE,
        "receipt_count": len(receipts),
        "sample_rate_hz": SAMPLE_RATE_HZ,
        "frame_rate": f"{FRAME_RATE.numerator}/{FRAME_RATE.denominator}",
        "color_delta_e_tolerance": COLOR_DELTA_E_TOLERANCE,
        "merkle_root_sha256": root_hex,
        "metrics": metrics,
        "show_bible_observations_checked": show_bible_receipt.observations_checked,
        "receipts": receipts,
    }
    fs.write_text(
        "season-continuity/manifest.json",
        json.dumps(manifest, sort_keys=True, separators=(",", ":")),
    )
    persisted = json.loads(fs.read_text("season-continuity/manifest.json"))
    assert persisted["merkle_root_sha256"] == merkle_ledger.merkle_root(persisted["receipts"]).hex()
    return root_hex, persisted


def test_pcm_timeline_rejects_single_shot_before_disk_io(tmp_path: Path) -> None:
    with pytest.raises(KernelError, match="at least two WAV shots"):
        timeline.assemble_pcm_wav_stream_copy(
            [tmp_path / "not-created.wav"],
            tmp_path / "published.wav",
            tmp_path / "staging",
        )


def test_axis_angle_rotation_rejects_invalid_axes_and_nonfinite_angles() -> None:
    with pytest.raises(KernelError, match="finite 3-vector"):
        geometry.rotation_matrix_from_axis_angle(np.array([1.0, 0.0]), 0.5)
    with pytest.raises(KernelError, match="nonzero axis"):
        geometry.rotation_matrix_from_axis_angle(np.zeros(3), 0.5)
    with pytest.raises(KernelError, match="finite 3-vector"):
        geometry.rotation_matrix_from_axis_angle(np.array([1.0, 0.0, 0.0]), float("nan"))


def test_twelve_episode_season_continuity_is_show_bible_anchored_and_merkle_persisted(
    tmp_path: Path,
) -> None:
    first_root, manifest = _persist_season_root(tmp_path / "workspace-a")
    second_root, second_manifest = _persist_season_root(tmp_path / "workspace-b")
    metrics = cast(dict[str, float | int], manifest["metrics"])
    receipts = cast(list[dict[str, int | float | str]], manifest["receipts"])

    assert manifest["episode_count"] == 12
    assert manifest["shots_per_episode"] == 10
    assert manifest["receipt_count"] == 120
    assert manifest["show_bible_observations_checked"] == 120
    assert metrics["naive_sample_clock_drift_samples"] == 60
    assert metrics["anchored_sample_clock_drift_samples"] == 0
    assert metrics["naive_max_cranial_scale_drift"] > 1e-3
    assert metrics["anchored_max_cranial_scale_drift"] < 1e-9
    assert metrics["naive_max_color_delta_e"] > COLOR_DELTA_E_TOLERANCE
    assert metrics["anchored_max_color_delta_e"] <= COLOR_DELTA_E_TOLERANCE
    assert all(float(receipt["color_delta_e"]) <= COLOR_DELTA_E_TOLERANCE for receipt in receipts)
    assert first_root == second_root == manifest["merkle_root_sha256"]
    assert first_root == EXPECTED_SEASON_MERKLE_ROOT
    assert receipts == second_manifest["receipts"]
    assert len(first_root) == 64
