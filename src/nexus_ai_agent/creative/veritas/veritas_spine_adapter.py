"""Fail-closed staging with quarantined failures and durable local lineage.

The injected token getter is checked twice; the file-backed ledger additionally
serializes local publication with POSIX flock and rejects token regression.
CommandBus authorization is an optional caller hook, not a fabricated canonical
bus integration. This is not a distributed database transaction.
"""

from __future__ import annotations

import fcntl
import hashlib
import importlib
import json
import os
import re
import shutil
import subprocess
import uuid
import wave
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from nexus_ai_agent.creative.veritas.omnia_cinema_physics import (
    RotarySubframeSequence,
    validate_rotary_subframes,
)
from nexus_ai_agent.creative.veritas.real_media_veritas_compiler import (
    FloatArray,
    RealMediaVeritasBundle,
    _read_png_rgb,
    _read_wav_mono_16bit,
    compile_and_heal_real_media_shot,
    extract_cinema_shot_from_media,
)
from nexus_ai_agent.creative.veritas.reality_auditor import CinemaShot, VeritasRealityAuditor


class VeritasMediaVerificationError(ValueError):
    """Persisted evidence or decoded physical observations failed verification."""


class VeritasSpineVerificationError(VeritasMediaVerificationError):
    """A private attempt was refused before publication."""


class VeritasStaleFencingTokenError(VeritasSpineVerificationError):
    """The caller's lease is no longer active."""


def _canonical_digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _verify_inclusion(leaf: bytes, index: int, size: int, proof: list[str], root: str) -> bool:
    """Verify an RFC 6962 unbalanced-tree path with index-derived orientation."""
    cursor = iter(proof)

    def ascend(value: bytes, position: int, count: int) -> bytes:
        if count == 1:
            return value
        split = 1 << ((count - 1).bit_length() - 1)
        if position < split:
            child = ascend(value, position, split)
            return hashlib.sha256(b"\x01" + child + bytes.fromhex(next(cursor))).digest()
        child = ascend(value, position - split, count - split)
        return hashlib.sha256(b"\x01" + bytes.fromhex(next(cursor)) + child).digest()

    try:
        result = ascend(leaf, index, size)
        return result.hex() == root and next(cursor, None) is None
    except (StopIteration, ValueError):
        return False


def _verify_mp4_stream(
    binary: str, path: Path, count: int, height: int, width: int, fps: float
) -> None:
    # showinfo observes decoded frame timestamps before any output-rate conversion.
    completed = subprocess.run(
        [
            binary,
            "-v",
            "info",
            "-xerror",
            "-i",
            str(path),
            "-map",
            "0:v:0",
            "-vf",
            "showinfo",
            "-fps_mode",
            "passthrough",
            "-frames:v",
            str(count + 1),
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-",
        ],
        capture_output=True,
        check=False,
        timeout=30,
    )
    if completed.returncode:
        raise ValueError("MP4 decode refused")
    log = completed.stderr.decode(errors="replace")
    times = [float(value) for value in re.findall(r"\bpts_time:([\d.eE+\-]+)", log)]
    durations = [float(value) for value in re.findall(r"\bduration_time:([\d.eE+\-]+)", log)]
    if durations and (
        len(durations) != count or not np.allclose(durations, 1 / fps, rtol=0, atol=1e-5)
    ):
        raise ValueError("MP4 decoded frame duration mismatch")
    rate = re.search(r"config in time_base: (\d+)/(\d+), frame_rate: (\d+)/(\d+)", log)
    if len(completed.stdout) != count * height * width * 3 or len(times) != count:
        raise ValueError("MP4 frame count mismatch")
    if (
        rate is None
        or int(rate[4]) == 0
        or abs(int(rate[3]) / int(rate[4]) - fps) > 1e-8
        or not np.allclose(times, np.arange(count) / fps, rtol=0, atol=1e-5)
    ):
        raise ValueError("MP4 frame rate or duration mismatch")


def _verify_rotary_coordinates(sequence: RotarySubframeSequence) -> None:
    """Independent scalar coordinate-transform and convex pixel-range oracle."""
    import math

    m = sequence.angular_harmonic_m
    if type(m) is not int or m < 1 or sequence.output_fps <= 0:
        raise ValueError("invalid angular harmonic clock")
    step = sequence.angular_velocity_rad_s / sequence.output_fps
    if m * abs(step) >= math.pi or not np.allclose(
        np.diff(sequence.phases_rad), step, rtol=0, atol=1e-12
    ):
        raise ValueError("angular harmonic Nyquist or phase step mismatch")
    reference = sequence.reference
    height, width = reference.shape[:2]
    if (
        not np.all(np.isfinite(sequence.frames))
        or np.min(sequence.frames) < np.min(reference) - 1 / 255
        or np.max(sequence.frames) > np.max(reference) + 1 / 255
    ):
        raise ValueError("rotary pixel range invariant")
    # Check spatially separated coordinates in EVERY frame without _rotate/_bilinear.
    for index, phase in enumerate(sequence.phases_rad):
        for y, x in {(0, 0), (height // 2, width // 2), (height - 1, width - 1), (1, width - 2)}:
            value = np.zeros(reference.shape[2:] or (), dtype=np.float64)
            for offset in sequence.exposure_offsets_s:
                angle = float(phase + sequence.angular_velocity_rad_s * offset)
                dx, dy = x - (width - 1) / 2, y - (height - 1) / 2
                sx = np.clip(
                    (width - 1) / 2 + math.cos(angle) * dx + math.sin(angle) * dy, 0, width - 1
                )
                sy = np.clip(
                    (height - 1) / 2 - math.sin(angle) * dx + math.cos(angle) * dy, 0, height - 1
                )
                x0, y0 = int(sx), int(sy)
                x1, y1 = min(x0 + 1, width - 1), min(y0 + 1, height - 1)
                wx, wy = sx - x0, sy - y0
                value += (
                    reference[y0, x0] * (1 - wx) * (1 - wy)
                    + reference[y0, x1] * wx * (1 - wy)
                    + reference[y1, x0] * (1 - wx) * wy
                    + reference[y1, x1] * wx * wy
                )
            expected = np.rint(value / len(sequence.exposure_offsets_s) * 255)
            if not np.array_equal(expected, np.rint(sequence.frames[index, y, x] * 255)):
                raise ValueError("independent rotary coordinate check refused")


def verify_compiled_veritas_bundle_on_disk(bundle: RealMediaVeritasBundle) -> bool:
    """Verify anchored disk evidence AND require zero RAW decoded violations.

    A second audit that could heal a defect is insufficient: the PNG/WAV files
    must already satisfy the implemented observation predicates. This certifies
    only these deterministic proxies, not scene semantics or unobserved laws.
    """
    try:
        directory = Path(bundle.output_dir).resolve(strict=True)
        manifest_path = directory / "veritas_merkle_bundle.json"
        if manifest_path.is_symlink():
            raise ValueError("symlink manifest")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        def artifact(name: str) -> Path:
            if not isinstance(name, str) or Path(name).name != name or name in ("", ".", ".."):
                raise ValueError("invalid artifact name")
            path = directory / name
            if path.is_symlink() or not path.is_file() or not path.stat().st_size:
                raise ValueError("missing, empty or symlink artifact")
            return path

        pngs = [artifact(name) for name in manifest["png_names"]]
        if not pngs or len(set(pngs)) != len(pngs):
            raise ValueError("empty or duplicate frames")
        wav = artifact(manifest["wav_name"])
        frames = np.stack([_read_png_rgb(path) for path in pngs])
        pcm = _read_wav_mono_16bit(wav)
        if not pcm.size:
            raise ValueError("empty PCM")
        with wave.open(str(wav), "rb") as handle:
            if handle.getframerate() != manifest["sample_rate_hz"]:
                raise ValueError("sample rate mismatch")
        frame_files = hashlib.sha256()
        for path in pngs:
            frame_files.update(path.read_bytes())
        actual = {
            "input_frames_content_sha256": bundle.input_frames_content_sha256,
            "input_audio_content_sha256": bundle.input_audio_content_sha256,
            "output_frames_content_sha256": hashlib.sha256(frames.tobytes()).hexdigest(),
            "output_audio_content_sha256": hashlib.sha256(pcm.tobytes()).hexdigest(),
            "output_frames_sha256": frame_files.hexdigest(),
            "output_audio_sha256": hashlib.sha256(wav.read_bytes()).hexdigest(),
            "output_mp4_sha256": "",
        }
        if manifest["mp4_name"]:
            mp4 = artifact(manifest["mp4_name"])
            actual["output_mp4_sha256"] = hashlib.sha256(mp4.read_bytes()).hexdigest()
        for name, value in actual.items():
            if value != manifest[name] or value != getattr(bundle, name):
                raise ValueError(f"digest mismatch: {name}")
        if manifest["mp4_name"]:
            decoder = shutil.which("ffmpeg")
            if decoder is None:
                imageio_ffmpeg = importlib.import_module("imageio_ffmpeg")
                decoder = imageio_ffmpeg.get_ffmpeg_exe()
            _verify_mp4_stream(
                decoder, mp4, len(pngs), frames.shape[1], frames.shape[2], bundle.output_fps
            )
        expected_names = {path.name for path in pngs} | {wav.name, manifest_path.name}
        temporal_digest = ""
        if bundle.rotary_evidence_sha256:
            evidence_path = artifact("rotary_subframes.json")
            expected_names.add(evidence_path.name)
            temporal_digest = hashlib.sha256(evidence_path.read_bytes()).hexdigest()
            if temporal_digest != bundle.rotary_evidence_sha256:
                raise ValueError("rotary evidence digest mismatch")
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            sequence = RotarySubframeSequence(
                np.asarray(evidence["reference_rgb_u8"], dtype=np.float64) / 255,
                frames.astype(np.float64) / 255,
                np.asarray(evidence["timestamps_s"], dtype=np.float64),
                np.asarray(evidence["phases_rad"], dtype=np.float64),
                np.asarray(evidence["exposure_offsets_s"], dtype=np.float64),
                evidence["source_fps"],
                evidence["output_fps"],
                evidence["angular_velocity_rad_s"],
                evidence["source_frame_count"],
                evidence["shutter_angle_deg"],
                evidence["angular_harmonic_m"],
            )
            _verify_rotary_coordinates(sequence)
            if (
                validate_rotary_subframes(sequence, quantized=True)
                or sequence.output_fps != manifest["fps"]
                or sequence.source_fps != bundle.source_fps
            ):
                raise ValueError("decoded rotary trajectory refused")
        elif bundle.output_fps != bundle.source_fps:
            raise ValueError("HFR clock without rendered subframes")
        if manifest["mp4_name"]:
            expected_names.add(manifest["mp4_name"])
        if {path.name for path in directory.iterdir()} != expected_names:
            raise ValueError("unbound staging artifacts")
        if (
            manifest["fps"] != bundle.output_fps
            or manifest["source_fps"] != bundle.source_fps
            or manifest["sample_rate_hz"] != 48000
            or manifest["output_frame_count"] != len(pngs)
        ):
            raise ValueError("unsupported media clock")
        temporal_binding = {
            "fps": bundle.output_fps,
            "source_fps": bundle.source_fps,
            "output_frame_count": len(pngs),
            "rotary_evidence_sha256": temporal_digest,
        }
        if manifest["rotary_evidence_sha256"] != temporal_digest:
            raise ValueError("manifest rotary digest mismatch")
        payloads, proofs = manifest["leaf_payloads"], manifest["inclusion_proofs"]
        if (
            len(payloads) != 15
            or len(proofs) != 15
            or manifest["merkle_root"] != bundle.merkle_root
        ):
            raise ValueError("invalid tree")
        for index, (payload, proof) in enumerate(zip(payloads, proofs, strict=True)):
            if payload["law"] != index + 1 or payload["violations_after"]:
                raise ValueError("invalid law receipt")
            if any(
                payload[name] != value for name, value in {**actual, **temporal_binding}.items()
            ):
                raise ValueError("leaf content mismatch")
            leaf = hashlib.sha256(b"\x00" + json.dumps(payload, sort_keys=True).encode()).digest()
            if not _verify_inclusion(leaf, index, 15, proof, bundle.merkle_root):
                raise ValueError("inclusion proof refused")
        if manifest["post_heal_violations"] != 0 or bundle.post_heal_violations != 0:
            raise ValueError("compile audit not clean")
        shot = extract_cinema_shot_from_media(
            frames.astype(np.float64) / 255.0,
            pcm.astype(np.float64) / 32767.0,
            fps=manifest["fps"],
            sample_rate_hz=manifest["sample_rate_hz"],
        )
        audit = VeritasRealityAuditor().audit_and_heal_cinema_shot(shot)
        if audit.pre_audit_violations or audit.post_heal_violations:
            raise ValueError(
                f"decoded physical observations are not compliant: {audit.details_before}"
            )
        return True
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        IndexError,
        wave.Error,
        ImportError,
        subprocess.SubprocessError,
    ) as exc:
        raise VeritasMediaVerificationError(str(exc)) from exc


@dataclass(frozen=True)
class VeritasSpinePublishRequest:
    project_id: str
    revision_id: str
    shot_id: str
    attempt_id: str
    fencing_token: int
    workspace_root: Path
    published_root: Path
    shot: CinemaShot | None = None
    input_frames_rgb: FloatArray | None = None
    input_audio_pcm: FloatArray | None = None
    sample_rate_hz: int = 48000
    fps: int = 24
    angular_harmonic_m: int = 1


@dataclass(frozen=True)
class VeritasAssetVersionRecord:
    asset_version_id: str
    passport_id: str
    project_id: str
    revision_id: str
    shot_id: str
    attempt_id: str
    fencing_token: int
    merkle_root_sha256: str
    input_frames_content_sha256: str
    output_frames_content_sha256: str
    input_audio_content_sha256: str
    output_audio_content_sha256: str
    output_mp4_sha256: str
    published_mp4_path: str
    published_wav_path: str
    published_manifest_path: str


@dataclass(frozen=True)
class VeritasArtifactPassportRecord:
    passport_id: str
    asset_version_id: str
    project_id: str
    revision_id: str
    shot_id: str
    attempt_id: str
    fencing_token: int
    merkle_root_sha256: str
    rotary_evidence_sha256: str
    output_mp4_sha256: str
    law_pass_count: int = 15
    post_disk_verified: bool = True


class VeritasLineageRepository(Protocol):
    def transaction(self) -> AbstractContextManager[None]: ...
    def records(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]: ...
    def commit(
        self, asset: VeritasAssetVersionRecord, passport: VeritasArtifactPassportRecord
    ) -> None: ...


@dataclass(frozen=True)
class VeritasLineageStore:
    """Process-safe local CAS ledger; one atomic symlink publishes BOTH tables.

    Readers resolve _lineage once per snapshot. Generations are immutable and
    retained; a crash before the pointer swap leaves the prior ledger intact.
    This is POSIX local-filesystem durability, not a distributed CommandBus.
    """

    published_root: Path
    lock_path: Path

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def records(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        pointer = self.published_root / "_lineage"
        if not pointer.exists() and not pointer.is_symlink():
            return [], []
        directory = pointer.resolve(strict=True)
        generations = (self.published_root / ".lineage-generations").resolve()
        if not directory.is_relative_to(generations):
            raise VeritasSpineVerificationError("invalid lineage generation")
        return (
            json.loads((directory / "asset_versions.json").read_text()),
            json.loads((directory / "artifact_passports.json").read_text()),
        )

    def commit(
        self, asset: VeritasAssetVersionRecord, passport: VeritasArtifactPassportRecord
    ) -> None:
        assets, passports = self.records()
        relevant = [
            r
            for r in assets
            if (r["project_id"], r["shot_id"]) == (asset.project_id, asset.shot_id)
        ]
        if any(r["fencing_token"] > asset.fencing_token for r in relevant):
            raise VeritasStaleFencingTokenError("stale durable lineage token")
        existing = next(
            (
                r
                for r in relevant
                if r["attempt_id"] == asset.attempt_id and r["fencing_token"] == asset.fencing_token
            ),
            None,
        )
        if existing is not None:
            if existing != asdict(asset) or asdict(passport) not in passports:
                raise VeritasSpineVerificationError("conflicting lineage retry")
            return
        assets.append(asdict(asset))
        passports.append(asdict(passport))
        generation = self.published_root / ".lineage-generations" / uuid.uuid4().hex
        generation.mkdir(parents=True)
        for name, values in (
            ("asset_versions.json", assets),
            ("artifact_passports.json", passports),
        ):
            with (generation / name).open("w") as handle:
                json.dump(values, handle, sort_keys=True, separators=(",", ":"), allow_nan=False)
                handle.flush()
                os.fsync(handle.fileno())
        _fsync_directory(generation)
        _fsync_directory(generation.parent)
        pointer = self.published_root / (".lineage-" + uuid.uuid4().hex)
        pointer.symlink_to(generation.relative_to(self.published_root), target_is_directory=True)
        os.replace(pointer, self.published_root / "_lineage")
        _fsync_directory(self.published_root)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _quarantine(stage: Path, request: VeritasSpinePublishRequest, reason: str) -> None:
    root = request.workspace_root / ".quarantine"
    if root.is_symlink():
        raise VeritasSpineVerificationError("unsafe quarantine root; staging retained")
    root.mkdir(parents=True, exist_ok=True)
    destination = root / f"{request.attempt_id}_{request.fencing_token}"
    # Never overwrite evidence from an earlier attempt with the same identity.
    if destination.exists():
        destination = destination / ("retry_" + uuid.uuid4().hex)
    digests = {
        str(p.relative_to(stage)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(stage.rglob("*"))
        if p.is_file() and not p.is_symlink()
    }
    receipt = {
        "attempt_id": request.attempt_id,
        "fencing_token": request.fencing_token,
        "failure_reason": reason,
        "observed_digests": digests,
    }
    (stage / "quarantine_receipt.json").write_text(
        json.dumps(receipt, sort_keys=True, separators=(",", ":")), encoding="utf-8"
    )
    os.replace(stage, destination)
    _fsync_directory(root)


@dataclass(frozen=True)
class VeritasSpinePublishResult:
    asset_version: VeritasAssetVersionRecord
    compile_result: RealMediaVeritasBundle
    post_disk_verified: bool
    published_dir: Path
    artifact_passport: VeritasArtifactPassportRecord


@dataclass(frozen=True)
class VeritasStagedSpineAdapter:
    active_fencing_token_getter: Callable[[], int]
    pre_publish_tamper_hook: Callable[[Path], None] | None = None
    lineage_store: VeritasLineageRepository | None = None
    command_bus_authorizer: Callable[[VeritasSpinePublishRequest], bool] | None = None

    def execute(self, request: VeritasSpinePublishRequest) -> VeritasSpinePublishResult:
        if self.command_bus_authorizer is not None and not self.command_bus_authorizer(request):
            raise VeritasSpineVerificationError("CommandBus authorization refused")
        for value in (request.project_id, request.revision_id, request.shot_id, request.attempt_id):
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", value):
                raise VeritasSpineVerificationError("invalid identifier")
        if type(request.fencing_token) is not int or request.fencing_token <= 0:
            raise VeritasSpineVerificationError("invalid fencing token")
        if request.fps != 24 or request.sample_rate_hz != 48000:
            raise VeritasSpineVerificationError("current publication format is 24 fps / 48 kHz")
        if self.active_fencing_token_getter() != request.fencing_token:
            raise VeritasStaleFencingTokenError("stale before staging")
        if ".." in request.workspace_root.parts or ".." in request.published_root.parts:
            raise VeritasSpineVerificationError("path traversal")
        workspace = request.workspace_root.resolve()
        published = request.published_root.resolve()
        if not published.is_relative_to(workspace) or published == workspace:
            raise VeritasSpineVerificationError("publication must be within workspace")
        store = self.lineage_store or VeritasLineageStore(
            published,
            workspace / ".lineage-locks" / (_canonical_digest({"root": str(published)}) + ".lock"),
        )
        with store.transaction():
            return self._execute_locked(request, workspace, published, store)

    def _execute_locked(
        self,
        request: VeritasSpinePublishRequest,
        workspace: Path,
        published: Path,
        store: VeritasLineageRepository,
    ) -> VeritasSpinePublishResult:
        assets, _ = store.records()
        if any(
            r["project_id"] == request.project_id
            and r["shot_id"] == request.shot_id
            and r["fencing_token"] > request.fencing_token
            for r in assets
        ):
            raise VeritasStaleFencingTokenError("stale durable lineage token")
        private = workspace / ".staging"
        if private.is_symlink() or published.is_relative_to(private):
            raise VeritasSpineVerificationError("invalid staging boundary")
        private.mkdir(parents=True, exist_ok=True)
        stage = private / f"{request.attempt_id}_{request.fencing_token}"
        # Exclusive creation prevents reuse, symlink substitution and overwrites.
        try:
            stage.mkdir(mode=0o700)
        except OSError as exc:
            raise VeritasSpineVerificationError(
                "attempt staging already exists or is inaccessible"
            ) from exc
        try:
            bundle = compile_and_heal_real_media_shot(
                stage,
                shot=request.shot,
                input_frames=request.input_frames_rgb,
                input_audio_pcm=request.input_audio_pcm,
                sample_rate_hz=request.sample_rate_hz,
                fps=float(request.fps),
                angular_harmonic_m=request.angular_harmonic_m,
            )
            if self.pre_publish_tamper_hook is not None:
                self.pre_publish_tamper_hook(stage)
            if not verify_compiled_veritas_bundle_on_disk(bundle):
                raise VeritasSpineVerificationError("verifier returned false")
            asset_id = _canonical_digest(
                {
                    "project_id": request.project_id,
                    "revision_id": request.revision_id,
                    "shot_id": request.shot_id,
                    "attempt_id": request.attempt_id,
                    "fencing_token": request.fencing_token,
                    "merkle_root": bundle.merkle_root,
                    "output_mp4_sha256": bundle.output_mp4_sha256,
                }
            )
            target = published / request.shot_id
            retry = next((r for r in assets if r["asset_version_id"] == asset_id), None)
            if (target.exists() or target.is_symlink()) and retry is None:
                raise VeritasSpineVerificationError("publication already exists")
            # No published files or directories are created before verification.
            if self.active_fencing_token_getter() != request.fencing_token:
                raise VeritasStaleFencingTokenError("stale before promotion")
            relocated = replace(
                bundle,
                output_dir=str(target),
                png_paths=tuple(str(target / Path(p).name) for p in bundle.png_paths),
                wav_path=str(target / Path(bundle.wav_path).name),
                mp4_path=str(target / Path(bundle.mp4_path).name) if bundle.mp4_path else None,
                post_disk_verified=True,
            )
            record = VeritasAssetVersionRecord(
                asset_id,
                _canonical_digest({"asset": asset_id, "root": bundle.merkle_root}),
                request.project_id,
                request.revision_id,
                request.shot_id,
                request.attempt_id,
                request.fencing_token,
                bundle.merkle_root,
                bundle.input_frames_content_sha256,
                bundle.output_frames_content_sha256,
                bundle.input_audio_content_sha256,
                bundle.output_audio_content_sha256,
                bundle.output_mp4_sha256,
                relocated.mp4_path or "",
                relocated.wav_path,
                str(target / "veritas_merkle_bundle.json"),
            )
            passport = VeritasArtifactPassportRecord(
                record.passport_id,
                asset_id,
                request.project_id,
                request.revision_id,
                request.shot_id,
                request.attempt_id,
                request.fencing_token,
                bundle.merkle_root,
                bundle.rotary_evidence_sha256,
                bundle.output_mp4_sha256,
            )
            if retry is not None:
                if retry != asdict(record):
                    raise VeritasSpineVerificationError("conflicting publication retry")
                verify_compiled_veritas_bundle_on_disk(relocated)
                store.commit(record, passport)
                shutil.rmtree(stage)  # successful exact retry ONLY, never a failed attempt
            else:
                published.mkdir(parents=True, exist_ok=True)
                os.replace(stage, target)
                try:
                    store.commit(record, passport)
                except Exception:
                    os.replace(target, stage)
                    raise
            return VeritasSpinePublishResult(record, relocated, True, target, passport)
        except Exception as exc:
            if stage.exists():
                _quarantine(stage, request, str(exc))
            if isinstance(exc, VeritasSpineVerificationError):
                raise
            raise VeritasSpineVerificationError(str(exc)) from exc
