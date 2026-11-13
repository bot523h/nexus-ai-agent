from __future__ import annotations

import ast
import hashlib
import inspect
import math
import textwrap
import wave
from collections.abc import Callable
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from types import ModuleType
from typing import Any

import numpy as np
import pytest

from nexus_ai_agent.creative.chronos import (
    acoustics_lipsync,
    audio_continuity,
    biomechanics_motion,
    build_default_chronos_handlers,
    color_science,
    compositor_25d,
    episode_pipeline,
    geometry,
    identity_lock,
    merkle_ledger,
    scene_portrait_ops,
    temporal_optics,
    timeline,
    weather_and_grading,
)
from nexus_ai_agent.creative.chronos.contracts import RationalTimestamp
from nexus_ai_agent.creative.studio.chronos_port import (
    ChronosCommand,
    ChronosExecutionReceipt,
    ChronosPort,
)


@dataclass(frozen=True)
class MutationCase:
    label: str
    module: ModuleType
    function_name: str
    original_fragment: str
    mutant_fragment: str
    assertion_check: Callable[[], None]


def _resolve_target(module: ModuleType, qualified_name: str) -> tuple[Any, str]:
    components = qualified_name.split(".")
    parent: Any = module
    for component in components[:-1]:
        parent = getattr(parent, component)
    return parent, components[-1]


def _function_ast_mutant(
    module: ModuleType, function_name: str, old: str, new: str
) -> Callable[..., Any]:
    parent, attribute = _resolve_target(module, function_name)
    original = getattr(parent, attribute)
    source = textwrap.dedent(inspect.getsource(original))
    if source.count(old) != 1:
        raise AssertionError(
            f"{module.__name__}.{function_name}: expected exactly one target fragment, "
            f"found {source.count(old)}: {old!r}"
        )
    mutated_source = source.replace(old, new, 1)
    tree = ast.parse(mutated_source)
    if len(tree.body) != 1 or not isinstance(tree.body[0], (ast.FunctionDef, ast.AsyncFunctionDef)):
        raise AssertionError("mutation target did not parse as exactly one function")
    ast.fix_missing_locations(tree)
    namespace = module.__dict__
    sentinel = object()
    prior_binding = namespace.get(attribute, sentinel)
    try:
        exec(compile(tree, inspect.getsourcefile(original) or "<mutation>", "exec"), namespace)
        mutated = namespace[attribute]
    finally:
        if prior_binding is sentinel:
            namespace.pop(attribute, None)
        else:
            namespace[attribute] = prior_binding
    if mutated is original:
        raise AssertionError("AST mutation did not replace the function object")
    return mutated


def _assert_result(call: Callable[[], Any], predicate: Callable[[Any], bool], message: str) -> None:
    try:
        result = call()
    except Exception as exc:
        raise AssertionError(
            f"{message}: mutated operation raised {type(exc).__name__}: {exc}"
        ) from exc
    assert predicate(result), message


def _write_wav(path: Path, samples: np.ndarray, rate: int = 8000) -> None:
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(np.asarray(samples, dtype="<i2").tobytes())


def _season_shot_assets(
    root: Path,
) -> tuple[episode_pipeline.ShowBibleCharacterBible, list[episode_pipeline.EpisodeShotInput]]:
    bible = episode_pipeline.ShowBibleCharacterBible(
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
    landmarks = np.asarray(bible.cranial_landmarks_m, dtype=np.float64)
    frames = 1024
    samples = np.rint(0.2 * np.sin(2 * np.pi * 180 * np.arange(frames) / 8000) * 32767).astype(
        "<i2"
    )
    ambience = np.rint(0.03 * np.sin(2 * np.pi * 60 * np.arange(frames) / 8000) * 32767).astype(
        "<i2"
    )
    y, x = np.mgrid[0:16, 0:20]
    image = np.stack((0.2 + 0.01 * x + 0.004 * y, 0.18 + 0.008 * x, 0.16 + 0.006 * y), axis=-1)
    shots: list[episode_pipeline.EpisodeShotInput] = []
    for index in range(2):
        focal, depth = (400.0, 2.0) if index == 0 else (700.0, 2.0)
        k = np.array([[focal, 0.0, 8.0], [0.0, focal, 8.0], [0.0, 0.0, 1.0]])
        pixels = np.column_stack(
            (8 + focal * landmarks[:, 0] / depth, 8 + focal * landmarks[:, 1] / depth)
        )
        dialogue, bed = root / f"mutant-dialogue-{index}.wav", root / f"mutant-bed-{index}.wav"
        _write_wav(dialogue, samples)
        _write_wav(bed, ambience)
        shots.append(
            episode_pipeline.EpisodeShotInput(
                shot_index=index,
                scene_linear_rgb=image.copy(),
                alpha=np.ones((16, 20)),
                depth_m=4.0,
                canonical_landmarks_m=landmarks.copy(),
                observed_landmarks_3d_m=landmarks.copy(),
                observed_landmarks_px=pixels,
                intrinsics=k,
                camera_depth_m=depth,
                top_px=(8.0, 8.0),
                bottom_px=(8.0, 8.0 + focal * 1.8 / depth),
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
                dialogue_wav_path=dialogue,
                ambience_wav_path=bed,
                speaker_position_m=(2.0, 2.0, 1.2),
                camera_position_m=(2.5, 2.0, 1.2),
            )
        )
    return bible, shots


def _identity_fixture() -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    points = np.array(
        [
            [-0.06, -0.05, 0.00],
            [0.06, -0.05, 0.00],
            [-0.05, 0.05, 0.00],
            [0.05, 0.05, 0.00],
            [-0.025, 0.0, 0.015],
            [0.025, 0.0, -0.015],
        ],
        dtype=np.float64,
    )
    intrinsics = np.array([[800.0, 0.0, 32.0], [0.0, 800.0, 32.0], [0.0, 0.0, 1.0]])
    observed = np.column_stack((32 + points[:, 0] * 400, 32 + points[:, 1] * 400))
    return points, intrinsics, observed


def test_all_default_handler_names_are_registered_and_dispatchable() -> None:
    handlers = build_default_chronos_handlers()
    assert set(handlers) == set(ChronosPort.OPERATIONS)
    assert all(callable(handler) for handler in handlers.values())
    calls: list[str] = []

    def dispatch_spy(command: ChronosCommand) -> ChronosExecutionReceipt:
        calls.append(command.operation)
        return ChronosExecutionReceipt(
            command.operation, "in-memory", "a" * 64, "MUTATION_AUDIT", {}
        )

    port = ChronosPort({name: dispatch_spy for name in ChronosPort.OPERATIONS})
    for name in sorted(ChronosPort.OPERATIONS):
        receipt = port.execute(ChronosCommand(name, ("synthetic-input",), {}))
        assert receipt.operation == name and receipt.sha256 == "a" * 64
    assert set(calls) == set(ChronosPort.OPERATIONS)
    assert len(calls) == len(ChronosPort.OPERATIONS)
    assert {name.split(".", 1)[0] for name in calls} >= {
        "timeline",
        "geometry",
        "color",
        "acoustics",
        "audio",
        "identity",
        "compositor",
        "scene",
        "merkle",
        "motion",
        "temporal",
        "weather",
        "grading",
        "episode",
    }


def test_chronos_mutation_campaign_kills_40_of_40(tmp_path: Path) -> None:
    points, k, observed = _identity_fixture()
    image = np.zeros((5, 5, 3), dtype=np.float64)
    image[2, 2] = 1.0
    rng = np.random.default_rng(2026)
    phase_reference = rng.normal(size=(64, 64))
    phase_moving = np.roll(phase_reference, 3, axis=1)
    tiny_ref = np.random.default_rng(2026).normal(size=(4, 4))
    tiny_move = np.roll(tiny_ref, 1, axis=1)
    tone_input = np.repeat(np.array([0.0, 0.1, 0.3, 1.0, 2.0])[:, None, None], 3, axis=2)
    pde_image = np.arange(75, dtype=np.float64).reshape(5, 5, 3) / 75
    pde_mask = np.zeros((5, 5), dtype=bool)
    pde_mask[2, 2] = True
    retouch_image = np.zeros((9, 9, 3), dtype=np.float64)
    retouch_image[:, 4:, :] = 0.8
    retouch_mask = np.ones((9, 9), dtype=bool)
    rolling_frame = np.broadcast_to(np.arange(8, dtype=np.float64), (4, 8)).copy()
    handover_tail = np.linspace(-1.0, 1.0, 64, dtype=np.float64)
    wet_signal = np.r_[np.full(100, 12000, dtype=np.int16), np.full(100, -12000, dtype=np.int16)]
    clip_path = tmp_path / "declik-source.wav"
    _write_wav(clip_path, wet_signal)
    bible = episode_pipeline.ShowBibleCharacterBible(
        "mira",
        tuple(tuple(float(x) for x in row) for row in points),
        1.8,
        (0.1, 0.2),
        (55.0, 15.0, 20.0),
    )
    registry = episode_pipeline.ShowBibleRegistry()
    registry.register(bible)
    conflicting_bible = episode_pipeline.ShowBibleCharacterBible(
        "mira",
        bible.cranial_landmarks_m,
        1.9,
        bible.cepstral_reference_envelope,
        bible.skin_tone_lab,
    )
    season_bible, season_shots = _season_shot_assets(tmp_path)
    run_counter = 0

    def _run_small_episode() -> episode_pipeline.EpisodeContinuityPassport:
        nonlocal run_counter
        run_counter += 1
        return episode_pipeline.run_verified_episode_continuity_pipeline(
            1, season_shots, season_bible, tmp_path / f"mutation-merkle-{run_counter}"
        )

    def _timeline_ok() -> Any:
        source_a, source_b = tmp_path / "timeline-a.wav", tmp_path / "timeline-b.wav"
        _write_wav(source_a, np.arange(128, dtype=np.int16))
        _write_wav(source_b, np.arange(96, dtype=np.int16))
        output = tmp_path / f"timeline-out-{run_counter}-{time_token()}.wav"
        return timeline.assemble_pcm_wav_stream_copy(
            [source_a, source_b], output, tmp_path / f"timeline-stage-{time_token()}"
        )

    cases: list[MutationCase] = [
        MutationCase(
            "1 Rodrigues SO(3) skew sign",
            geometry,
            "rotation_matrix_from_axis_angle",
            "np.sin(angle_rad) * skew",
            "-np.sin(angle_rad) * skew",
            lambda: _assert_result(
                lambda: (
                    geometry.rotation_matrix_from_axis_angle(np.array([0.0, 0.0, 1.0]), math.pi / 2)
                    @ np.array([1.0, 0.0, 0.0])
                ),
                lambda result: np.allclose(result, [0.0, 1.0, 0.0], atol=1e-12),
                "positive Rodrigues angle must rotate x toward y",
            ),
        ),
        MutationCase(
            "2 skew-ray closest-approach denominator",
            identity_lock,
            "verify_eyeline_and_180_rule",
            "abs(float(np.dot(origin_delta, direction_cross))) / cross_norm",
            "abs(float(np.dot(origin_delta, direction_cross))) / (cross_norm**2)",
            lambda: _assert_result(
                lambda: (
                    identity_lock.verify_eyeline_and_180_rule(
                        np.array([-2.0, 0.0, 0.0]),
                        np.array([-2.0, 1.0, 0.0]),
                        np.array([0.0, 0.0, 0.0]),
                        np.array([0.0, 1.0, 1.0]),
                        np.array([2.0, 0.0, 0.0]),
                        np.array([0.0, 3.0, 0.0]),
                    ).skew_ray_closest_distance_m
                ),
                lambda result: result == pytest.approx(1.0),
                "skew-ray distance uses a single cross-product norm denominator",
            ),
        ),
        MutationCase(
            "3 GUM depth sensitivity sign",
            identity_lock,
            "verify_pose_invariant_identity",
            "jacobian[:2, 2] = (x, y)",
            "jacobian[:2, 2] = (-x, -y)",
            lambda: _assert_result(
                lambda: (
                    identity_lock.verify_pose_invariant_identity(
                        points,
                        observed + np.array([[2.0, -1.0]] * len(points)),
                        k,
                        2.0,
                        pixel_sigma_px=0.2,
                        depth_sigma_m=0.05,
                    ).mahalanobis_distance
                ),
                lambda result: result == pytest.approx(0.5863841571716959, abs=1e-8),
                "GUM depth-sensitivity sign preserves the reference Mahalanobis score",
            ),
        ),
        MutationCase(
            "4 Huber IRLS outlier weight",
            identity_lock,
            "verify_pose_invariant_identity",
            "weights = np.minimum(1.0, huber_delta_mm / np.maximum(residual_mm, 1e-12))",
            "weights = np.ones_like(residual_mm)",
            lambda: _assert_result(
                lambda: (
                    identity_lock.verify_pose_invariant_identity(
                        points,
                        observed + np.array([[0.0, 0.0]] * 5 + [[80.0, -60.0]]),
                        k,
                        2.0,
                        huber_delta_mm=2.0,
                    ).downweighted_landmark_count
                ),
                lambda result: result >= 1,
                "Huber IRLS must downweight the injected gross landmark outlier",
            ),
        ),
        MutationCase(
            "5 Kabsch reflection guard",
            identity_lock,
            "verify_pose_invariant_identity",
            "fix[-1, -1] = np.linalg.det(u @ vt)",
            "fix[-1, -1] = 1.0",
            lambda: _assert_result(
                lambda: np.linalg.det(
                    np.asarray(
                        identity_lock.verify_pose_invariant_identity(
                            points,
                            np.column_stack((32 - points[:, 0] * 400, 32 + points[:, 1] * 400)),
                            k,
                            2.0,
                            tolerance_mm=1000,
                        ).rotation_so3
                    )
                ),
                lambda result: result == pytest.approx(1.0, abs=1e-8),
                "Kabsch must return a proper rotation rather than a reflection",
            ),
        ),
        MutationCase(
            "6 Procrustes scale denominator",
            identity_lock,
            "verify_pose_invariant_identity",
            "np.sum(singular * np.diag(fix)) / denom",
            "np.sum(singular * np.diag(fix)) / (denom * 2.0)",
            lambda: _assert_result(
                lambda: (
                    identity_lock.verify_pose_invariant_identity(points, observed, k, 2.0).scale
                ),
                lambda result: result == pytest.approx(0.995950041062209, abs=1e-8),
                "metric Procrustes fit preserves the reference scale normalization",
            ),
        ),
        MutationCase(
            "7 cranial/inter-pupillary baseline threshold",
            identity_lock,
            "verify_cranial_baseline",
            "drift <= relative_tolerance",
            "drift <= relative_tolerance / 10.0",
            lambda: _assert_result(
                lambda: (
                    identity_lock.verify_cranial_baseline(
                        points,
                        points * np.array([1.04, 1.0, 1.0]),
                        left_index=0,
                        right_index=1,
                        relative_tolerance=0.08,
                    ).within_tolerance
                ),
                bool,
                "four-percent inter-pupillary baseline drift is within eight-percent contract",
            ),
        ),
        MutationCase(
            "8 identity drift verdict comparison",
            identity_lock,
            "verify_pose_invariant_identity",
            "rms <= tolerance_mm",
            "rms >= tolerance_mm",
            lambda: _assert_result(
                lambda: (
                    identity_lock.verify_pose_invariant_identity(
                        points, observed, k, 2.0
                    ).identity_locked
                ),
                bool,
                "exact identity fit must remain locked",
            ),
        ),
        MutationCase(
            "9 Planckian-locus temperature exponent",
            color_science,
            "cct_kelvin_to_xyz",
            "-0.2661239e9 / temperature**3",
            "-0.2661239e9 / temperature**2",
            lambda: _assert_result(
                lambda: color_science.cct_kelvin_to_xyz(3200.0),
                lambda result: np.allclose(result, (1.0604, 1.0, 0.4454), atol=0.001),
                "Planckian 3200 K chromaticity remains on the expected locus",
            ),
        ),
        MutationCase(
            "10 CIEDE2000 rotation-term sign",
            color_science,
            "delta_e_2000",
            "x * x + y * y + z * z + rt * y * z",
            "x * x + y * y + z * z - rt * y * z",
            lambda: _assert_result(
                lambda: color_science.delta_e_2000(
                    np.array([50.0, 2.6772, -79.7751]), np.array([50.0, 0.0, -82.7485])
                ),
                lambda result: float(result) == pytest.approx(2.0425, abs=0.0001),
                "CIEDE2000 Sharma-Wu-Dalal reference pair",
            ),
        ),
        MutationCase(
            "11 Beer-Lambert depth transmittance",
            compositor_25d,
            "_composite",
            "np.exp(-beta_rgb * depth)",
            "np.exp(beta_rgb * depth)",
            lambda: _assert_result(
                lambda: compositor_25d._composite(
                    [(np.ones((4, 4, 3)), np.ones((4, 4)), 2.0)],
                    np.zeros((4, 4, 3)),
                    0.2,
                    (2.0, 50.0, 2.8, 36.0, 4),
                )[1, 1, 0],
                lambda result: result == pytest.approx(math.exp(-0.4)),
                "Beer-Lambert transmittance attenuates with depth",
            ),
        ),
        MutationCase(
            "12 premultiplied alpha transmittance",
            compositor_25d,
            "_composite",
            "out = scene * mask[..., None] + out * (1 - mask[..., None])",
            "out = scene * mask[..., None] + out * (1 + mask[..., None])",
            lambda: _assert_result(
                lambda: compositor_25d._composite(
                    [(np.full((4, 4, 3), 0.2), np.full((4, 4), 0.5), 2.0)],
                    np.full((4, 4, 3), 0.1),
                    0.0,
                    (2.0, 50.0, 2.8, 36.0, 4),
                )[1, 1, 0],
                lambda result: result == pytest.approx(0.15),
                "front-to-back alpha should preserve transparent black contribution",
            ),
        ),
        MutationCase(
            "13 coupled PDE harmonic stencil center weight",
            scene_portrait_ops,
            "inpaint_masked_region_pde",
            "harmonic = axial / 4",
            "harmonic = axial / 3",
            lambda: _assert_result(
                lambda: scene_portrait_ops.inpaint_masked_region_pde(
                    pde_image, pde_mask, iterations=1, tolerance=1e-12, relaxation=1.0
                ).image[2, 2, 0],
                lambda result: result == pytest.approx(0.48),
                "one-sweep PDE center must match four-neighbor harmonic stencil",
            ),
        ),
        MutationCase(
            "14 red halation Gaussian decay sign",
            scene_portrait_ops,
            "tonemap_scene_linear_filmic",
            "np.exp(-(coords**2) / max(1.0, radius**2 / 2))",
            "np.exp((coords**2) / max(1.0, radius**2 / 2))",
            lambda: _assert_result(
                lambda: scene_portrait_ops.tonemap_scene_linear_filmic(
                    image, halation_strength=0.5, halation_threshold=0.1, halation_radius_px=2
                ),
                lambda result: result[2, 1, 0] > 0.05,
                "red halation kernel preserves a decaying but nonzero adjacent highlight spread",
            ),
        ),
        MutationCase(
            "15 monotonic log-exposure shoulder denominator",
            scene_portrait_ops,
            "tonemap_scene_linear_filmic",
            "log_exposure + 1.0 / log_exposure_slope",
            "log_exposure - 1.0 / log_exposure_slope",
            lambda: _assert_result(
                lambda: scene_portrait_ops.tonemap_scene_linear_filmic(tone_input),
                lambda result: 0.0 < result[2, 0, 0] < 1.0,
                "intermediate log exposure must map to an interior shoulder value",
            ),
        ),
        MutationCase(
            "16 bilateral skin-retouch range Gaussian sign",
            scene_portrait_ops,
            "retouch_portrait_frequency_separation",
            "np.exp(-((grad / 0.08) ** 2))",
            "np.exp(((grad / 0.08) ** 2))",
            lambda: _assert_result(
                lambda: scene_portrait_ops.retouch_portrait_frequency_separation(
                    retouch_image, retouch_mask, attenuation=0.0
                )[4, 4, 0],
                lambda result: result == pytest.approx(0.8),
                "edge-preserving range weights must not blend across the luminance step",
            ),
        ),
        MutationCase(
            "17 Sabine reverberation constant",
            acoustics_lipsync,
            "sabine_rt60",
            "0.161 * volume_m3",
            "1.61 * volume_m3",
            lambda: _assert_result(
                lambda: acoustics_lipsync.sabine_rt60(120.0, 150.0, 0.3),
                lambda result: result == pytest.approx(0.161 * 120 / (150 * 0.3)),
                "Sabine RT60 uses the SI 0.161 seconds-per-metre constant",
            ),
        ),
        MutationCase(
            "18 image-source inverse-distance attenuation",
            acoustics_lipsync,
            "shoebox_rir",
            "fractional_impulse(delay, len(rir)) * decay / distance",
            "fractional_impulse(delay, len(rir)) * decay * distance",
            lambda: _assert_result(
                lambda: acoustics_lipsync.shoebox_rir(
                    np.array([1.0, 1.0, 1.0]),
                    np.array([4.0, 1.0, 1.0]),
                    np.array([9.0, 8.0, 7.0]),
                    0.3,
                    8000,
                    0,
                ),
                lambda result: float(np.max(np.abs(result))) < 0.5,
                "direct sound amplitude decays with propagation distance",
            ),
        ),
        MutationCase(
            "19 raised-cosine de-click window polarity",
            audio_continuity,
            "declick_pcm_wav_splices",
            "np.cos(t * np.pi / 2)",
            "-np.cos(t * np.pi / 2)",
            lambda: _assert_result(
                lambda: audio_continuity.declick_pcm_wav_splices(
                    clip_path,
                    tmp_path / f"declick-polarity-{time_token()}.wav",
                    [100],
                    bridge_frames=32,
                ),
                lambda result: result["max_slope_jump_after"] < 1e-3,
                "raised-cosine bridge bounds the on-disk splice slope discontinuity",
            ),
        ),
        MutationCase(
            "20 real-disk WAV boundary blend weight",
            audio_continuity,
            "declick_pcm_wav_splices",
            "t = np.linspace(0.0, 1.0, 2 * half + 2)",
            "t = np.linspace(0.0, 2.0 * np.pi, 2 * half + 2)",
            lambda: _assert_result(
                lambda: audio_continuity.declick_pcm_wav_splices(
                    clip_path,
                    tmp_path / f"declick-window-{time_token()}.wav",
                    [100],
                    bridge_frames=32,
                ),
                lambda result: result["max_slope_jump_after"] < 1e-3,
                "on-disk raised-cosine window maintains a smooth blend slope",
            ),
        ),
        MutationCase(
            "21 integer timeline sample-count off-by-one",
            timeline,
            "assemble_pcm_wav_stream_copy",
            "expected_frames = sum(counts)",
            "expected_frames = sum(counts) + 1",
            lambda: _assert_result(
                _timeline_ok,
                lambda result: result.total_sample_frames == 224,
                "stream-copy assembly conserves the exact sum of input sample frames",
            ),
        ),
        MutationCase(
            "22 RFC 6962 leaf domain prefix",
            merkle_ledger,
            "leaf_hash",
            'hashlib.sha256(b"\\x00" + data).digest()',
            'hashlib.sha256(b"\\x01" + data).digest()',
            lambda: _assert_result(
                lambda: merkle_ledger.leaf_hash({"episode": 1}),
                lambda result: result == hashlib.sha256(b"\x00" + b'{"episode":1}').digest(),
                "RFC 6962 leaves use the 0x00 domain prefix",
            ),
        ),
        MutationCase(
            "23 Merkle odd-node promotion",
            merkle_ledger,
            "_root",
            "if len(hashes) == 1:\n        return hashes[0]",
            "if len(hashes) == 1:\n        return node_hash(hashes[0], hashes[0])",
            lambda: _assert_result(
                lambda: merkle_ledger.merkle_root([{"x": 1}, {"x": 2}, {"x": 3}]),
                lambda result: result == _reference_merkle_root([{"x": 1}, {"x": 2}, {"x": 3}]),
                "odd Merkle subtrees promote their unpaired leaf without rehashing it",
            ),
        ),
        MutationCase(
            "24 real-disk WAV verifier sample-rate signature",
            timeline,
            "_wav_signature",
            "reader.getframerate(),",
            "reader.getframerate() * reader.getnchannels() * reader.getsampwidth(),",
            lambda: _assert_result(
                lambda: _wav_signature_from_disk(clip_path),
                lambda result: result == (1, 2, 8000, "NONE"),
                "disk verifier preserves the PCM sample-rate signature, not byte-rate units",
            ),
        ),
        MutationCase(
            "25 damped least-squares IK damping matrix sign",
            biomechanics_motion,
            "solve_two_bone_or_chain_ik",
            "jacobian @ jacobian.T + damping**2 * np.eye(2)",
            "jacobian @ jacobian.T - damping**2 * np.eye(2)",
            lambda: _assert_result(
                lambda: (
                    biomechanics_motion.solve_two_bone_or_chain_ik(
                        np.zeros(3),
                        (0.8, 0.7, 0.4),
                        np.array([1.0, 0.8, 0.0]),
                        damping=0.3,
                        tolerance_m=1e-5,
                    ).target_residual_m
                ),
                lambda result: result < 1e-5,
                "positive DLS damping converges a three-bone chain to its reachable target",
            ),
        ),
        MutationCase(
            "26 bone-length preservation projection",
            biomechanics_motion,
            "simulate_secondary_verlet_chain",
            "current[segment + 1] = current[segment] + delta * rest_length",
            "current[segment + 1] = current[segment] + delta * (rest_length * 1.1)",
            lambda: _assert_result(
                lambda: (
                    biomechanics_motion.simulate_secondary_verlet_chain(
                        (
                            RationalTimestamp(0, Fraction(1, 24)),
                            RationalTimestamp(1, Fraction(1, 24)),
                        ),
                        np.zeros((2, 3)),
                        np.array([[0.0, 0.0, 0.0], [0.0, 0.0, 1.0]]),
                        gravity_m_s2=(0.0, 0.0, 0.0),
                    ).max_segment_strain
                ),
                lambda result: result < 1e-12,
                "final PBD projection preserves rest length exactly",
            ),
        ),
        MutationCase(
            "27 stance foot-lock transition weight bounds",
            biomechanics_motion,
            "enforce_stance_foot_lock",
            "                weight = 3 * t**2 - 2 * t**3\n                result[frame, :2] = (1 - weight) * anchor",
            "                weight = 2.0\n                result[frame, :2] = (1 - weight) * anchor",
            lambda: _assert_result(
                lambda: (
                    biomechanics_motion.enforce_stance_foot_lock(
                        tuple(RationalTimestamp(i, Fraction(1, 24)) for i in range(3)),
                        np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [2.0, 0.0, 0.0]]),
                        np.array([False, True, False]),
                        transition_frames=1,
                    ).positions
                ),
                lambda result: all(0.0 <= row[0] <= 2.0 for row in result),
                "stance-transition weights stay inside the endpoint convex hull",
            ),
        ),
        MutationCase(
            "28 symplectic Verlet acceleration sign",
            biomechanics_motion,
            "simulate_secondary_verlet_chain",
            "current + velocity + (gravity + wind) * dt**2",
            "current + velocity - (gravity + wind) * dt**2",
            lambda: _assert_result(
                lambda: biomechanics_motion.simulate_secondary_verlet_chain(
                    (RationalTimestamp(0, Fraction(1, 1)), RationalTimestamp(1, Fraction(1, 1))),
                    np.zeros((2, 3)),
                    np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0]]),
                    gravity_m_s2=(0.0, 0.0, -1.0),
                    drag=0.0,
                ).positions[-1][1][2],
                lambda result: result < -0.5,
                "Verlet acceleration follows the signed gravity vector",
            ),
        ),
        MutationCase(
            "29 rotary shutter duty-cycle ratio",
            biomechanics_motion,
            "render_shutter_motion_blur_linear",
            "fraction = shutter_angle_deg / 360.0",
            "fraction = 360.0 / shutter_angle_deg",
            lambda: _assert_result(
                lambda: (
                    biomechanics_motion.render_shutter_motion_blur_linear(
                        np.ones((8, 8, 3)), (2.0, 0.0), shutter_angle_deg=180.0
                    ).shutter_fraction
                ),
                lambda result: result == pytest.approx(0.5),
                "180-degree rotary shutter exposes for one half frame",
            ),
        ),
        MutationCase(
            "30 FFT phase-correlation cross-power conjugate",
            temporal_optics,
            "estimate_subpixel_translation_phase_corr",
            "cross_spectrum = fa * np.conj(fb)",
            "cross_spectrum = fa * fb",
            lambda: _assert_result(
                lambda: (
                    temporal_optics.estimate_subpixel_translation_phase_corr(
                        phase_reference, phase_moving
                    ).translation_x_px
                ),
                lambda result: result == pytest.approx(3.0, abs=0.2),
                "phase correlation returns the known three-pixel translation",
            ),
        ),
        MutationCase(
            "31 subpixel parabolic peak interpolation sign",
            temporal_optics,
            "estimate_subpixel_translation_phase_corr",
            "return 0.5 * (left - right) / denominator if abs(denominator) > 1e-15 else 0.0",
            "return 0.5 * (right - left) / denominator if abs(denominator) > 1e-15 else 0.0",
            lambda: _assert_result(
                lambda: (
                    temporal_optics.estimate_subpixel_translation_phase_corr(
                        tiny_ref, tiny_move
                    ).translation_x_px
                ),
                lambda result: result == pytest.approx(-1.128569801, abs=1e-6),
                "subpixel estimator sign follows reference-to-moving displacement convention",
            ),
        ),
        MutationCase(
            "32 temporal deflicker forward-backward symmetry",
            temporal_optics,
            "_zero_phase_smooth",
            'np.pad(values, (radius, radius), mode="reflect")',
            'np.pad(values, (2 * radius, 0), mode="reflect")',
            lambda: _assert_result(
                lambda: temporal_optics._zero_phase_smooth(np.array([0.0, 0.0, 1.0, 0.0, 0.0]), 1),
                lambda result: np.allclose(result, result[::-1], atol=1e-12),
                "zero-phase smoothing preserves time-reversal symmetry around an impulse",
            ),
        ),
        MutationCase(
            "33 CMOS rolling-shutter row skew sign",
            temporal_optics,
            "rectify_rolling_shutter_skew",
            "sample_x = np.clip(columns + dx, 0, width - 1)",
            "sample_x = np.clip(columns - dx, 0, width - 1)",
            lambda: _assert_result(
                lambda: temporal_optics.rectify_rolling_shutter_skew(
                    rolling_frame, (2.0, 0.0), readout_duration_s=1.0, frame_period_s=1.0
                ).frame[0, 3],
                lambda result: result == pytest.approx(2.0),
                "top scanline is inverse-warped by the signed readout-time displacement",
            ),
        ),
        MutationCase(
            "34 precipitation shutter streak-length scaling",
            weather_and_grading,
            "render_physical_precipitation_layer",
            "exposure_s = shutter_angle_deg / 360.0 / frame_rate_fps",
            "exposure_s = 360.0 / shutter_angle_deg / frame_rate_fps",
            lambda: _assert_result(
                lambda: (
                    weather_and_grading.render_physical_precipitation_layer(
                        20,
                        20,
                        400.0,
                        4.0,
                        8.0,
                        frame_rate_fps=24.0,
                        shutter_angle_deg=180.0,
                        particle_count=1,
                        seed=3,
                    ).vertical_streak_length_px
                ),
                lambda result: result == pytest.approx(400 * 8 * (0.5 / 24) / 4),
                "rain streak length scales with shutter exposure fraction",
            ),
        ),
        MutationCase(
            "35 ASC-CDL slope/offset/power order",
            weather_and_grading,
            "apply_asc_cdl_and_3d_lut",
            "rgb * slope_array + offset_array",
            "(rgb + offset_array) * slope_array",
            lambda: _assert_result(
                lambda: weather_and_grading.apply_asc_cdl_and_3d_lut(
                    np.array([[[0.2, 0.2, 0.2]]]),
                    slope=(2.0, 2.0, 2.0),
                    offset=(0.1, 0.1, 0.1),
                    power=(1.0, 1.0, 1.0),
                )[0, 0, 0],
                lambda result: result == pytest.approx(0.5),
                "ASC-CDL applies slope before offset and power after their sum",
            ),
        ),
        MutationCase(
            "36 trilinear LUT red-axis corner-weight branch",
            weather_and_grading,
            "_apply_lut",
            "(flat_fraction[:, 0] if red else 1 - flat_fraction[:, 0])",
            "(1 - flat_fraction[:, 0] if red else flat_fraction[:, 0])",
            lambda: _assert_result(
                lambda: weather_and_grading._apply_lut(
                    np.array([[[0.23, 0.51, 0.77]]]), weather_and_grading.identity_cube_lut(2)
                )[0, 0],
                lambda result: np.allclose(result, [0.23, 0.51, 0.77], atol=1e-12),
                "identity LUT is invariant under its trilinear lattice interpolation",
            ),
        ),
        MutationCase(
            "37 EBU R103 upper legal-signal excursion bound",
            weather_and_grading,
            "legalize_broadcast_rgb_ebu_r103",
            "if low_luma < 0.0 or high_luma > 1.0:",
            "if low_luma < 0.0 or high_luma > 0.5:",
            lambda: _assert_result(
                lambda: (
                    weather_and_grading.legalize_broadcast_rgb_ebu_r103(
                        np.repeat(np.linspace(0.2, 0.8, 16).reshape(4, 4, 1), 3, axis=2)
                    ).image
                ),
                lambda result: np.allclose(
                    result,
                    np.repeat(np.linspace(0.2, 0.8, 16).reshape(4, 4, 1), 3, axis=2),
                    atol=1e-12,
                ),
                "legalizer is identity on already-legal in-gamut signal values",
            ),
        ),
        MutationCase(
            "38 ShowBibleRegistry conflict-registration guard",
            episode_pipeline,
            "ShowBibleRegistry.register",
            "if existing is not None and existing != bible:",
            "if False:",
            lambda: _assert_registry_conflict_refused(bible, conflicting_bible),
        ),
        MutationCase(
            "39 cross-shot audio phase handover state",
            audio_continuity,
            "align_audio_phase_handover",
            "inverted = correlation <= -minimum_correlation",
            "inverted = correlation >= minimum_correlation",
            lambda: _assert_result(
                lambda: audio_continuity.align_audio_phase_handover(
                    handover_tail, -handover_tail, window_frames=64
                ),
                lambda result: result.polarity_inverted and result.correlation_after > 0.99,
                "audio phase-polarity handover propagates the previous shot tail state",
            ),
        ),
        MutationCase(
            "40 episode Merkle-manifest verification guard",
            episode_pipeline,
            "run_verified_episode_continuity_pipeline",
            "if not merkle_ledger.verify_inclusion_proof(leaf, proof, root):",
            "if merkle_ledger.verify_inclusion_proof(leaf, proof, root):",
            lambda: _assert_result(
                _run_small_episode,
                lambda result: (
                    result.all_invariants_verified and len(result.merkle_root_sha256) == 64
                ),
                "untampered episode manifest leaves pass every inclusion-proof check",
            ),
        ),
    ]
    assert len(cases) == 40
    killed: list[str] = []
    for case in cases:
        # Prove the oracle accepts the unmutated production function before mutation.
        case.assertion_check()
        parent, attribute = _resolve_target(case.module, case.function_name)
        original = getattr(parent, attribute)
        try:
            mutant = _function_ast_mutant(
                case.module, case.function_name, case.original_fragment, case.mutant_fragment
            )
            setattr(parent, attribute, mutant)
            try:
                case.assertion_check()
            except AssertionError:
                killed.append(case.label)
            else:
                raise AssertionError(f"surviving mutant: {case.label}")
        finally:
            setattr(parent, attribute, original)
    assert len(killed) == 40, (
        f"surviving mutants: {sorted(set(c.label for c in cases) - set(killed))}"
    )
    assert len(set(killed)) == 40


def _assert_registry_conflict_refused(
    first: episode_pipeline.ShowBibleCharacterBible,
    second: episode_pipeline.ShowBibleCharacterBible,
) -> None:
    registry = episode_pipeline.ShowBibleRegistry()
    registry.register(first)
    try:
        registry.register(second)
    except episode_pipeline.ContinuityRefusal:
        return
    raise AssertionError("conflicting character registration was accepted")


def _reference_merkle_root(receipts: list[dict[str, int]]) -> bytes:
    leaves = [
        hashlib.sha256(b"\x00" + merkle_ledger.canonical_receipt_bytes(item)).digest()
        for item in receipts
    ]
    first_pair = hashlib.sha256(b"\x01" + leaves[0] + leaves[1]).digest()
    return hashlib.sha256(b"\x01" + first_pair + leaves[2]).digest()


def _wav_signature_from_disk(path: Path) -> tuple[int, int, int, str]:
    with wave.open(str(path), "rb") as reader:
        return timeline._wav_signature(reader)


def time_token() -> str:
    # A monotone-enough local token that keeps repeat real-disk writes distinct.
    import time

    return f"{time.time_ns()}"
