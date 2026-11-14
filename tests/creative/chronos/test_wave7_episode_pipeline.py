from __future__ import annotations

import hashlib
import json
import wave
from dataclasses import fields, replace
from fractions import Fraction
from pathlib import Path

import numpy as np
import pytest

import nexus_ai_agent.creative.chronos.acoustics_lipsync as acoustics_lipsync
import nexus_ai_agent.creative.chronos.merkle_ledger as merkle_ledger
import nexus_ai_agent.creative.chronos.weather_and_grading as weather_and_grading
from nexus_ai_agent.creative.chronos import episode_pipeline as episode_module
from nexus_ai_agent.creative.chronos.contracts import Continuity, KernelError, RationalTimestamp
from nexus_ai_agent.creative.chronos.episode_pipeline import (
    ContinuityRefusal,
    EpisodeShotInput,
    ShotDAG,
    ShowBibleCharacterBible,
    ShowBibleRegistry,
    ShowBibleShotObservation,
    _read_mono_pcm16,
    run_verified_episode_continuity_pipeline,
    verify_cross_episode_show_bible,
)


def _write_mono_wav(path: Path, samples: np.ndarray, sample_rate: int = 8000) -> None:
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(sample_rate)
        writer.writeframes(np.rint(np.clip(samples, -1, 0.999) * 32768).astype("<i2").tobytes())


def _bible() -> ShowBibleCharacterBible:
    landmarks = (
        (-0.06, -0.05, 0.0),
        (0.06, -0.05, 0.0),
        (-0.05, 0.05, 0.0),
        (0.05, 0.05, 0.0),
        (-0.025, 0.0, 0.015),
        (0.025, 0.0, -0.015),
    )
    return ShowBibleCharacterBible("mira", landmarks, 1.8, (0.1, 0.2, 0.3, 0.4), (55.0, 15.0, 20.0))


def _shot(tmp_path: Path, shot_index: int, focal_px: float = 400.0) -> EpisodeShotInput:
    bible = _bible()
    landmarks = np.asarray(bible.cranial_landmarks_m)
    depth = 2.0
    k = np.array([[focal_px, 0.0, 8.0], [0.0, focal_px, 8.0], [0.0, 0.0, 1.0]])
    pixels = np.column_stack(
        (8 + focal_px * landmarks[:, 0] / depth, 8 + focal_px * landmarks[:, 1] / depth)
    )
    count, rate = 4096, 8000
    time = np.arange(count) / rate
    voice = 0.22 * np.sin(2 * np.pi * 180 * time) + 0.06 * np.sin(2 * np.pi * 360 * time)
    dialogue_path, ambience_path = (
        tmp_path / f"dialogue-{shot_index}.wav",
        tmp_path / f"ambience-{shot_index}.wav",
    )
    _write_mono_wav(dialogue_path, voice)
    _write_mono_wav(ambience_path, 0.04 * np.sin(2 * np.pi * 60 * time))
    y, x = np.mgrid[0:16, 0:20]
    image = np.repeat((0.2 + 0.01 * x + 0.005 * y)[..., None], 3, axis=2)
    return EpisodeShotInput(
        shot_index=shot_index,
        scene_linear_rgb=image,
        alpha=np.ones((16, 20)),
        depth_m=4.0,
        canonical_landmarks_m=landmarks.copy(),
        observed_landmarks_3d_m=landmarks.copy(),
        observed_landmarks_px=pixels,
        intrinsics=k,
        camera_depth_m=depth,
        top_px=(8.0, 8.0),
        bottom_px=(8.0, 8.0 + focal_px * 1.8 / depth),
        camera_a=np.array([-2.0, 0.0, 1.0]),
        camera_b=np.array([-2.0, 10.0, 1.0]),
        actor_a=np.array([0.0, 0.0, 1.0]),
        actor_b=np.array([0.0, 10.0, 1.0]),
        gaze_a=np.array([0.0, 10.0, 0.0]),
        gaze_b=np.array([0.0, -10.0, 0.0]),
        ik_root_position=np.zeros(3),
        ik_segment_lengths_m=(1.0, 1.0),
        ik_effector_target=np.array([1.0, 1.0, 0.0]),
        foot_timestamps=tuple(RationalTimestamp(i, Fraction(1, 24)) for i in range(3)),
        ankle_positions_m=np.array([[0.0, 0.0, 0.0], [0.1, 0.0, 0.02], [0.2, 0.0, 0.0]]),
        stance_contact=np.array([False, True, True]),
        cepstral_envelope=bible.cepstral_reference_envelope,
        skin_tone_lab=bible.skin_tone_lab,
        dialogue_wav_path=dialogue_path,
        ambience_wav_path=ambience_path,
        speaker_position_m=(2.0, 2.0, 1.2),
        camera_position_m=(2.5, 2.0, 1.2),
    )


def test_show_bible_registry_conflict_and_invalid_wav_inputs(tmp_path: Path) -> None:
    bible = _bible()
    registry = ShowBibleRegistry()
    registry.register(bible)
    registry.register(bible)
    assert registry.get("mira") == bible
    with pytest.raises(ContinuityRefusal, match="conflicting show-bible"):
        registry.register(replace(bible, metric_height_m=1.7))
    with pytest.raises(ContinuityRefusal, match="unknown show-bible"):
        registry.get("unknown")
    with pytest.raises(ContinuityRefusal, match="invalid WAV"):
        _read_mono_pcm16(tmp_path / "missing.wav")
    empty = tmp_path / "empty.wav"
    _write_mono_wav(empty, np.array([]))
    with pytest.raises(ContinuityRefusal, match="empty or truncated"):
        _read_mono_pcm16(empty)


def test_show_bible_cross_episode_metrics_and_typed_refusal() -> None:
    bible = _bible()
    observation = ShowBibleShotObservation(
        1,
        2,
        bible.cranial_landmarks_m,
        1.801,
        bible.cepstral_reference_envelope,
        bible.skin_tone_lab,
    )
    receipt = verify_cross_episode_show_bible(bible, [observation])
    assert receipt.observations_checked == 1 and receipt.max_height_drift_m == pytest.approx(0.001)
    drift = replace(
        observation,
        cranial_landmarks_m=tuple((x + 0.02, y, z) for x, y, z in observation.cranial_landmarks_m),
        metric_height_m=1.9,
        cepstral_envelope=(8.0, 8.0, 8.0, 8.0),
        skin_tone_lab=(70.0, 30.0, 30.0),
    )
    with pytest.raises(ContinuityRefusal, match="drift"):
        verify_cross_episode_show_bible(bible, [drift])
    with pytest.raises(ContinuityRefusal, match="at least one"):
        verify_cross_episode_show_bible(bible, [])
    with pytest.raises(ContinuityRefusal, match="duplicate"):
        verify_cross_episode_show_bible(bible, [observation, observation])
    malformed = replace(observation, cepstral_envelope=(1.0,))
    with pytest.raises(ContinuityRefusal, match="malformed"):
        verify_cross_episode_show_bible(bible, [malformed])
    with pytest.raises(ContinuityRefusal, match="canonical"):
        verify_cross_episode_show_bible(replace(bible, metric_height_m=0.0), [observation])


def test_shotdag_delta_transitive_invalidation_and_refusal() -> None:
    dag = ShotDAG(
        Continuity.OK,
        {"a": "old", "gone": "hash"},
        {"a": "new", "b": "new"},
        {"a": ("identity",), "identity": ("png",), "b": ("audio",)},
    )
    changed, invalidated = dag.evaluate_delta()
    assert changed == ("a", "b", "gone")
    assert set(invalidated) == {"a", "b", "gone", "identity", "png", "audio"}
    for state in (Continuity.UNKNOWN, Continuity.FAIL):
        with pytest.raises(ContinuityRefusal):
            replace(dag, continuity=state).evaluate_delta()


@pytest.mark.parametrize(
    ("fault", "message"),
    [
        ("ik-error", "effector IK rejected"),
        ("ik-unreachable", "effector IK residual"),
        ("foot-slip", "stance foot slip"),
        ("color-qc", "broadcast color QC"),
        ("png-shape", "PNG persistence verification"),
        ("sample-rate", "sample rates differ"),
        ("timbre", "cepstral timbre consistency"),
        ("jaw-lag", "jaw-viseme timing drift"),
        ("merkle", "Merkle inclusion proof failed"),
    ],
)
def test_episode_pipeline_downstream_refusal_branches(
    fault: str, message: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    shots = [_shot(tmp_path, 0), _shot(tmp_path, 1)]
    if fault == "ik-error":
        monkeypatch.setattr(
            episode_module,
            "solve_two_bone_or_chain_ik",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(KernelError("bad IK")),
        )
    elif fault == "ik-unreachable":
        monkeypatch.setattr(
            episode_module,
            "solve_two_bone_or_chain_ik",
            lambda *_args, **_kwargs: SimpleNamespace(reachable=False),
        )
    elif fault == "foot-slip":
        monkeypatch.setattr(
            episode_module,
            "enforce_stance_foot_lock",
            lambda *_args, **_kwargs: SimpleNamespace(max_foot_slip_mm_after=1.0),
        )
    elif fault == "color-qc":
        monkeypatch.setattr(
            weather_and_grading,
            "legalize_broadcast_rgb_ebu_r103",
            lambda image: SimpleNamespace(
                illegal_pixel_fraction_after=1.0, sobel_structure_correlation=1.0, image=image
            ),
        )
    elif fault == "png-shape":
        monkeypatch.setattr(episode_module, "read_rgb8_png", lambda _path: np.zeros((1, 1, 3)))
    elif fault == "sample-rate":
        voice = 0.2 * np.sin(2 * np.pi * 180 * np.arange(4096) / 16_000)
        _write_mono_wav(shots[1].dialogue_wav_path, voice, sample_rate=16_000)
        _write_mono_wav(shots[1].ambience_wav_path, voice * 0.1, sample_rate=16_000)
    elif fault == "timbre":
        monkeypatch.setattr(
            acoustics_lipsync,
            "measure_cepstral_timbre_consistency",
            lambda *_args, **_kwargs: SimpleNamespace(status="REFUSED", mean_distance_db=None),
        )
    elif fault == "jaw-lag":
        monkeypatch.setattr(
            acoustics_lipsync,
            "solve_jaw_trajectory",
            lambda *_args, **_kwargs: SimpleNamespace(peak_lag_frames=4.0),
        )
    elif fault == "merkle":
        monkeypatch.setattr(merkle_ledger, "verify_inclusion_proof", lambda *_args: False)

    with pytest.raises(ContinuityRefusal, match=message):
        run_verified_episode_continuity_pipeline(9, shots, _bible(), tmp_path / f"fault-{fault}")


def test_verified_episode_pipeline_real_png_wav_and_merkle(tmp_path: Path) -> None:
    shots = [_shot(tmp_path, 0, 300.0), _shot(tmp_path, 1, 700.0), _shot(tmp_path, 2, 1000.0)]
    passport = run_verified_episode_continuity_pipeline(
        7, shots, _bible(), tmp_path / "episode-stage"
    )
    assert passport.all_invariants_verified
    assert passport.shot_count == 3
    assert len(passport.published_png_sha256s) == 3
    assert all(Path(path).is_file() for path in passport.published_png_paths)
    assert all(len(digest) == 64 for digest in passport.published_png_sha256s)
    assert Path(passport.published_wav_path).is_file()
    assert (
        hashlib.sha256(Path(passport.published_wav_path).read_bytes()).hexdigest()
        == passport.published_wav_sha256
    )
    with wave.open(passport.published_wav_path, "rb") as reader:
        assert reader.getnchannels() == 2 and reader.getnframes() > 3 * 4096
    assert len(passport.merkle_root_sha256) == 64
    assert passport.show_bible_receipt.observations_checked == 3
    assert "episode:merkle" in passport.invalidated_nodes


def test_episode_pipeline_refuses_unknown_drift_dag_and_missing_media(tmp_path: Path) -> None:
    shots = [_shot(tmp_path, 0), _shot(tmp_path, 1)]
    with pytest.raises(ContinuityRefusal, match="UNKNOWN"):
        run_verified_episode_continuity_pipeline(
            1, shots, _bible(), tmp_path / "unknown", shot_dag=ShotDAG(Continuity.UNKNOWN)
        )
    mismatched = ShotDAG(Continuity.OK, {}, {"not-a-shot": "stale"})
    with pytest.raises(ContinuityRefusal, match="fingerprints"):
        run_verified_episode_continuity_pipeline(
            1, shots, _bible(), tmp_path / "dag", shot_dag=mismatched
        )
    bad_pixels = shots[0].observed_landmarks_px.copy()
    bad_pixels[0] += 150.0
    bad_identity = replace(shots[0], observed_landmarks_px=bad_pixels)
    with pytest.raises(ContinuityRefusal):
        run_verified_episode_continuity_pipeline(
            1, [bad_identity, shots[1]], _bible(), tmp_path / "identity"
        )
    bad_height = replace(shots[0], bottom_px=(8.0, 8.0 + 100.0))
    with pytest.raises(ContinuityRefusal, match="height drift"):
        run_verified_episode_continuity_pipeline(
            1, [bad_height, shots[1]], _bible(), tmp_path / "height"
        )
    bad_canonical = replace(
        shots[0],
        canonical_landmarks_m=np.asarray(
            [(x + 0.03, y, z) for x, y, z in shots[0].canonical_landmarks_m]
        ),
    )
    with pytest.raises(ContinuityRefusal, match="local cranial"):
        run_verified_episode_continuity_pipeline(
            1, [bad_canonical, shots[1]], _bible(), tmp_path / "canonical"
        )
    no_media = replace(shots[0], dialogue_wav_path=tmp_path / "missing.wav")
    with pytest.raises(ContinuityRefusal, match="unavailable"):
        run_verified_episode_continuity_pipeline(
            1, [no_media, shots[1]], _bible(), tmp_path / "missing"
        )
    with pytest.raises(ContinuityRefusal, match="at least two"):
        run_verified_episode_continuity_pipeline(1, [shots[0]], _bible(), tmp_path / "one")
    with pytest.raises(ContinuityRefusal, match="unique"):
        run_verified_episode_continuity_pipeline(
            1, [shots[0], shots[0]], _bible(), tmp_path / "duplicate"
        )


def test_episode_pipeline_eyeline_ik_and_audio_refusals(tmp_path: Path) -> None:
    shots = [_shot(tmp_path, 0), _shot(tmp_path, 1)]
    wrong_axis = replace(shots[0], camera_b=np.array([2.0, 10.0, 1.0]))
    with pytest.raises(ContinuityRefusal, match="180-degree"):
        run_verified_episode_continuity_pipeline(
            2, [wrong_axis, shots[1]], _bible(), tmp_path / "axis"
        )
    unreachable = replace(shots[0], ik_effector_target=np.array([4.0, 0.0, 0.0]))
    with pytest.raises(ContinuityRefusal, match="exceeds total chain"):
        run_verified_episode_continuity_pipeline(
            2, [unreachable, shots[1]], _bible(), tmp_path / "ik"
        )
    other_rate = tmp_path / "ambience-other-rate.wav"
    _write_mono_wav(other_rate, np.zeros(4096), sample_rate=16000)
    mismatch = replace(shots[0], ambience_wav_path=other_rate)
    with pytest.raises(ContinuityRefusal, match="format mismatch"):
        run_verified_episode_continuity_pipeline(
            2, [mismatch, shots[1]], _bible(), tmp_path / "audio"
        )
    stereo = tmp_path / "stereo.wav"
    with wave.open(str(stereo), "wb") as writer:
        writer.setnchannels(2)
        writer.setsampwidth(2)
        writer.setframerate(8000)
        writer.writeframes(np.zeros((4096, 2), dtype="<i2").tobytes())
    malformed = replace(shots[0], dialogue_wav_path=stereo)
    with pytest.raises(ContinuityRefusal, match="mono PCM16"):
        run_verified_episode_continuity_pipeline(
            2, [malformed, shots[1]], _bible(), tmp_path / "stereo"
        )


def test_show_bible_nonfinite_and_numeric_refusal(tmp_path: Path) -> None:
    bible = _bible()
    obs = ShowBibleShotObservation(
        0, 0, bible.cranial_landmarks_m, 1.8, bible.cepstral_reference_envelope, bible.skin_tone_lab
    )
    with pytest.raises(ContinuityRefusal, match="malformed"):
        verify_cross_episode_show_bible(
            bible,
            [
                replace(
                    obs,
                    cranial_landmarks_m=tuple(
                        (np.nan, 0.0, 0.0) for _ in bible.cranial_landmarks_m
                    ),
                )
            ],
        )
    with pytest.raises(ContinuityRefusal, match="negative"):
        verify_cross_episode_show_bible(bible, [replace(obs, episode_index=-1)])


def test_episode_handlers_and_studio_port_manifest_binding(tmp_path: Path) -> None:
    from nexus_ai_agent.creative.chronos import build_default_chronos_handlers
    from nexus_ai_agent.creative.chronos.chronos_port import ChronosCommand, ChronosPort

    bible = _bible()
    shots = [_shot(tmp_path, 0), _shot(tmp_path, 1, 700.0)]
    manifest_shots = []
    for shot in shots:
        record = {}
        for item in fields(shot):
            value = getattr(shot, item.name)
            if isinstance(value, np.ndarray):
                value = value.tolist()
            elif isinstance(value, Path):
                value = str(value)
            elif item.name == "foot_timestamps":
                value = [
                    {
                        "pts": stamp.pts,
                        "time_base_num": stamp.time_base.numerator,
                        "time_base_den": stamp.time_base.denominator,
                    }
                    for stamp in value
                ]
            elif isinstance(value, tuple):
                value = list(value)
            record[item.name] = value
        manifest_shots.append(record)
    payload = {
        "episode_index": 9,
        "show_bible": {
            "character_id": bible.character_id,
            "cranial_landmarks_m": bible.cranial_landmarks_m,
            "metric_height_m": bible.metric_height_m,
            "cepstral_reference_envelope": bible.cepstral_reference_envelope,
            "skin_tone_lab": bible.skin_tone_lab,
        },
        "shots": manifest_shots,
    }
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    port = ChronosPort(build_default_chronos_handlers())
    output = tmp_path / "passport.json"
    receipt = port.execute(
        ChronosCommand(
            "episode.run_verified",
            (str(manifest),),
            {"output": str(output), "staging_dir": str(tmp_path / "port-stage")},
        )
    )
    passport = json.loads(output.read_text(encoding="utf-8"))
    assert passport["all_invariants_verified"] is True
    assert len(passport["published_png_sha256s"]) == 2
    assert receipt.metadata["merkle_root_sha256"] == passport["merkle_root_sha256"]

    bible_payload = {
        "show_bible": payload["show_bible"],
        "observations": [
            {
                "episode_index": 9,
                "shot_index": i,
                "cranial_landmarks_m": bible.cranial_landmarks_m,
                "metric_height_m": 1.8,
                "cepstral_envelope": bible.cepstral_reference_envelope,
                "skin_tone_lab": bible.skin_tone_lab,
            }
            for i in range(2)
        ],
    }
    bible_manifest, bible_output = tmp_path / "bible.json", tmp_path / "bible-result.json"
    bible_manifest.write_text(json.dumps(bible_payload), encoding="utf-8")
    result = port.execute(
        ChronosCommand(
            "episode.verify_show_bible",
            (str(bible_manifest),),
            {"output": str(bible_output)},
        )
    )
    assert json.loads(bible_output.read_text(encoding="utf-8"))["observations_checked"] == 2
    assert result.quality_claim == "SHOW_BIBLE_CONTINUITY"


def test_wave7_episode_pipeline_benchmark_n3(tmp_path: Path) -> None:
    import time

    shots = [_shot(tmp_path, 0, 450.0), _shot(tmp_path, 1, 650.0)]
    durations_ms: list[float] = []
    roots: list[str] = []
    output_bytes: list[int] = []
    for repeat in range(3):
        started = time.perf_counter()
        passport = run_verified_episode_continuity_pipeline(
            8, shots, _bible(), tmp_path / f"benchmark-{repeat}"
        )
        durations_ms.append((time.perf_counter() - started) * 1000)
        assert passport.all_invariants_verified
        roots.append(passport.merkle_root_sha256)
        output_bytes.append(
            sum(
                path.stat().st_size
                for path in (tmp_path / f"benchmark-{repeat}").rglob("*")
                if path.is_file()
            )
        )
    assert len(set(roots)) == 1
    print(
        {
            "wave7_episode_pipeline_n": 3,
            "median_ms": float(np.median(durations_ms)),
            "max_ms": max(durations_ms),
            "staging_and_publication_bytes": output_bytes,
            "deterministic_merkle_root": roots[0],
        }
    )
