"""Fail-closed bridge from deterministic Chronos renders into the NEXUS spine.

The adapter owns no queue, database, or second filesystem boundary. Callers
inject the authoritative fencing-token reader; all bytes are staged below the
workspace through :class:`WorkspaceFilesystem`, re-read and verified, then the
whole attempt directory is atomically renamed into the immutable publication
namespace.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
import subprocess
import wave
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any, Literal, TypeAlias, cast

import numpy as np
from numpy.typing import NDArray
from PIL import Image

from nexus_ai_agent.creative.chronos import (
    acoustic_space_continuity,
    color_science,
    merkle_ledger,
    multicam_epipolar_matching,
)
from nexus_ai_agent.creative.chronos.acoustic_space_continuity import AcousticRoomSpec
from nexus_ai_agent.creative.chronos.multicamera_mastering import (
    MultiCameraMasteringReceipt,
    MultiCameraShotInput,
    execute_multicamera_mastering,
)
from nexus_ai_agent.creative.chronos.real_media_continuity import (
    CharacterIdentityAnchor,
    RealMediaSeasonReceipt,
    RealMediaShot,
    canonical_sample_horizon,
    execute_real_media_season_continuity,
    measure_patch_delta_e00_by_frame,
)
from nexus_ai_agent.execution import ExecutionIdentity
from nexus_ai_agent.tools.filesystem_policy import (
    FilesystemBoundaryError,
    WorkspaceFilesystem,
)

FloatArray: TypeAlias = NDArray[np.float64]
OperationName: TypeAlias = Literal["chronos.real_media_continuity", "chronos.omniverse_mastering"]
JsonValue: TypeAlias = "None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]"

ENGINE_VERSION = "chronos-omniverse-1.0"
REAL_MEDIA_OPERATION: OperationName = "chronos.real_media_continuity"
OMNIVERSE_OPERATION: OperationName = "chronos.omniverse_mastering"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_COMPONENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_MAX_SAFE_INTEGER = (1 << 53) - 1


class ChronosSpineExecutionError(RuntimeError):
    """The staged execution could not be safely completed."""


class ChronosVerificationRefusalError(ChronosSpineExecutionError):
    """Private bytes failed independent verification or the attempt was stale."""


@dataclass(frozen=True, slots=True)
class ChronosSpineAttempt:
    """Immutable caller intent for one fenced real-media execution attempt."""

    project_id: str
    parent_revision_id: str
    attempt_id: str
    fencing_token: int
    operation_name: OperationName
    parameters: Mapping[str, object]
    real_media_shots: tuple[RealMediaShot, ...] = ()
    multicamera_shots: tuple[MultiCameraShotInput, ...] = ()
    show_bible_anchor: CharacterIdentityAnchor | None = None
    room: AcousticRoomSpec | None = None
    speaker_position_m: FloatArray | None = None


@dataclass(frozen=True, slots=True)
class ChronosRecipeIdentity:
    """JCS/SHA-256 identity of inputs, normalized render parameters, and lineage."""

    project_id: str
    parent_revision_id: str
    attempt_id: str
    fencing_token: int
    input_media_digests: tuple[str, ...]
    operation_name: OperationName
    normalized_parameters_json: str
    engine_version: str
    recipe_digest: str

    @property
    def parameters(self) -> dict[str, JsonValue]:
        """Return a detached JSON value so callers cannot mutate this identity."""
        value = json.loads(self.normalized_parameters_json)
        if not isinstance(value, dict):
            raise ChronosSpineExecutionError("normalized recipe parameters are not an object")
        return cast(dict[str, JsonValue], value)


@dataclass(frozen=True, slots=True)
class ChronosFileVerification:
    """One artifact measured from staged bytes and (where relevant) decoded bytes."""

    relative_path: str
    sha256: str
    size_bytes: int
    decoded_content_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class ChronosVerificationReceipt:
    """Independent post-disk measurements required before publication."""

    verification_tier: Literal["REAL-DISK"]
    files: tuple[ChronosFileVerification, ...]
    merkle_root: str
    sample_drift: int
    max_delta_e00: float
    epipolar_sampson_error: float | None
    rt60_error: float | None


@dataclass(frozen=True, slots=True)
class ChronosAssetVersionRecord:
    """Immutable, measured Chronos asset-version lineage projection."""

    asset_version_id: str
    project_id: str
    parent_revision_id: str
    produced_revision_id: str
    attempt_id: str
    fencing_token: int
    recipe_digest: str
    merkle_root: str
    input_content_sha256: str
    output_content_sha256: str
    published_file_digests: tuple[tuple[str, str], ...]
    verification_tier: Literal["REAL-DISK"]
    verified_at_utc: str
    sample_drift: int
    max_delta_e00: float
    epipolar_sampson_error: float | None
    rt60_error: float | None
    published_path: str

    def to_dict(self) -> dict[str, object]:
        """Return canonical JSON-compatible lineage data."""
        return {
            "asset_version_id": self.asset_version_id,
            "project_id": self.project_id,
            "parent_revision_id": self.parent_revision_id,
            "produced_revision_id": self.produced_revision_id,
            "attempt_id": self.attempt_id,
            "fencing_token": self.fencing_token,
            "recipe_digest": self.recipe_digest,
            "merkle_root": self.merkle_root,
            "input_content_sha256": self.input_content_sha256,
            "output_content_sha256": self.output_content_sha256,
            "published_file_digests": dict(self.published_file_digests),
            "verification_tier": self.verification_tier,
            "verified_at_utc": self.verified_at_utc,
            "sample_drift": self.sample_drift,
            "max_delta_e00": self.max_delta_e00,
            "epipolar_sampson_error": self.epipolar_sampson_error,
            "rt60_error": self.rt60_error,
            "published_path": self.published_path,
        }


@dataclass(frozen=True, slots=True)
class ChronosArtifactPassport:
    """Immutable evidence projection binding recipe, proof, and published bytes."""

    asset_version_id: str
    project_id: str
    parent_revision_id: str
    produced_revision_id: str
    attempt_id: str
    fencing_token: int
    recipe_digest: str
    merkle_root: str
    input_content_sha256: str
    output_content_sha256: str
    published_file_digests: tuple[tuple[str, str], ...]
    verification_tier: Literal["REAL-DISK"]
    verified_at_utc: str
    sample_drift: int
    max_delta_e00: float
    epipolar_sampson_error: float | None
    rt60_error: float | None
    passport_digest: str

    def to_dict(self) -> dict[str, object]:
        """Serialize the passport excluding its own digest from the digest input."""
        return {
            "asset_version_id": self.asset_version_id,
            "project_id": self.project_id,
            "parent_revision_id": self.parent_revision_id,
            "produced_revision_id": self.produced_revision_id,
            "attempt_id": self.attempt_id,
            "fencing_token": self.fencing_token,
            "recipe_digest": self.recipe_digest,
            "merkle_root": self.merkle_root,
            "input_content_sha256": self.input_content_sha256,
            "output_content_sha256": self.output_content_sha256,
            "published_file_digests": dict(self.published_file_digests),
            "verification_tier": self.verification_tier,
            "verified_at_utc": self.verified_at_utc,
            "sample_drift": self.sample_drift,
            "max_delta_e00": self.max_delta_e00,
            "epipolar_sampson_error": self.epipolar_sampson_error,
            "rt60_error": self.rt60_error,
            "passport_digest": self.passport_digest,
        }


@dataclass(frozen=True, slots=True)
class ChronosSpineExecutionReceipt:
    """Successful publish and its independent disk evidence."""

    recipe_identity: ChronosRecipeIdentity
    verification: ChronosVerificationReceipt
    asset_version: ChronosAssetVersionRecord
    artifact_passport: ChronosArtifactPassport
    engine_summary: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class _Snapshot:
    attempt: ChronosSpineAttempt
    input_media_digests: tuple[str, ...]


FencingTokenReader: TypeAlias = Callable[[str, str], int]
PreVerifyHook: TypeAlias = Callable[[Path], None]


def _component(value: object, name: str) -> str:
    if not isinstance(value, str) or not _COMPONENT_RE.fullmatch(value):
        raise ChronosSpineExecutionError(f"{name} must be a safe, non-empty path component")
    if value in {".", ".."}:
        raise ChronosSpineExecutionError(f"{name} must not be a traversal component")
    return value


def _lineage_token(value: object, name: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or any(char in value for char in ("/", "\\", "\x00"))
    ):
        raise ChronosSpineExecutionError(f"{name} must be a non-empty non-path identity")
    if value in {".", ".."}:
        raise ChronosSpineExecutionError(f"{name} must not be a traversal component")
    return value


def _float_jcs(value: float) -> str:
    """ECMAScript-compatible shortest binary64 spelling for RFC 8785 numbers."""
    if not math.isfinite(value):
        raise ChronosSpineExecutionError("JCS forbids NaN and infinite numbers")
    if value == 0.0:
        return "0"
    negative = value < 0
    raw = repr(abs(value)).lower()
    if "e" in raw:
        mantissa, exponent_text = raw.split("e", 1)
        exponent = int(exponent_text)
    else:
        mantissa, exponent = raw, 0
    whole, dot, fraction = mantissa.partition(".")
    digits = whole + fraction
    decimal_position = len(whole) + exponent
    while len(digits) > 1 and digits.startswith("0"):
        digits = digits[1:]
        decimal_position -= 1
    while len(digits) > 1 and digits.endswith("0"):
        digits = digits[:-1]
    magnitude = abs(value)
    if 1e-6 <= magnitude < 1e21:
        if decimal_position <= 0:
            body = "0." + ("0" * -decimal_position) + digits
        elif decimal_position >= len(digits):
            body = digits + ("0" * (decimal_position - len(digits)))
        else:
            body = digits[:decimal_position] + "." + digits[decimal_position:]
    else:
        coefficient = digits[0] + ("." + digits[1:] if len(digits) > 1 else "")
        scientific_exponent = decimal_position - 1
        sign = "+" if scientific_exponent >= 0 else ""
        body = f"{coefficient}e{sign}{scientific_exponent}"
    return ("-" if negative else "") + body


def jcs_canonical_json(value: object) -> str:
    """Canonicalize an I-JSON value per RFC 8785 (UTF-16 key order, binary64)."""
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        if abs(value) > _MAX_SAFE_INTEGER:
            raise ChronosSpineExecutionError("JCS integer exceeds the interoperable safe range")
        return str(value)
    if isinstance(value, float):
        return _float_jcs(value)
    if isinstance(value, str):
        try:
            value.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise ChronosSpineExecutionError("JCS strings cannot contain lone surrogates") from exc
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(jcs_canonical_json(item) for item in value) + "]"
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ChronosSpineExecutionError("JCS object keys must be strings")
        keys = sorted(
            cast(Sequence[str], tuple(value.keys())), key=lambda item: item.encode("utf-16be")
        )
        return (
            "{"
            + ",".join(
                jcs_canonical_json(key) + ":" + jcs_canonical_json(value[key]) for key in keys
            )
            + "}"
        )
    raise ChronosSpineExecutionError(f"unsupported JCS value type: {type(value).__name__}")


def _normalized_parameters(parameters: Mapping[str, object]) -> tuple[str, dict[str, JsonValue]]:
    try:
        canonical = jcs_canonical_json(parameters)
        parsed = json.loads(canonical)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        if isinstance(exc, ChronosSpineExecutionError):
            raise
        raise ChronosSpineExecutionError("render parameters are not canonical JSON") from exc
    if not isinstance(parsed, dict):
        raise ChronosSpineExecutionError("render parameters must be a JSON object")
    return canonical, cast(dict[str, JsonValue], parsed)


def _buffer_identity(value: object, label: str) -> dict[str, object]:
    array = np.asarray(value)
    raw = _array_bytes(array, label=label)
    return {
        "shape": [int(size) for size in array.shape],
        "float64_le_sha256": hashlib.sha256(raw).hexdigest(),
    }


def _identity_parameters(attempt: ChronosSpineAttempt) -> dict[str, JsonValue]:
    """Bind every non-media calibration/control input into normalized recipe data."""
    _, normalized = _normalized_parameters(attempt.parameters)
    identity: dict[str, object] = dict(normalized)
    anchor = attempt.show_bible_anchor
    if anchor is not None:
        identity["show_bible_anchor"] = {
            "character_id": anchor.character_id,
            "cranial_landmarks": _buffer_identity(anchor.cranial_landmarks_m, "anchor landmarks"),
            "reference_patch_linear_rgb": list(anchor.reference_patch_linear_rgb),
            "reference_illuminant_kelvin": anchor.reference_illuminant_kelvin,
            "left_landmark_index": anchor.left_landmark_index,
            "right_landmark_index": anchor.right_landmark_index,
            "calibration_roi": list(anchor.calibration_roi),
        }
    if attempt.operation_name == REAL_MEDIA_OPERATION:
        identity["shot_grid"] = [
            [shot.episode_index, shot.shot_index] for shot in attempt.real_media_shots
        ]
        identity["input_buffer_shapes"] = [
            {
                "frames": None
                if shot.frames_linear_rgb is None
                else _buffer_identity(shot.frames_linear_rgb, "frame input"),
                "landmarks": None
                if shot.cranial_landmarks_m is None
                else _buffer_identity(shot.cranial_landmarks_m, "landmark input"),
                "mono_pcm": None
                if shot.audio_pcm_float is None
                else _buffer_identity(shot.audio_pcm_float, "mono PCM input"),
                "stereo_pcm": None
                if shot.audio_pcm_stereo_float is None
                else _buffer_identity(shot.audio_pcm_stereo_float, "stereo PCM input"),
            }
            for shot in attempt.real_media_shots
        ]
    else:
        identity["multicamera_shots"] = [
            {
                "episode_index": shot.episode_index,
                "shot_index": shot.shot_index,
                "frame_rate_fps": shot.frame_rate_fps,
                "focal_length_mm": shot.focal_length_mm,
                "reference_focus_distance_mm": shot.reference_focus_distance_mm,
                "focus_distance_mm": shot.focus_distance_mm,
                "principal_point_px": list(shot.principal_point_px),
                "camera_velocity_px_per_frame": list(shot.camera_velocity_px_per_frame),
                "readout_fraction": shot.readout_fraction,
                "buffers": {
                    name: _buffer_identity(getattr(shot, name), name)
                    for name in (
                        "camera_a_frames_linear_rgb",
                        "camera_b_frames_linear_rgb",
                        "intrinsics_a",
                        "intrinsics_b",
                        "rotation_ab",
                        "translation_ab",
                        "points_a_px",
                        "points_b_px",
                        "source_sensor_chart_rgb",
                        "reference_chart_rgb",
                        "landmarks_3d",
                        "screen_velocities_px_s",
                        "camera_position_m",
                        "camera_to_world_rotation",
                        "dialogue_pcm_mono",
                    )
                },
            }
            for shot in attempt.multicamera_shots
        ]
        if attempt.room is not None:
            identity["room"] = {
                "room_id": attempt.room.room_id,
                "dimensions_m": list(attempt.room.dimensions_m),
                "wall_absorption": list(attempt.room.wall_absorption),
                "air_attenuation_per_m": attempt.room.air_attenuation_per_m,
            }
        if attempt.speaker_position_m is not None:
            identity["speaker_position_m"] = [float(value) for value in attempt.speaker_position_m]
    return cast(dict[str, JsonValue], identity)


def _array_bytes(value: object, *, label: str) -> bytes:
    array = np.asarray(value)
    if array.dtype.kind not in "fiu" or np.any(~np.isfinite(array)):
        raise ChronosSpineExecutionError(f"{label} must be a finite numeric buffer")
    try:
        normalized = np.ascontiguousarray(array, dtype=np.dtype("<f8"))
    except (TypeError, ValueError, OverflowError) as exc:
        raise ChronosSpineExecutionError(f"{label} cannot be normalized as float64") from exc
    return normalized.tobytes(order="C")


def _copied_array(value: FloatArray | None, *, label: str) -> FloatArray | None:
    if value is None:
        return None
    raw = _array_bytes(value, label=label)
    original = np.asarray(value)
    return np.frombuffer(raw, dtype="<f8").copy().reshape(original.shape).astype(np.float64)


def _snapshot_attempt(attempt: ChronosSpineAttempt) -> _Snapshot:
    project_id = _component(attempt.project_id, "project_id")
    parent = _lineage_token(attempt.parent_revision_id, "parent_revision_id")
    attempt_id = _component(attempt.attempt_id, "attempt_id")
    if (
        type(attempt.fencing_token) is not int
        or attempt.fencing_token < 1
        or attempt.fencing_token > _MAX_SAFE_INTEGER
    ):
        raise ChronosSpineExecutionError("fencing_token must be a positive safe integer")
    if attempt.operation_name not in {REAL_MEDIA_OPERATION, OMNIVERSE_OPERATION}:
        raise ChronosSpineExecutionError("unsupported Chronos spine operation")
    if not isinstance(attempt.parameters, Mapping):
        raise ChronosSpineExecutionError("parameters must be a mapping")
    parameters_json, normalized = _normalized_parameters(attempt.parameters)
    digests: list[str] = []
    real_shots: list[RealMediaShot] = []
    multi_shots: list[MultiCameraShotInput] = []
    if attempt.operation_name == REAL_MEDIA_OPERATION:
        if not attempt.real_media_shots:
            raise ChronosSpineExecutionError("real-media operation requires explicit shot inputs")
        if attempt.multicamera_shots:
            raise ChronosSpineExecutionError(
                "real-media attempt cannot also carry multicamera shots"
            )
        for shot_index, shot in enumerate(attempt.real_media_shots):
            frames = _copied_array(shot.frames_linear_rgb, label=f"shot {shot_index} frames")
            landmarks = _copied_array(
                shot.cranial_landmarks_m, label=f"shot {shot_index} landmarks"
            )
            mono = _copied_array(shot.audio_pcm_float, label=f"shot {shot_index} mono PCM")
            stereo = _copied_array(
                shot.audio_pcm_stereo_float, label=f"shot {shot_index} stereo PCM"
            )
            for value, label in (
                (frames, "frames"),
                (landmarks, "landmarks"),
                (mono, "mono PCM"),
                (stereo, "stereo PCM"),
            ):
                if value is not None:
                    digests.append(hashlib.sha256(_array_bytes(value, label=label)).hexdigest())
            real_shots.append(
                RealMediaShot(shot.episode_index, shot.shot_index, frames, landmarks, mono, stereo)
            )
    else:
        if len(attempt.multicamera_shots) < 2 or attempt.real_media_shots:
            raise ChronosSpineExecutionError(
                "Omniverse mastering requires at least two camera shots"
            )
        for shot_index, multi_shot in enumerate(attempt.multicamera_shots):
            copied: dict[str, object] = {}
            for name in (
                "camera_a_frames_linear_rgb",
                "camera_b_frames_linear_rgb",
                "intrinsics_a",
                "intrinsics_b",
                "rotation_ab",
                "translation_ab",
                "points_a_px",
                "points_b_px",
                "source_sensor_chart_rgb",
                "reference_chart_rgb",
                "landmarks_3d",
                "screen_velocities_px_s",
                "camera_position_m",
                "camera_to_world_rotation",
                "dialogue_pcm_mono",
            ):
                array = _copied_array(
                    cast(FloatArray, getattr(multi_shot, name)), label=f"shot {shot_index} {name}"
                )
                assert array is not None
                copied[name] = array
                if name in {
                    "camera_a_frames_linear_rgb",
                    "camera_b_frames_linear_rgb",
                    "points_a_px",
                    "points_b_px",
                    "landmarks_3d",
                    "dialogue_pcm_mono",
                }:
                    digests.append(hashlib.sha256(_array_bytes(array, label=name)).hexdigest())
            multi_shots.append(cast(MultiCameraShotInput, _replace_dataclass(multi_shot, copied)))
    if attempt.show_bible_anchor is None:
        raise ChronosSpineExecutionError(
            "post-disk color verification requires a Show-Bible anchor"
        )
    anchor = attempt.show_bible_anchor
    anchor_points = _copied_array(anchor.cranial_landmarks_m, label="anchor landmarks")
    assert anchor_points is not None
    digests.append(
        hashlib.sha256(_array_bytes(anchor_points, label="anchor landmarks")).hexdigest()
    )
    snapshot_anchor = CharacterIdentityAnchor(
        anchor.character_id,
        anchor_points,
        anchor.reference_patch_linear_rgb,
        anchor.reference_illuminant_kelvin,
        anchor.left_landmark_index,
        anchor.right_landmark_index,
        anchor.calibration_roi,
    )
    speaker = _copied_array(attempt.speaker_position_m, label="speaker position")
    snapshot = ChronosSpineAttempt(
        project_id,
        parent,
        attempt_id,
        attempt.fencing_token,
        attempt.operation_name,
        normalized,
        tuple(real_shots),
        tuple(multi_shots),
        snapshot_anchor,
        attempt.room,
        speaker,
    )
    return _Snapshot(snapshot, tuple(digests))


def _replace_dataclass(instance: object, changes: Mapping[str, object]) -> object:
    from dataclasses import replace

    return cast(object, replace(cast(Any, instance), **changes))


def compute_chronos_recipe_identity(attempt: ChronosSpineAttempt) -> ChronosRecipeIdentity:
    """Compute the RFC 8785 recipe digest from a stable copy of all input buffers."""
    snapshot = _snapshot_attempt(attempt)
    parameters = _identity_parameters(snapshot.attempt)
    normalized_json = jcs_canonical_json(parameters)
    payload: dict[str, object] = {
        "project_id": snapshot.attempt.project_id,
        "parent_revision_id": snapshot.attempt.parent_revision_id,
        "attempt_id": snapshot.attempt.attempt_id,
        "fencing_token": snapshot.attempt.fencing_token,
        "input_media_digests": list(snapshot.input_media_digests),
        "operation_name": snapshot.attempt.operation_name,
        "parameters": parameters,
        "engine_version": ENGINE_VERSION,
    }
    digest = hashlib.sha256(jcs_canonical_json(payload).encode("utf-8")).hexdigest()
    return ChronosRecipeIdentity(
        snapshot.attempt.project_id,
        snapshot.attempt.parent_revision_id,
        snapshot.attempt.attempt_id,
        snapshot.attempt.fencing_token,
        snapshot.input_media_digests,
        snapshot.attempt.operation_name,
        normalized_json,
        ENGINE_VERSION,
        digest,
    )


def _require_current_fence(attempt: ChronosSpineAttempt, token_reader: FencingTokenReader) -> None:
    try:
        current = token_reader(attempt.project_id, attempt.attempt_id)
    except Exception as exc:  # noqa: BLE001 - authority failures must refuse
        raise ChronosVerificationRefusalError("active fencing authority was unavailable") from exc
    if type(current) is not int or current != attempt.fencing_token:
        raise ChronosVerificationRefusalError("attempt fencing token is stale or malformed")


def _engine_run(
    attempt: ChronosSpineAttempt, workspace: Path
) -> tuple[RealMediaSeasonReceipt | MultiCameraMasteringReceipt, Mapping[str, object]]:
    parameters = dict(attempt.parameters)
    try:
        if attempt.operation_name == REAL_MEDIA_OPERATION:
            allowed = {
                "episode_count",
                "shots_per_episode",
                "frame_count_per_shot",
                "frame_size",
                "sample_rate_hz",
                "frame_rate_num",
                "frame_rate_den",
                "drift_kelvin_per_shot",
                "exposure_drift_per_shot",
                "scale_drift_per_shot",
                "naive_frame_rate_fps",
                "ffmpeg_path",
                "render_mp4",
                "max_delta_e00",
            }
            unknown = set(parameters) - allowed
            if unknown:
                raise ChronosSpineExecutionError(
                    f"unsupported real-media parameters: {sorted(unknown)}"
                )
            render_mp4 = parameters.pop("render_mp4", False)
            parameters.pop("max_delta_e00", None)
            if type(render_mp4) is not bool:
                raise ChronosSpineExecutionError("render_mp4 must be a boolean")
            frame_size = parameters.get("frame_size")
            if frame_size is not None:
                if (
                    not isinstance(frame_size, (list, tuple))
                    or len(frame_size) != 2
                    or any(type(dimension) is not int or dimension <= 4 for dimension in frame_size)
                ):
                    raise ChronosSpineExecutionError("frame_size must contain two dimensions > 4")
                parameters["frame_size"] = (frame_size[0], frame_size[1])
            result: RealMediaSeasonReceipt | MultiCameraMasteringReceipt
            result = execute_real_media_season_continuity(
                workspace,
                shots=attempt.real_media_shots,
                show_bible_anchor=attempt.show_bible_anchor,
                **cast(Any, parameters),
            )
            summary: Mapping[str, object] = {
                "operation_name": attempt.operation_name,
                "shot_count": result.shot_count,
                "frame_count": result.frame_count,
                "sample_count": result.sample_count,
                "exact_sample_horizon": result.exact_sample_horizon,
                "anchored_sample_clock_drift": result.anchored_sample_clock_drift,
                "merkle_root_sha256": result.merkle_root_sha256,
            }
        else:
            allowed = {
                "sample_rate_hz",
                "max_reflection_order",
                "bridge_duration_s",
                "bridge_sample_count",
                "ffmpeg_path",
                "render_mp4",
                "max_delta_e00",
            }
            unknown = set(parameters) - allowed
            if unknown:
                raise ChronosSpineExecutionError(
                    f"unsupported mastering parameters: {sorted(unknown)}"
                )
            render_mp4 = parameters.pop("render_mp4", False)
            parameters.pop("max_delta_e00", None)
            if type(render_mp4) is not bool:
                raise ChronosSpineExecutionError("render_mp4 must be a boolean")
            if attempt.room is None or attempt.speaker_position_m is None:
                raise ChronosSpineExecutionError(
                    "Omniverse mastering requires room and speaker inputs"
                )
            mastering_result = execute_multicamera_mastering(
                attempt.multicamera_shots,
                room=attempt.room,
                speaker_position_m=attempt.speaker_position_m,
                workspace_root=workspace,
                show_bible_anchor=attempt.show_bible_anchor,
                **cast(Any, parameters),
            )
            result = mastering_result
            summary = mastering_result.summary()
        if render_mp4:
            _render_optional_mp4(workspace, attempt.parameters)
            summary = _summary_with_disk_merkle(workspace, summary)
        return result, summary
    except ChronosSpineExecutionError:
        raise
    except Exception as exc:  # noqa: BLE001 - kernel/IO failure is a typed refusal
        raise ChronosSpineExecutionError("Chronos media render failed closed") from exc


def _render_optional_mp4(workspace: Path, parameters: Mapping[str, object]) -> None:
    fs = WorkspaceFilesystem(workspace)
    manifest_rel = "media/season-continuity-manifest.json"
    try:
        manifest = json.loads(fs.read_text(manifest_rel))
    except (FilesystemBoundaryError, OSError, json.JSONDecodeError, UnicodeError) as exc:
        raise ChronosSpineExecutionError(
            "cannot load the staged media manifest for MP4 mux"
        ) from exc
    binary = parameters.get("ffmpeg_path")
    if not isinstance(binary, str) or not binary:
        from nexus_ai_agent.creative.chronos.real_media_continuity import discover_ffmpeg_executable

        binary = discover_ffmpeg_executable()
    if binary is None:
        raise ChronosSpineExecutionError("MP4 was requested but FFmpeg is unavailable")
    frame_rate_text = manifest.get("frame_rate")
    if not isinstance(frame_rate_text, str) or "/" not in frame_rate_text:
        raise ChronosSpineExecutionError("staged manifest has no rational frame rate")
    for receipt in manifest.get("receipts", []):
        if not isinstance(receipt, dict):
            raise ChronosSpineExecutionError("staged receipt is malformed")
        episode, shot = int(receipt["episode_index"]), int(receipt["shot_index"])
        shot_rel = f"media/episode-{episode:04d}/shot-{shot:04d}"
        first_frame = fs.resolve(f"{shot_rel}/frame-00000.png")
        audio_path = fs.resolve(f"{shot_rel}/audio.wav")
        output_rel = f"{shot_rel}/mux.mp4"
        output_path = fs.resolve(output_rel)
        command = [
            binary,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-fflags",
            "+bitexact",
            "-framerate",
            frame_rate_text,
            "-i",
            str(first_frame.parent / "frame-%05d.png"),
            "-i",
            str(audio_path),
            "-frames:v",
            str(int(receipt["frame_count"])),
            "-c:v",
            "mpeg4",
            "-q:v",
            "2",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-map_metadata",
            "-1",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
        try:
            completed = subprocess.run(
                command, capture_output=True, text=True, timeout=90, check=False
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ChronosSpineExecutionError("optional MP4 mux could not run") from exc
        if completed.returncode != 0 or not fs.exists(output_rel):
            raise ChronosSpineExecutionError("optional MP4 mux failed")
        mp4_digest, _ = fs.hash_file(output_rel)
        receipt["mp4_path"] = output_rel
        receipt["mp4_sha256"] = mp4_digest
    root = merkle_ledger.merkle_root(manifest["receipts"])
    manifest["merkle_root_sha256"] = root.hex()
    manifest["inclusion_bundles"] = [
        merkle_ledger.to_rfc6962_bundle(
            receipt,
            merkle_ledger.build_inclusion_proof(manifest["receipts"], index),
            root,
        )
        for index, receipt in enumerate(manifest["receipts"])
    ]
    fs.write_text(manifest_rel, jcs_canonical_json(manifest))


def _summary_with_disk_merkle(
    workspace: Path, summary: Mapping[str, object]
) -> Mapping[str, object]:
    fs = WorkspaceFilesystem(workspace)
    manifest = json.loads(fs.read_text("media/season-continuity-manifest.json"))
    merged = dict(summary)
    merged["merkle_root_sha256"] = manifest["merkle_root_sha256"]
    return merged


def _read_manifest(workspace: Path) -> tuple[dict[str, Any], bytes]:
    fs = WorkspaceFilesystem(workspace)
    try:
        content = fs.read_bytes("media/season-continuity-manifest.json")
        value = json.loads(content)
    except (FilesystemBoundaryError, json.JSONDecodeError, UnicodeError) as exc:
        raise ChronosVerificationRefusalError("staged continuity receipt is unreadable") from exc
    if not isinstance(value, dict) or value.get("schema") != "chronos-real-media-season-v1":
        raise ChronosVerificationRefusalError("staged continuity receipt has an unsupported schema")
    if content != jcs_canonical_json(value).encode("utf-8"):
        raise ChronosVerificationRefusalError("staged continuity receipt is not canonical JSON")
    return value, content


def verify_staged_chronos_artifacts(
    workspace: str | Path,
    attempt: ChronosSpineAttempt,
    *,
    expected_summary: Mapping[str, object],
) -> ChronosVerificationReceipt:
    """Independently re-read, hash, decode, and verify every staged output file."""
    root = Path(workspace).expanduser().resolve(strict=True)
    fs = WorkspaceFilesystem(root)
    manifest, manifest_bytes = _read_manifest(root)
    try:
        spine_receipt_bytes = fs.read_bytes("chronos-spine-receipt.json")
        spine_receipt = json.loads(spine_receipt_bytes)
    except (FilesystemBoundaryError, json.JSONDecodeError, UnicodeError) as exc:
        raise ChronosVerificationRefusalError("staged spine receipt is unreadable") from exc
    if (
        not isinstance(spine_receipt, dict)
        or spine_receipt.get("schema") != "chronos-spine-receipt-v1"
        or spine_receipt.get("operation_name") != attempt.operation_name
        or spine_receipt.get("engine_version") != ENGINE_VERSION
        or not isinstance(spine_receipt.get("engine_summary"), dict)
        or spine_receipt.get("recipe_digest")
        != compute_chronos_recipe_identity(attempt).recipe_digest
        or spine_receipt_bytes != jcs_canonical_json(spine_receipt).encode("utf-8")
        or jcs_canonical_json(spine_receipt["engine_summary"])
        != jcs_canonical_json(expected_summary)
    ):
        raise ChronosVerificationRefusalError(
            "staged spine receipt does not match the engine result"
        )
    disk_summary = cast(Mapping[str, object], spine_receipt["engine_summary"])
    if not isinstance(manifest.get("receipts"), list) or not manifest["receipts"]:
        raise ChronosVerificationRefusalError("continuity receipt has no per-shot artifact entries")
    root_claim = manifest.get("merkle_root_sha256")
    recomputed = merkle_ledger.merkle_root(manifest["receipts"]).hex()
    if not isinstance(root_claim, str) or root_claim != recomputed:
        raise ChronosVerificationRefusalError(
            "RFC 6962 root differs from receipts re-read from disk"
        )
    bundles = manifest.get("inclusion_bundles")
    if not isinstance(bundles, list) or len(bundles) != len(manifest["receipts"]):
        raise ChronosVerificationRefusalError("RFC 6962 inclusion bundle count is incomplete")
    for receipt, bundle in zip(manifest["receipts"], bundles, strict=True):
        if (
            not isinstance(bundle, dict)
            or bundle.get("root_sha256") != root_claim
            or bundle.get("receipt") != receipt
            or not merkle_ledger.verify_rfc6962_bundle(bundle)
        ):
            raise ChronosVerificationRefusalError(
                "RFC 6962 inclusion proof failed independent verification"
            )

    sample_rate = manifest.get("sample_rate_hz")
    frame_count = manifest.get("frame_count")
    frame_rate = manifest.get("frame_rate")
    if (
        type(sample_rate) is not int
        or type(frame_count) is not int
        or not isinstance(frame_rate, str)
        or "/" not in frame_rate
    ):
        raise ChronosVerificationRefusalError("sample-clock receipt fields are malformed")
    try:
        numerator_text, denominator_text = frame_rate.split("/", 1)
        frame_rate_num, frame_rate_den = int(numerator_text), int(denominator_text)
        expected_horizon = canonical_sample_horizon(
            frame_count,
            sample_rate_hz=sample_rate,
            frame_rate_num=frame_rate_num,
            frame_rate_den=frame_rate_den,
        )
    except (ValueError, TypeError, OverflowError) as exc:
        raise ChronosVerificationRefusalError("sample-clock receipt is not rational") from exc
    if (
        sample_rate != 48_000
        or manifest.get("sample_count") != expected_horizon
        or manifest.get("exact_sample_horizon") != expected_horizon
        or disk_summary.get("anchored_sample_clock_drift", 0) != 0
    ):
        raise ChronosVerificationRefusalError(
            "persisted PCM sample horizon has nonzero rational drift"
        )

    anchor = attempt.show_bible_anchor
    if anchor is None:
        raise ChronosVerificationRefusalError(
            "no Show-Bible anchor is available for disk color verification"
        )
    files: list[ChronosFileVerification] = []
    spine_receipt_digest = hashlib.sha256(spine_receipt_bytes).hexdigest()
    spine_receipt_size = len(spine_receipt_bytes)
    if spine_receipt_size <= 0:
        raise ChronosVerificationRefusalError("staged spine receipt JSON is empty")
    files.append(
        ChronosFileVerification(
            "chronos-spine-receipt.json", spine_receipt_digest, spine_receipt_size
        )
    )
    decoded_frames_by_shot: list[tuple[dict[str, Any], list[FloatArray]]] = []
    sample_cursor = 0
    frame_cursor = 0
    observed_shot_keys: list[tuple[int, int]] = []
    if attempt.operation_name == REAL_MEDIA_OPERATION:
        expected_shot_keys = [
            (shot.episode_index, shot.shot_index) for shot in attempt.real_media_shots
        ]
    else:
        expected_shot_keys = [
            (shot.episode_index, shot.shot_index) for shot in attempt.multicamera_shots
        ]
    expected_paths: set[str] = {
        "media/season-continuity-manifest.json",
        "chronos-spine-receipt.json",
    }
    for receipt in manifest["receipts"]:
        if not isinstance(receipt, dict):
            raise ChronosVerificationRefusalError("per-shot media receipt is not an object")
        episode, shot_index = receipt.get("episode_index"), receipt.get("shot_index")
        frame_n, shot_samples = receipt.get("frame_count"), receipt.get("audio_sample_count")
        start, end = receipt.get("sample_start"), receipt.get("sample_end")
        frame_hashes = receipt.get("frame_sha256s")
        if (
            type(episode) is not int
            or type(shot_index) is not int
            or type(frame_n) is not int
            or type(shot_samples) is not int
            or type(start) is not int
            or type(end) is not int
            or episode < 1
            or shot_index < 1
            or frame_n <= 0
            or shot_samples <= 0
            or start != sample_cursor
            or start
            != canonical_sample_horizon(
                frame_cursor,
                sample_rate_hz=sample_rate,
                frame_rate_num=frame_rate_num,
                frame_rate_den=frame_rate_den,
            )
            or end
            != canonical_sample_horizon(
                frame_cursor + frame_n,
                sample_rate_hz=sample_rate,
                frame_rate_num=frame_rate_num,
                frame_rate_den=frame_rate_den,
            )
            or end - start != shot_samples
            or not isinstance(frame_hashes, list)
            or len(frame_hashes) != frame_n
        ):
            raise ChronosVerificationRefusalError(
                "per-shot timing or frame digest fields are invalid"
            )
        sample_cursor = end
        frame_cursor += frame_n
        observed_shot_keys.append((episode, shot_index))
        shot_rel = f"media/episode-{episode:04d}/shot-{shot_index:04d}"
        decoded: list[FloatArray] = []
        for frame_index, expected_digest in enumerate(frame_hashes):
            relative = f"{shot_rel}/frame-{frame_index:05d}.png"
            png_bytes, png_digest, png_size = _read_verified_file(fs, relative, expected_digest)
            try:
                with Image.open(BytesIO(png_bytes)) as image:
                    image.load()
                    decoded_pixels = np.asarray(image.convert("RGB"), dtype=np.uint8)
                decoded_rgb = decoded_pixels.astype(np.float64) / 255.0
            except Exception as exc:  # noqa: BLE001 - image decoder failures are verification refusals
                raise ChronosVerificationRefusalError(
                    "staged PNG could not be decoded from disk"
                ) from exc
            decoded_linear = color_science.srgb_decode(decoded_rgb)
            decoded.append(decoded_linear)
            files.append(
                ChronosFileVerification(
                    relative,
                    png_digest,
                    png_size,
                    hashlib.sha256(np.ascontiguousarray(decoded_pixels).tobytes()).hexdigest(),
                )
            )
            expected_paths.add(relative)
        decoded_frames_by_shot.append((receipt, decoded))
        audio_relative = f"{shot_rel}/audio.wav"
        audio_expected = receipt.get("audio_sha256")
        wav_bytes, wav_digest, wav_size = _read_verified_file(fs, audio_relative, audio_expected)
        try:
            with wave.open(BytesIO(wav_bytes), "rb") as reader:
                channels = reader.getnchannels()
                width = reader.getsampwidth()
                rate = reader.getframerate()
                audio_samples = reader.getnframes()
                pcm = reader.readframes(audio_samples)
        except (wave.Error, EOFError, FilesystemBoundaryError) as exc:
            raise ChronosVerificationRefusalError(
                "staged WAV could not be decoded from disk"
            ) from exc
        if (
            channels != receipt.get("audio_channel_count")
            or channels not in (1, 2)
            or width != 2
            or rate != sample_rate
            or audio_samples != shot_samples
            or len(pcm) != audio_samples * channels * width
        ):
            raise ChronosVerificationRefusalError(
                "persisted WAV channel/clock metadata is inconsistent"
            )
        decoded_pcm = np.frombuffer(pcm, dtype="<i2")
        files.append(
            ChronosFileVerification(
                audio_relative,
                wav_digest,
                wav_size,
                hashlib.sha256(decoded_pcm.tobytes()).hexdigest(),
            )
        )
        expected_paths.add(audio_relative)
        mux_digest = receipt.get("mux_sha256")
        if mux_digest is not None:
            mux_relative = f"{shot_rel}/mux.nut"
            mux_file = _verify_staged_file(fs, mux_relative, mux_digest)
            files.append(ChronosFileVerification(mux_relative, mux_file[0], mux_file[1]))
            expected_paths.add(mux_relative)
        mp4_digest = receipt.get("mp4_sha256")
        mp4_relative = receipt.get("mp4_path")
        if mp4_digest is not None:
            if not isinstance(mp4_relative, str) or not mp4_relative.startswith(f"{shot_rel}/"):
                raise ChronosVerificationRefusalError("MP4 path is not shot-contained")
            mp4_bytes, mp4_digest_measured, mp4_size = _read_verified_file(
                fs, mp4_relative, mp4_digest
            )
            if len(mp4_bytes) < 12 or mp4_bytes[4:8] != b"ftyp":
                raise ChronosVerificationRefusalError("staged MP4 framing is invalid")
            _decode_mp4_bytes(mp4_bytes, attempt.parameters)
            files.append(ChronosFileVerification(mp4_relative, mp4_digest_measured, mp4_size))
            expected_paths.add(mp4_relative)
    if (
        sample_cursor != expected_horizon
        or frame_cursor != frame_count
        or observed_shot_keys != expected_shot_keys
    ):
        raise ChronosVerificationRefusalError(
            "per-shot media receipts do not match the ordered exact input horizons"
        )

    max_delta = 0.0
    for _, decoded_frames in decoded_frames_by_shot:
        measurements = measure_patch_delta_e00_by_frame(
            np.stack(decoded_frames),
            anchor.reference_patch_linear_rgb,
            anchor.calibration_roi,
        )
        max_delta = max(max_delta, max(measurements))
    threshold = attempt.parameters.get("max_delta_e00", 0.25)
    if (
        not isinstance(threshold, (int, float))
        or isinstance(threshold, bool)
        or not math.isfinite(float(threshold))
        or float(threshold) <= 0
    ):
        raise ChronosVerificationRefusalError("color continuity threshold is invalid")
    if max_delta > float(threshold):
        raise ChronosVerificationRefusalError(
            "post-disk CIEDE2000 exceeds the configured threshold"
        )

    epipolar_error: float | None = None
    rt60_error: float | None = None
    if attempt.operation_name == OMNIVERSE_OPERATION:
        epipolar_values: list[float] = []
        for shot in attempt.multicamera_shots:
            rig = multicam_epipolar_matching.compute_fundamental_matrix_from_rig(
                shot.intrinsics_a, shot.intrinsics_b, shot.rotation_ab, shot.translation_ab
            )
            audit = multicam_epipolar_matching.audit_and_project_epipolar_correspondences(
                shot.points_a_px, shot.points_b_px, rig.fundamental_matrix
            )
            epipolar_values.extend(float(value) for value in audit.sampson_distance_squared_after)
        if not epipolar_values or any(not math.isfinite(value) for value in epipolar_values):
            raise ChronosVerificationRefusalError("camera correspondence audit is incomplete")
        epipolar_error = max(epipolar_values)
        if epipolar_error > 1e-10:
            raise ChronosVerificationRefusalError("independent Sampson residual exceeds tolerance")
        if attempt.room is None or attempt.speaker_position_m is None:
            raise ChronosVerificationRefusalError("Omniverse room-verification inputs are missing")
        acoustic_shots = tuple(
            (
                f"episode-{shot.episode_index:04d}-shot-{shot.shot_index:04d}",
                shot.camera_position_m,
                shot.camera_to_world_rotation,
            )
            for shot in attempt.multicamera_shots
        )
        max_order = attempt.parameters.get("max_reflection_order", 2)
        if type(max_order) is not int or max_order < 0:
            raise ChronosVerificationRefusalError("room reflection order is invalid")
        try:
            acoustic = acoustic_space_continuity.audit_cross_shot_acoustic_continuity(
                attempt.room,
                attempt.speaker_position_m,
                acoustic_shots,
                sample_rate_hz=48_000,
                max_order=max_order,
            )
        except Exception as exc:  # noqa: BLE001 - remeasurement errors refuse publish
            raise ChronosVerificationRefusalError(
                "independent room-continuity remeasurement failed"
            ) from exc
        rt60_error = acoustic.rt60_max_spread_seconds
        if rt60_error > 1e-9 or not acoustic.drr_monotonic_with_proximity:
            raise ChronosVerificationRefusalError("independent room continuity audit failed")
        shot_summaries = disk_summary.get("shots")
        if not isinstance(shot_summaries, list) or len(shot_summaries) != len(
            attempt.multicamera_shots
        ):
            raise ChronosVerificationRefusalError("disk receipt omits camera audit measurements")
        claimed_epipolar: list[float] = []
        for shot_summary in shot_summaries:
            if not isinstance(shot_summary, dict):
                raise ChronosVerificationRefusalError("disk camera audit entry is malformed")
            claimed = shot_summary.get("max_sampson_distance_squared_after")
            if not isinstance(claimed, (int, float)) or not math.isfinite(float(claimed)):
                raise ChronosVerificationRefusalError("disk camera audit metric is invalid")
            claimed_epipolar.append(float(claimed))
        if not math.isclose(max(claimed_epipolar), epipolar_error, rel_tol=0.0, abs_tol=1e-12):
            raise ChronosVerificationRefusalError(
                "disk camera metrics differ from independent remeasurement"
            )
        claimed_rt60 = disk_summary.get("rt60_max_spread_seconds")
        if (
            not isinstance(claimed_rt60, (int, float))
            or not math.isfinite(float(claimed_rt60))
            or not math.isclose(float(claimed_rt60), rt60_error, rel_tol=0.0, abs_tol=1e-12)
        ):
            raise ChronosVerificationRefusalError(
                "disk RT60 metric differs from independent remeasurement"
            )

    manifest_path = "media/season-continuity-manifest.json"
    manifest_digest = hashlib.sha256(manifest_bytes).hexdigest()
    manifest_size = len(manifest_bytes)
    if manifest_size <= 0:
        raise ChronosVerificationRefusalError("staged receipt JSON is empty")
    files.append(ChronosFileVerification(manifest_path, manifest_digest, manifest_size))
    if manifest.get("merkle_root_sha256") != disk_summary.get("merkle_root_sha256"):
        raise ChronosVerificationRefusalError(
            "engine summary and independently recomputed Merkle roots differ"
        )

    for path in fs.iter_files():
        relative = path.relative_to(root).as_posix()
        if relative not in expected_paths:
            raise ChronosVerificationRefusalError(
                f"unexpected staged file is outside the verified media manifest: {relative}"
            )
    if not files or any(file.size_bytes <= 0 for file in files):
        raise ChronosVerificationRefusalError("staging contains an empty or missing expected file")
    file_map = {file.relative_path: file.sha256 for file in files}
    if len(file_map) != len(files):
        raise ChronosVerificationRefusalError("staged output file list contains duplicate paths")
    return ChronosVerificationReceipt(
        "REAL-DISK",
        tuple(sorted(files, key=lambda file: file.relative_path)),
        root_claim,
        sample_cursor - expected_horizon,
        max_delta,
        epipolar_error,
        rt60_error,
    )


def _read_verified_file(
    fs: WorkspaceFilesystem, relative_path: str, expected_digest: object
) -> tuple[bytes, str, int]:
    if not isinstance(expected_digest, str) or not _SHA256_RE.fullmatch(expected_digest):
        raise ChronosVerificationRefusalError("artifact receipt contains an invalid SHA-256 digest")
    try:
        fs.require_regular_file(relative_path)
        content = fs.read_bytes(relative_path)
    except (FilesystemBoundaryError, OSError) as exc:
        raise ChronosVerificationRefusalError(
            "expected staged artifact is missing or unsafe"
        ) from exc
    digest = hashlib.sha256(content).hexdigest()
    size = len(content)
    if size <= 0 or digest != expected_digest:
        raise ChronosVerificationRefusalError(
            "staged artifact is empty or differs from its receipt digest"
        )
    return content, digest, size


def _verify_staged_file(
    fs: WorkspaceFilesystem, relative_path: str, expected_digest: object
) -> tuple[str, int]:
    """Verify a recorded digest over the exact bytes returned for decoding."""
    _, digest, size = _read_verified_file(fs, relative_path, expected_digest)
    return digest, size


def _decode_mp4_bytes(content: bytes, parameters: Mapping[str, object]) -> None:
    """Decode the exact hash-verified MP4 bytes with FFmpeg before publication."""
    binary = parameters.get("ffmpeg_path")
    if not isinstance(binary, str) or not binary:
        from nexus_ai_agent.creative.chronos.real_media_continuity import (
            discover_ffmpeg_executable,
        )

        binary = discover_ffmpeg_executable()
    if binary is None:
        raise ChronosVerificationRefusalError("cannot independently decode MP4 without FFmpeg")
    command = [
        binary,
        "-hide_banner",
        "-loglevel",
        "error",
        "-xerror",
        "-i",
        "pipe:0",
        "-map",
        "0:v:0",
        "-map",
        "0:a:0",
        "-f",
        "null",
        "-",
    ]
    try:
        completed = subprocess.run(
            command,
            input=content,
            capture_output=True,
            timeout=90,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ChronosVerificationRefusalError("staged MP4 decoder could not run") from exc
    if completed.returncode != 0:
        raise ChronosVerificationRefusalError("staged MP4 failed independent stream decoding")


def execute_chronos_staged_attempt(
    workspace_root: str | Path,
    attempt: ChronosSpineAttempt,
    *,
    active_fencing_token: FencingTokenReader,
    before_verify: PreVerifyHook | None = None,
) -> ChronosSpineExecutionReceipt:
    """Stage, render, independently verify, and atomically publish one attempt."""
    if not callable(active_fencing_token):
        raise ChronosSpineExecutionError("an authoritative active fencing-token reader is required")
    snapshot = _snapshot_attempt(attempt)
    stable_attempt = snapshot.attempt
    try:
        identity = ExecutionIdentity(
            request_id=stable_attempt.project_id,
            idempotency_key=hashlib.sha256(
                f"{stable_attempt.project_id}\x00{stable_attempt.attempt_id}".encode()
            ).hexdigest(),
            job_id=stable_attempt.project_id,
            attempt_id=stable_attempt.attempt_id,
            fencing_token=stable_attempt.fencing_token,
            backend="chronos_local",
        )
    except ValueError as exc:
        raise ChronosSpineExecutionError("NEXUS execution identity rejected the attempt") from exc
    del identity  # The NEXUS contract is used as the identity validator, not as local authority.
    identity_parameters = _identity_parameters(stable_attempt)
    recipe_payload: dict[str, object] = {
        "project_id": stable_attempt.project_id,
        "parent_revision_id": stable_attempt.parent_revision_id,
        "attempt_id": stable_attempt.attempt_id,
        "fencing_token": stable_attempt.fencing_token,
        "input_media_digests": list(snapshot.input_media_digests),
        "operation_name": stable_attempt.operation_name,
        "parameters": identity_parameters,
        "engine_version": ENGINE_VERSION,
    }
    parameters_json = jcs_canonical_json(identity_parameters)
    recipe_digest = hashlib.sha256(jcs_canonical_json(recipe_payload).encode("utf-8")).hexdigest()
    recipe = ChronosRecipeIdentity(
        stable_attempt.project_id,
        stable_attempt.parent_revision_id,
        stable_attempt.attempt_id,
        stable_attempt.fencing_token,
        snapshot.input_media_digests,
        stable_attempt.operation_name,
        parameters_json,
        ENGINE_VERSION,
        recipe_digest,
    )
    _require_current_fence(stable_attempt, active_fencing_token)

    root = Path(workspace_root).expanduser().resolve(strict=False)
    root.mkdir(parents=True, exist_ok=True)
    fs = WorkspaceFilesystem(root)
    stage_name = f"{stable_attempt.attempt_id}_{stable_attempt.fencing_token}"
    stage_rel = f".staging/{stage_name}"
    owns_stage = False
    try:
        fs.ensure_directory(".staging")
        if _directory_exists(fs, stage_rel):
            raise ChronosSpineExecutionError("private attempt staging path already exists")
        stage_path = fs.ensure_directory(stage_rel)
        owns_stage = True
        _engine_result, engine_summary = _engine_run(stable_attempt, stage_path)
        summary_payload: dict[str, object] = {
            "schema": "chronos-spine-receipt-v1",
            "recipe_digest": recipe.recipe_digest,
            "operation_name": recipe.operation_name,
            "engine_version": recipe.engine_version,
            "engine_summary": dict(engine_summary),
        }
        summary_fs = WorkspaceFilesystem(stage_path)
        summary_fs.write_text("chronos-spine-receipt.json", jcs_canonical_json(summary_payload))
        if before_verify is not None:
            before_verify(stage_path)
        verification = verify_staged_chronos_artifacts(
            stage_path, stable_attempt, expected_summary=engine_summary
        )
        # The reader is consulted again at the publish boundary: verification
        # can take long enough for a newer attempt to supersede this worker.
        _require_current_fence(stable_attempt, active_fencing_token)
        published_file_digests = tuple(
            sorted((item.relative_path, item.sha256) for item in verification.files)
        )
        output_digest = hashlib.sha256(
            jcs_canonical_json(dict(published_file_digests)).encode("utf-8")
        ).hexdigest()
        input_digest = hashlib.sha256(
            jcs_canonical_json(list(recipe.input_media_digests)).encode("utf-8")
        ).hexdigest()
        produced_revision = (
            "rev_"
            + hashlib.sha256(
                jcs_canonical_json(
                    {
                        "parent_revision_id": recipe.parent_revision_id,
                        "output_content_sha256": output_digest,
                    }
                ).encode("utf-8")
            ).hexdigest()
        )
        asset_version_id = (
            "av_"
            + hashlib.sha256(
                jcs_canonical_json(
                    {
                        "project_id": recipe.project_id,
                        "produced_revision_id": produced_revision,
                        "output_content_sha256": output_digest,
                    }
                ).encode("utf-8")
            ).hexdigest()
        )
        published_rel = f"published/{recipe.project_id}/{produced_revision}/{asset_version_id}"
        verified_at = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        record = ChronosAssetVersionRecord(
            asset_version_id,
            recipe.project_id,
            recipe.parent_revision_id,
            produced_revision,
            recipe.attempt_id,
            recipe.fencing_token,
            recipe.recipe_digest,
            verification.merkle_root,
            input_digest,
            output_digest,
            published_file_digests,
            "REAL-DISK",
            verified_at,
            verification.sample_drift,
            verification.max_delta_e00,
            verification.epipolar_sampson_error,
            verification.rt60_error,
            str(root / published_rel),
        )
        passport_payload = record.to_dict()
        passport_payload.pop("published_path", None)
        passport_digest = hashlib.sha256(
            jcs_canonical_json(passport_payload).encode("utf-8")
        ).hexdigest()
        passport = ChronosArtifactPassport(
            record.asset_version_id,
            record.project_id,
            record.parent_revision_id,
            record.produced_revision_id,
            record.attempt_id,
            record.fencing_token,
            record.recipe_digest,
            record.merkle_root,
            record.input_content_sha256,
            record.output_content_sha256,
            record.published_file_digests,
            record.verification_tier,
            record.verified_at_utc,
            record.sample_drift,
            record.max_delta_e00,
            record.epipolar_sampson_error,
            record.rt60_error,
            passport_digest,
        )
        # Recheck immediately before the only namespace transition. rename_within
        # is descriptor-relative and atomic; an existing version is never replaced.
        _require_current_fence(stable_attempt, active_fencing_token)
        if _directory_exists(fs, published_rel):
            raise ChronosSpineExecutionError("immutable asset-version destination already exists")
        fs.rename_within(stage_rel, published_rel, create_parents=True)
        return ChronosSpineExecutionReceipt(
            recipe,
            verification,
            record,
            passport,
            dict(engine_summary),
        )
    except ChronosVerificationRefusalError:
        if owns_stage:
            _quarantine_stage(fs, stage_rel, stage_name)
        raise
    except ChronosSpineExecutionError:
        if owns_stage:
            _quarantine_stage(fs, stage_rel, stage_name)
        raise
    except (FilesystemBoundaryError, OSError, ValueError, TypeError) as exc:
        if owns_stage:
            _quarantine_stage(fs, stage_rel, stage_name)
        raise ChronosSpineExecutionError("Chronos staged attempt failed closed") from exc
    except Exception as exc:  # noqa: BLE001 - hooks and renderer failures also quarantine
        if owns_stage:
            _quarantine_stage(fs, stage_rel, stage_name)
        raise ChronosSpineExecutionError("Chronos staged attempt failed closed") from exc


def _directory_exists(fs: WorkspaceFilesystem, relative: str) -> bool:
    try:
        resolved = fs.resolve(relative)
        if not resolved.exists():
            return False
        fs.ensure_directory(relative, create=False)
    except FilesystemBoundaryError as exc:
        raise ChronosSpineExecutionError(
            "unsafe path in Chronos staging or publication namespace"
        ) from exc
    return True


def _quarantine_stage(fs: WorkspaceFilesystem, stage_rel: str, stage_name: str) -> None:
    quarantine_rel = f".staging/_quarantine/{stage_name}.{secrets.token_hex(8)}.refused"
    try:
        if not _directory_exists(fs, stage_rel):
            return
        fs.ensure_directory(".staging/_quarantine")
        fs.rename_within(stage_rel, quarantine_rel, create_parents=True)
    except (ChronosSpineExecutionError, FilesystemBoundaryError, OSError):
        try:
            fs.remove_tree(stage_rel)
        except (FilesystemBoundaryError, OSError):
            pass


__all__ = [
    "ChronosArtifactPassport",
    "ChronosAssetVersionRecord",
    "ChronosFileVerification",
    "ChronosRecipeIdentity",
    "ChronosSpineAttempt",
    "ChronosSpineExecutionError",
    "ChronosSpineExecutionReceipt",
    "ChronosVerificationReceipt",
    "ChronosVerificationRefusalError",
    "ENGINE_VERSION",
    "FencingTokenReader",
    "OMNIVERSE_OPERATION",
    "OperationName",
    "REAL_MEDIA_OPERATION",
    "compute_chronos_recipe_identity",
    "execute_chronos_staged_attempt",
    "jcs_canonical_json",
    "verify_staged_chronos_artifacts",
]
