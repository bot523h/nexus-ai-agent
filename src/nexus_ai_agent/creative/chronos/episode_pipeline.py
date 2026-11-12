"""Fail-closed show-bible continuity checks and a small disk-backed episode runner."""

from __future__ import annotations

import hashlib
import math
import wave
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from fractions import Fraction
from pathlib import Path
from typing import cast

import numpy as np
from numpy.typing import NDArray

from . import (
    acoustics_lipsync,
    audio_continuity,
    compositor_25d,
    geometry,
    identity_lock,
    merkle_ledger,
    timeline,
    weather_and_grading,
)
from .biomechanics_motion import enforce_stance_foot_lock, solve_two_bone_or_chain_ik
from .color_science import read_rgb8_png
from .contracts import Continuity, KernelError, RationalTimestamp

FloatArray = NDArray[np.float64]


class ContinuityRefusal(KernelError):
    """Typed fail-closed refusal with machine-readable invariant failures."""

    def __init__(self, reasons: Sequence[str]) -> None:
        self.reasons = tuple(reasons)
        super().__init__("episode continuity refused: " + "; ".join(self.reasons))


@dataclass(frozen=True, slots=True)
class ShowBibleCharacterBible:
    character_id: str
    cranial_landmarks_m: tuple[tuple[float, float, float], ...]
    metric_height_m: float
    cepstral_reference_envelope: tuple[float, ...]
    skin_tone_lab: tuple[float, float, float]


@dataclass(frozen=True, slots=True)
class ShowBibleShotObservation:
    episode_index: int
    shot_index: int
    cranial_landmarks_m: tuple[tuple[float, float, float], ...]
    metric_height_m: float
    cepstral_envelope: tuple[float, ...]
    skin_tone_lab: tuple[float, float, float]


@dataclass(frozen=True, slots=True)
class ShowBibleVerificationReceipt:
    character_id: str
    observations_checked: int
    max_cranial_rms_m: float
    max_height_drift_m: float
    max_cepstral_rms: float
    max_skin_delta_e: float


@dataclass(frozen=True, slots=True)
class ShotDAG:
    continuity: Continuity = Continuity.OK
    previous_input_hashes: Mapping[str, str] = field(default_factory=dict)
    current_input_hashes: Mapping[str, str] = field(default_factory=dict)
    dependencies: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    def evaluate_delta(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        if self.continuity is Continuity.UNKNOWN:
            raise ContinuityRefusal(("ShotDAG continuity is UNKNOWN",))
        if self.continuity is Continuity.FAIL:
            raise ContinuityRefusal(("ShotDAG continuity is FAIL",))
        changed = set(self.previous_input_hashes) ^ set(self.current_input_hashes)
        changed.update(
            key
            for key, value in self.current_input_hashes.items()
            if self.previous_input_hashes.get(key) != value
        )
        invalidated = set(changed)
        pending = deque(changed)
        while pending:
            node = pending.popleft()
            for dependent in self.dependencies.get(node, ()):
                if dependent not in invalidated:
                    invalidated.add(dependent)
                    pending.append(dependent)
        return tuple(sorted(changed)), tuple(sorted(invalidated))


@dataclass(frozen=True, slots=True)
class EpisodeShotInput:
    shot_index: int
    scene_linear_rgb: FloatArray
    alpha: FloatArray
    depth_m: float
    canonical_landmarks_m: FloatArray
    observed_landmarks_3d_m: FloatArray
    observed_landmarks_px: FloatArray
    intrinsics: FloatArray
    camera_depth_m: float
    top_px: tuple[float, float]
    bottom_px: tuple[float, float]
    camera_a: FloatArray
    camera_b: FloatArray
    actor_a: FloatArray
    actor_b: FloatArray
    gaze_a: FloatArray
    gaze_b: FloatArray
    ik_root_position: FloatArray
    ik_segment_lengths_m: tuple[float, ...]
    ik_effector_target: FloatArray
    foot_timestamps: tuple[RationalTimestamp, ...]
    ankle_positions_m: FloatArray
    stance_contact: NDArray[np.bool_]
    cepstral_envelope: tuple[float, ...]
    skin_tone_lab: tuple[float, float, float]
    dialogue_wav_path: Path
    ambience_wav_path: Path
    speaker_position_m: tuple[float, float, float]
    camera_position_m: tuple[float, float, float]
    camera_right_unit: tuple[float, float, float] = (1.0, 0.0, 0.0)
    room_dimensions_m: tuple[float, float, float] = (6.0, 5.0, 3.0)
    room_absorption: float = 0.35


@dataclass(frozen=True, slots=True)
class EpisodeContinuityPassport:
    episode_index: int
    character_id: str
    shot_count: int
    merkle_root_sha256: str
    published_wav_path: str
    published_wav_sha256: str
    published_png_paths: tuple[str, ...]
    published_png_sha256s: tuple[str, ...]
    changed_inputs: tuple[str, ...]
    invalidated_nodes: tuple[str, ...]
    show_bible_receipt: ShowBibleVerificationReceipt
    all_invariants_verified: bool


def verify_cross_episode_show_bible(
    bible: ShowBibleCharacterBible,
    observations: Sequence[ShowBibleShotObservation],
    *,
    cranial_rms_tolerance_m: float = 0.005,
    height_tolerance_m: float = 0.02,
    cepstral_rms_tolerance: float = 2.0,
    skin_delta_e_tolerance: float = 2.0,
) -> ShowBibleVerificationReceipt:
    """Compare metric cranial geometry, height, voice envelope, and CIE LAB per shot."""
    canonical = np.asarray(bible.cranial_landmarks_m, dtype=np.float64)
    voice = np.asarray(bible.cepstral_reference_envelope, dtype=np.float64)
    lab = np.asarray(bible.skin_tone_lab, dtype=np.float64)
    if (
        not bible.character_id
        or canonical.ndim != 2
        or canonical.shape[1] != 3
        or len(canonical) < 4
        or voice.ndim != 1
        or voice.size == 0
        or lab.shape != (3,)
        or np.any(~np.isfinite(canonical))
        or np.any(~np.isfinite(voice))
        or np.any(~np.isfinite(lab))
        or not np.isfinite(bible.metric_height_m)
        or bible.metric_height_m <= 0
        or min(
            cranial_rms_tolerance_m,
            height_tolerance_m,
            cepstral_rms_tolerance,
            skin_delta_e_tolerance,
        )
        < 0
    ):
        raise ContinuityRefusal(("invalid canonical show-bible record",))
    if not observations:
        raise ContinuityRefusal(("show-bible verification requires at least one shot",))
    seen: set[tuple[int, int]] = set()
    max_cranial = max_height = max_voice = max_skin = 0.0
    reasons: list[str] = []
    for item in observations:
        key = (item.episode_index, item.shot_index)
        if min(key) < 0 or key in seen:
            raise ContinuityRefusal(("duplicate or negative episode/shot index",))
        seen.add(key)
        landmarks = np.asarray(item.cranial_landmarks_m, dtype=np.float64)
        envelope = np.asarray(item.cepstral_envelope, dtype=np.float64)
        shot_lab = np.asarray(item.skin_tone_lab, dtype=np.float64)
        if (
            landmarks.shape != canonical.shape
            or envelope.shape != voice.shape
            or shot_lab.shape != (3,)
            or np.any(~np.isfinite(landmarks))
            or np.any(~np.isfinite(envelope))
            or np.any(~np.isfinite(shot_lab))
            or not np.isfinite(item.metric_height_m)
        ):
            raise ContinuityRefusal((f"shot {key} has malformed show-bible measurements",))
        cranial = float(np.sqrt(np.mean((landmarks - canonical) ** 2)))
        height = abs(item.metric_height_m - bible.metric_height_m)
        vocal = float(np.sqrt(np.mean((envelope - voice) ** 2)))
        skin = float(np.linalg.norm(shot_lab - lab))
        max_cranial, max_height = max(max_cranial, cranial), max(max_height, height)
        max_voice, max_skin = max(max_voice, vocal), max(max_skin, skin)
        if cranial > cranial_rms_tolerance_m:
            reasons.append(f"shot {key} cranial RMS drift {cranial:.6g} m")
        if height > height_tolerance_m:
            reasons.append(f"shot {key} height drift {height:.6g} m")
        if vocal > cepstral_rms_tolerance:
            reasons.append(f"shot {key} cepstral drift {vocal:.6g}")
        if skin > skin_delta_e_tolerance:
            reasons.append(f"shot {key} skin LAB delta {skin:.6g}")
    if reasons:
        raise ContinuityRefusal(reasons)
    return ShowBibleVerificationReceipt(
        bible.character_id, len(observations), max_cranial, max_height, max_voice, max_skin
    )


def _read_mono_pcm16(path: Path) -> tuple[FloatArray, int]:
    try:
        with wave.open(str(path), "rb") as reader:
            if (
                reader.getcomptype() != "NONE"
                or reader.getnchannels() != 1
                or reader.getsampwidth() != 2
            ):
                raise ContinuityRefusal((f"{path.name} must be uncompressed mono PCM16",))
            rate, count = reader.getframerate(), reader.getnframes()
            payload = reader.readframes(count)
    except (wave.Error, OSError, EOFError) as exc:
        raise ContinuityRefusal((f"invalid WAV {path.name}: {exc}",)) from exc
    if rate <= 0 or count <= 0 or len(payload) != count * 2:
        raise ContinuityRefusal((f"empty or truncated WAV {path.name}",))
    return np.frombuffer(payload, dtype="<i2").astype(np.float64) / 32768.0, rate


def _write_stereo_pcm16(path: Path, samples: FloatArray, sample_rate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pcm = np.rint(
        np.clip(
            samples,
            -1.0,
            0.999969,
        )
        * 32768.0
    ).astype("<i2")
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(2)
        writer.setsampwidth(2)
        writer.setframerate(sample_rate)
        writer.writeframes(pcm.reshape(-1, 2).tobytes())


def _fingerprint_shot(shot: EpisodeShotInput) -> str:
    digest = hashlib.sha256()
    try:
        digest.update(np.ascontiguousarray(shot.scene_linear_rgb).tobytes())
        digest.update(np.ascontiguousarray(shot.observed_landmarks_px).tobytes())
        digest.update(shot.dialogue_wav_path.read_bytes())
        digest.update(shot.ambience_wav_path.read_bytes())
    except OSError as exc:
        raise ContinuityRefusal((f"shot {shot.shot_index} source asset is unavailable",)) from exc
    return digest.hexdigest()


def _verify_show_data(
    bible: ShowBibleCharacterBible,
    shots: Sequence[EpisodeShotInput],
    episode_index: int,
    heights: Mapping[int, float],
) -> ShowBibleVerificationReceipt:
    observations = [
        ShowBibleShotObservation(
            episode_index,
            shot.shot_index,
            cast(
                tuple[tuple[float, float, float], ...],
                tuple(tuple(float(v) for v in row) for row in shot.observed_landmarks_3d_m),
            ),
            heights[shot.shot_index],
            shot.cepstral_envelope,
            shot.skin_tone_lab,
        )
        for shot in shots
    ]
    return verify_cross_episode_show_bible(bible, observations)


def run_verified_episode_continuity_pipeline(
    episode_index: int,
    shots: Sequence[EpisodeShotInput],
    show_bible: ShowBibleCharacterBible,
    staging_dir: str | Path,
    *,
    shot_dag: ShotDAG | None = None,
    lut: weather_and_grading.CubeLUT | None = None,
    height_tolerance_m: float = 0.05,
    identity_mahalanobis_limit: float = 5.0,
    timbre_distance_limit_db: float = 3.0,
) -> EpisodeContinuityPassport:
    """Run geometry/continuity checks, persist PNG/WAV masters, and seal every receipt."""
    if episode_index < 0 or len(shots) < 2:
        raise ContinuityRefusal(("episode needs a nonnegative index and at least two shots",))
    ordered = tuple(sorted(shots, key=lambda item: item.shot_index))
    if len({shot.shot_index for shot in ordered}) != len(ordered) or ordered[0].shot_index < 0:
        raise ContinuityRefusal(("episode shot indices must be unique and nonnegative",))
    stage = Path(staging_dir).resolve()
    stage.mkdir(parents=True, exist_ok=True)
    hashes = {f"shot:{shot.shot_index}": _fingerprint_shot(shot) for shot in ordered}
    dependencies = {
        key: (f"identity:{key}", f"png:{key}", f"audio:{key}", "episode:merkle") for key in hashes
    }
    if shot_dag is None:
        effective_dag = ShotDAG(Continuity.OK, {}, hashes, dependencies)
    else:
        if shot_dag.current_input_hashes and dict(shot_dag.current_input_hashes) != hashes:
            raise ContinuityRefusal(("ShotDAG current fingerprints do not match shot inputs",))
        effective_dag = replace(
            shot_dag,
            current_input_hashes=hashes,
            dependencies=shot_dag.dependencies or dependencies,
        )
    changed_inputs, invalidated_nodes = effective_dag.evaluate_delta()

    heights: dict[int, float] = {}
    geometry_receipts: dict[int, tuple[identity_lock.IdentityVerificationReceipt, float]] = {}
    for shot in ordered:
        local_canonical = np.asarray(shot.canonical_landmarks_m, dtype=np.float64)
        show_canonical = np.asarray(show_bible.cranial_landmarks_m, dtype=np.float64)
        if (
            local_canonical.shape != show_canonical.shape
            or not np.all(np.isfinite(local_canonical))
            or float(np.sqrt(np.mean((local_canonical - show_canonical) ** 2))) > 0.005
        ):
            raise ContinuityRefusal((f"shot {shot.shot_index} local cranial bible drift",))
        identity = identity_lock.verify_pose_invariant_identity(
            np.asarray(show_bible.cranial_landmarks_m),
            shot.observed_landmarks_px,
            shot.intrinsics,
            shot.camera_depth_m,
        )
        if (
            not identity.identity_locked
            or identity.mahalanobis_distance > identity_mahalanobis_limit
        ):
            raise ContinuityRefusal((f"shot {shot.shot_index} identity/GUM check failed",))
        scale = geometry.backproject_character_height_m(
            shot.top_px, shot.bottom_px, shot.camera_depth_m, shot.intrinsics
        )
        if abs(scale.world_height_m - show_bible.metric_height_m) > height_tolerance_m:
            raise ContinuityRefusal((f"shot {shot.shot_index} cross-lens height drift",))
        eyeline = identity_lock.verify_eyeline_and_180_rule(
            shot.camera_a, shot.camera_b, shot.actor_a, shot.actor_b, shot.gaze_a, shot.gaze_b
        )
        if not eyeline.accepted or eyeline.skew_ray_closest_distance_m > 0.12:
            raise ContinuityRefusal((f"shot {shot.shot_index} 180-degree/skew-ray check failed",))
        try:
            ik = solve_two_bone_or_chain_ik(
                shot.ik_root_position, shot.ik_segment_lengths_m, shot.ik_effector_target
            )
        except KernelError as exc:
            raise ContinuityRefusal(
                (f"shot {shot.shot_index} effector IK rejected: {exc}",)
            ) from exc
        if not ik.reachable:
            raise ContinuityRefusal((f"shot {shot.shot_index} effector IK residual",))
        foot = enforce_stance_foot_lock(
            shot.foot_timestamps, shot.ankle_positions_m, shot.stance_contact
        )
        if foot.max_foot_slip_mm_after != 0.0:
            raise ContinuityRefusal((f"shot {shot.shot_index} stance foot slip",))
        heights[shot.shot_index] = scale.world_height_m
        geometry_receipts[shot.shot_index] = (identity, scale.world_height_m)

    show_receipt = _verify_show_data(show_bible, ordered, episode_index, heights)
    png_paths: list[Path] = []
    png_hashes: list[str] = []
    shot_receipts: list[dict[str, object]] = []
    for shot in ordered:
        image = weather_and_grading.apply_asc_cdl_and_3d_lut(shot.scene_linear_rgb, lut=lut)
        legal = weather_and_grading.legalize_broadcast_rgb_ebu_r103(image)
        if legal.illegal_pixel_fraction_after != 0.0 or legal.sobel_structure_correlation < 0.99:
            raise ContinuityRefusal((f"shot {shot.shot_index} broadcast color QC failed",))
        output_png = stage / f"episode-{episode_index}-shot-{shot.shot_index}.png"
        compositor_receipt = compositor_25d.render_25d_multiplane_composite(
            [(legal.image, shot.alpha, shot.depth_m)],
            np.zeros_like(legal.image),
            output_png,
            beta=0.0,
            focus=(shot.depth_m, 50.0, 2.8, 36.0, shot.scene_linear_rgb.shape[1]),
            tile_size_px=32,
        )
        persisted = read_rgb8_png(output_png)
        if persisted.shape != legal.image.shape:
            raise ContinuityRefusal(
                (f"shot {shot.shot_index} PNG persistence verification failed",)
            )
        png_paths.append(output_png)
        png_hashes.append(compositor_receipt.sha256)
        pose_receipt, measured_height = geometry_receipts[shot.shot_index]
        shot_receipts.append(
            {
                "episode": episode_index,
                "shot": shot.shot_index,
                "png_sha256": compositor_receipt.sha256,
                "identity_mahalanobis": pose_receipt.mahalanobis_distance,
                "height_m": measured_height,
                "foot_slip_after_mm": 0.0,
            }
        )

    audio_sample_rate: int | None = None
    rendered_paths: list[Path] = []
    rendered_frame_counts: list[int] = []
    reference_voice: FloatArray | None = None
    jaw_lags: list[float] = []
    duck_gains_db: list[float] = []
    for shot in ordered:
        dry, sample_rate = _read_mono_pcm16(shot.dialogue_wav_path)
        ambience, ambience_rate = _read_mono_pcm16(shot.ambience_wav_path)
        if ambience_rate != sample_rate or ambience.shape != dry.shape:
            raise ContinuityRefusal((f"shot {shot.shot_index} dialogue/ambience format mismatch",))
        if audio_sample_rate is not None and sample_rate != audio_sample_rate:
            raise ContinuityRefusal(("episode WAV sample rates differ",))
        audio_sample_rate = sample_rate
        if reference_voice is None:
            reference_voice = dry
        else:
            timbre = acoustics_lipsync.measure_cepstral_timbre_consistency(
                reference_voice, dry, sample_rate
            )
            if (
                timbre.status != "OK"
                or timbre.mean_distance_db is None
                or timbre.mean_distance_db > timbre_distance_limit_db
            ):
                raise ContinuityRefusal(
                    (f"shot {shot.shot_index} cepstral timbre consistency failed",)
                )
        ducked, snr_gain = audio_continuity.duck_ambience_under_dialogue(dry, ambience, sample_rate)
        duck_gains_db.append(snr_gain)
        mono_mix = np.clip(dry + ducked, -1.0, 0.999969)
        stereo_ir, _ = acoustics_lipsync.synthesize_stereo_room_ir(
            np.asarray(shot.room_dimensions_m),
            np.asarray(shot.speaker_position_m),
            np.asarray(shot.camera_position_m),
            np.asarray(shot.camera_right_unit),
            sample_rate,
            shot.room_absorption,
            max_order=1,
        )
        spatial, _ = acoustics_lipsync.render_spatial_dialogue(mono_mix, stereo_ir)
        output_wav = stage / f"episode-{episode_index}-shot-{shot.shot_index}-spatial.wav"
        _write_stereo_pcm16(output_wav, spatial, sample_rate)
        rendered_paths.append(output_wav)
        with wave.open(str(output_wav), "rb") as reader:
            rendered_frame_counts.append(reader.getnframes())
        step = max(1, len(dry) // 48)
        chunks = [dry[index : index + step] for index in range(0, len(dry), step)]
        envelope = np.asarray([math.sqrt(float(np.mean(chunk**2))) for chunk in chunks])
        peak = max(float(envelope.max(initial=0.0)), 1e-12)
        targets = np.clip(envelope / peak, 0.0, 1.0)
        jaw_samples = [
            (RationalTimestamp(index, Fraction(1, 24)), float(value))
            for index, value in enumerate(targets)
        ]
        jaw = acoustics_lipsync.solve_jaw_trajectory(jaw_samples, natural_frequency_hz=8.0)
        if abs(jaw.peak_lag_frames) > 3:
            raise ContinuityRefusal((f"shot {shot.shot_index} jaw-viseme timing drift",))
        jaw_lags.append(jaw.peak_lag_frames)

    if audio_sample_rate is None:
        raise ContinuityRefusal(("episode contains no dialogue audio",))
    assembled_wav = stage / f"episode-{episode_index}-assembled.wav"
    assembly = timeline.assemble_pcm_wav_stream_copy(
        rendered_paths, assembled_wav, stage / f"episode-{episode_index}-wav-stage"
    )
    boundaries: list[int] = []
    total_frames = 0
    for count in rendered_frame_counts[:-1]:
        total_frames += count
        boundaries.append(total_frames)
    published_wav = stage / f"episode-{episode_index}-published.wav"
    audio_receipt = audio_continuity.declick_pcm_wav_splices(
        assembled_wav, published_wav, boundaries, bridge_frames=32
    )
    published_wav_hash = hashlib.sha256(published_wav.read_bytes()).hexdigest()
    if audio_receipt["sample_frames"] != assembly.total_sample_frames:
        raise ContinuityRefusal(("lossless WAV assembly/de-click changed sample count",))

    merkle_receipts: list[Mapping[str, object]] = [
        {
            "episode": episode_index,
            "character_id": show_bible.character_id,
            "published_wav_sha256": published_wav_hash,
            "shot_count": len(ordered),
        },
        *shot_receipts,
    ]
    root = merkle_ledger.merkle_root(merkle_receipts)
    for leaf_index, leaf in enumerate(merkle_receipts):
        proof = merkle_ledger.build_inclusion_proof(merkle_receipts, leaf_index)
        if not merkle_ledger.verify_inclusion_proof(leaf, proof, root):
            raise ContinuityRefusal((f"Merkle inclusion proof failed for leaf {leaf_index}",))
    root_hex = root.hex()
    (stage / f"episode-{episode_index}-merkle-root.txt").write_text(root_hex, encoding="ascii")
    if len(jaw_lags) != len(ordered) or len(duck_gains_db) != len(ordered):
        raise ContinuityRefusal(("dialogue/jaw receipts incomplete",))
    return EpisodeContinuityPassport(
        episode_index,
        show_bible.character_id,
        len(ordered),
        root_hex,
        str(published_wav),
        published_wav_hash,
        tuple(str(path) for path in png_paths),
        tuple(png_hashes),
        changed_inputs,
        invalidated_nodes,
        show_receipt,
        True,
    )
