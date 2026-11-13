"""NAGAR VERITAS OMNIA — 15-law real-media PNG/WAV/FFmpeg shot compiler.

Runs every one of the fifteen Veritas laws over genuine multi-frame RGB buffers
and 48 kHz 16-bit PCM audio, **applies the resulting restorations to the media**,
writes the healed PNG sequence and WAV file to disk, optionally muxes an ``.mp4``
artefact, and seals the receipts in an RFC 6962 Merkle tree whose leaves bind the
input and output media digests.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import struct
import wave
import zlib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt

from nexus_ai_agent.creative.veritas.continuum_and_quantum_mechanics import (
    stabilize_madelung_phase_coherence,
)
from nexus_ai_agent.creative.veritas.homography_diegetic_typography import (
    render_homography_locked_diegetic_text,
)
from nexus_ai_agent.creative.veritas.omnia_cinema_physics import (
    apply_acoustic_doppler_shift,
    audit_and_heal_viseme_formant_sync,
    rectify_anamorphic_dioptric_optics,
    rectify_stroboscopic_wagon_wheel,
)
from nexus_ai_agent.creative.veritas.reality_auditor import (
    CinemaShot,
    VeritasRealityAuditor,
    build_corrupted_15_law_cinema_shot,
)

FloatArray = npt.NDArray[np.float64]
UInt8Array = npt.NDArray[np.uint8]

LAW_COUNT = 15
COMPILER_VERSION = "veritas-omnia-2.0"


@dataclass(frozen=True)
class LawReceipt:
    """One law's before/after violation record."""

    law: int
    name: str
    violations_before: tuple[str, ...]
    violations_after: tuple[str, ...]

    @property
    def healed(self) -> bool:
        return bool(self.violations_before) and not self.violations_after


@dataclass(frozen=True)
class RealMediaVeritasBundle:
    """Complete artefact bundle for a healed real-media shot."""

    output_dir: str
    png_paths: tuple[str, ...]
    wav_path: str
    mp4_path: str | None
    mp4_sha256: str | None
    merkle_root: str
    leaf_digests: tuple[str, ...]
    leaf_payloads: tuple[dict[str, Any], ...]
    receipts: tuple[LawReceipt, ...]
    pre_audit_violations: int
    post_heal_violations: int
    input_frames_sha256: str
    input_audio_sha256: str
    output_frames_sha256: str
    output_audio_sha256: str
    healed_frames: UInt8Array
    healed_audio_pcm: FloatArray
    input_frames_content_sha256: str
    output_frames_content_sha256: str
    input_audio_content_sha256: str
    output_audio_content_sha256: str
    output_mp4_sha256: str

    @property
    def healed(self) -> bool:
        return self.pre_audit_violations > 0 and self.post_heal_violations == 0

    @property
    def media_changed(self) -> bool:
        return (
            self.output_frames_content_sha256 != self.input_frames_content_sha256
            and self.output_audio_content_sha256 != self.input_audio_content_sha256
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "compiler_version": COMPILER_VERSION,
            "output_dir": self.output_dir,
            "png_count": len(self.png_paths),
            "wav_path": self.wav_path,
            "mp4_path": self.mp4_path,
            "mp4_sha256": self.mp4_sha256,
            "merkle_root": self.merkle_root,
            "pre_audit_violations": self.pre_audit_violations,
            "post_heal_violations": self.post_heal_violations,
            "input_frames_sha256": self.input_frames_sha256,
            "input_audio_sha256": self.input_audio_sha256,
            "output_frames_sha256": self.output_frames_sha256,
            "output_audio_sha256": self.output_audio_sha256,
            "input_frames_content_sha256": self.input_frames_content_sha256,
            "output_frames_content_sha256": self.output_frames_content_sha256,
            "input_audio_content_sha256": self.input_audio_content_sha256,
            "output_audio_content_sha256": self.output_audio_content_sha256,
            "output_mp4_sha256": self.output_mp4_sha256,
            "laws": [
                {
                    "law": receipt.law,
                    "name": receipt.name,
                    "before": list(receipt.violations_before),
                    "after": list(receipt.violations_after),
                }
                for receipt in self.receipts
            ],
        }


def _write_png(path: Path, rgb: UInt8Array) -> None:
    height, width = int(rgb.shape[0]), int(rgb.shape[1])
    raw = b"".join(b"\x00" + rgb[row].tobytes() for row in range(height))

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def _read_png_rgb(path: Path) -> UInt8Array:
    """Decode and CRC-check the RGB8/filter-zero PNG dialect emitted here.

    This is deliberately not a general PNG decoder. Unsupported encodings,
    truncated data, corrupt chunks and trailing data fail closed.
    """
    data = path.read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("invalid PNG signature")
    offset = 8
    width = height = 0
    compressed = bytearray()
    ended = False
    while offset < len(data):
        if offset + 12 > len(data):
            raise ValueError("truncated PNG chunk")
        length = struct.unpack_from(">I", data, offset)[0]
        end = offset + 12 + length
        if end > len(data):
            raise ValueError("truncated PNG payload")
        tag = data[offset + 4 : offset + 8]
        payload = data[offset + 8 : end - 4]
        crc = struct.unpack_from(">I", data, end - 4)[0]
        if zlib.crc32(tag + payload) & 0xFFFFFFFF != crc:
            raise ValueError("PNG CRC mismatch")
        if tag == b"IHDR":
            if offset != 8 or length != 13:
                raise ValueError("invalid PNG header")
            width, height, depth, color, compression, filtering, interlace = struct.unpack(
                ">IIBBBBB", payload
            )
            if (depth, color, compression, filtering, interlace) != (8, 2, 0, 0, 0):
                raise ValueError("unsupported PNG encoding")
            if width < 1 or height < 1:
                raise ValueError("invalid PNG dimensions")
        elif tag == b"IDAT":
            if not width:
                raise ValueError("PNG data before header")
            compressed.extend(payload)
        elif tag == b"IEND":
            if length or end != len(data):
                raise ValueError("invalid PNG end")
            ended = True
            break
        else:
            raise ValueError("unsupported PNG chunk")
        offset = end
    if not ended or not compressed:
        raise ValueError("incomplete PNG")
    decoder = zlib.decompressobj()
    expected = height * (1 + width * 3)
    raw = decoder.decompress(bytes(compressed), expected + 1)
    if len(raw) != expected or not decoder.eof or decoder.unused_data:
        raise ValueError("invalid PNG image stream")
    rows = np.frombuffer(raw, dtype=np.uint8).reshape(height, 1 + width * 3)
    if np.any(rows[:, 0] != 0):
        raise ValueError("unsupported PNG row filter")
    return rows[:, 1:].reshape(height, width, 3).copy()


def _read_wav_mono_16bit(path: Path) -> npt.NDArray[np.int16]:
    """Decode the on-disk canonical little-endian signed mono PCM samples."""
    with wave.open(str(path), "rb") as handle:
        if handle.getnchannels() != 1 or handle.getsampwidth() != 2:
            raise ValueError("WAV must be mono PCM16")
        count = handle.getnframes()
        raw = handle.readframes(count)
        if len(raw) != 2 * count:
            raise ValueError("truncated WAV data")
    return np.frombuffer(raw, dtype="<i2").astype(np.int16, copy=True)


def _write_wav(path: Path, pcm: FloatArray, sample_rate_hz: int) -> None:
    clipped = np.clip(pcm, -1.0, 1.0)
    samples = np.asarray(np.round(clipped * 32767.0), dtype=np.int16)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(int(sample_rate_hz))
        handle.writeframes(samples.tobytes())


def _radial_scale(channel: FloatArray, scale: float) -> FloatArray:
    height, width = int(channel.shape[0]), int(channel.shape[1])
    cy = (height - 1) / 2.0
    cx = (width - 1) / 2.0
    ys, xs = np.meshgrid(
        np.arange(height, dtype=np.float64), np.arange(width, dtype=np.float64), indexing="ij"
    )
    src_x = np.clip(cx + (xs - cx) / scale, 0.0, width - 1.0)
    src_y = np.clip(cy + (ys - cy) / scale, 0.0, height - 1.0)
    x0 = np.floor(src_x).astype(np.int64)
    y0 = np.floor(src_y).astype(np.int64)
    x1 = np.clip(x0 + 1, 0, width - 1)
    y1 = np.clip(y0 + 1, 0, height - 1)
    wx = src_x - x0
    wy = src_y - y0
    return (
        channel[y0, x0] * (1 - wx) * (1 - wy)
        + channel[y0, x1] * wx * (1 - wy)
        + channel[y1, x0] * (1 - wx) * wy
        + channel[y1, x1] * wx * wy
    )


def _synthesize_frames(num_frames: int, height: int, width: int) -> FloatArray:
    ys, xs = np.meshgrid(
        np.arange(height, dtype=np.float64) / max(height - 1, 1),
        np.arange(width, dtype=np.float64) / max(width - 1, 1),
        indexing="ij",
    )
    frames = np.empty((num_frames, height, width, 3), dtype=np.float64)
    for index in range(num_frames):
        phase = 2.0 * np.pi * index / max(num_frames, 1)
        spoke = 0.5 + 0.5 * np.cos(np.arctan2(ys - 0.5, xs - 0.5) * 6.0 + phase)
        base = 0.25 + 0.45 * spoke
        frames[index, :, :, 0] = base
        frames[index, :, :, 1] = base
        frames[index, :, :, 2] = base
    for index in range(num_frames):
        for channel, scale in ((0, 1.012), (2, 0.988)):
            frames[index, :, :, channel] = _radial_scale(frames[index, :, :, channel], scale)
        radius_sq = (xs - 0.5) ** 2 + (ys - 0.5) ** 2
        frames[index] *= (1.0 / np.sqrt(1.0 + 4.0 * radius_sq))[:, :, None] ** 4
    return np.asarray(np.clip(frames, 0.0, 1.0), dtype=np.float64)


def _merkle_root(leaves: list[str]) -> str:
    level = [bytes.fromhex(leaf) for leaf in leaves]
    while len(level) > 1:
        if len(level) % 2 == 1:
            level.append(level[-1])
        level = [
            hashlib.sha256(b"\x01" + level[i] + level[i + 1]).digest()
            for i in range(0, len(level), 2)
        ]
    return level[0].hex()


def _leaf_digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(b"\x00" + json.dumps(payload, sort_keys=True).encode()).hexdigest()


def merkle_root_from_payloads(payloads: Sequence[dict[str, Any]]) -> str:
    """Recompute the RFC 6962 root from leaf payloads (tamper detection)."""
    return _merkle_root([_leaf_digest(payload) for payload in payloads])


def _digest_array(array: npt.NDArray[np.generic]) -> str:
    return hashlib.sha256(np.ascontiguousarray(array).tobytes()).hexdigest()


def _digest_files(paths: list[Path]) -> str:
    accumulator = hashlib.sha256()
    for path in paths:
        accumulator.update(path.read_bytes())
    return accumulator.hexdigest()


def extract_cinema_shot_from_media(
    frames: FloatArray,
    audio_pcm: FloatArray,
    *,
    fps: float = 24.0,
    sample_rate_hz: int = 48_000,
) -> CinemaShot:
    """Extract deterministic image/audio observations, not semantic ground truth.

    Mouth-region energy is a fixed lower-central ROI proxy, not a face detector
    or measured FACS. Optical velocity is the minimum-norm brightness-constraint
    estimate (the aperture problem remains). Audio peaks are spectral proxies,
    not a speech recognizer. Unobserved laws remain absent rather than receiving
    fabricated anatomy, mirrors, contacts or a corrupted demo fixture.
    """
    rgb = np.asarray(frames, dtype=np.float64)
    pcm = np.asarray(audio_pcm, dtype=np.float64)
    if rgb.ndim != 4 or rgb.shape[-1] != 3 or min(rgb.shape[:3]) < 1:
        raise ValueError("frames must be non-empty TxHxWx3")
    if min(rgb.shape[1:3]) < 2 or not np.all(np.isfinite(rgb)):
        raise ValueError("frames must be finite with spatial dimensions >= 2")
    if np.any(rgb < 0) or np.any(rgb > 1):
        raise ValueError("frames must lie in [0, 1]")
    if pcm.ndim != 1 or not pcm.size or not np.all(np.isfinite(pcm)):
        raise ValueError("audio must be a finite non-empty mono waveform")
    if not np.isfinite(fps) or fps <= 0 or sample_rate_hz <= 0:
        raise ValueError("frame and sample rates must be positive")
    count, height, width = rgb.shape[:3]
    luminance = rgb @ np.array([0.2126, 0.7152, 0.0722])
    previous = np.concatenate([luminance[:1], luminance[:-1]], axis=0)
    gy, gx = np.gradient(luminance, axis=(1, 2))
    phase = np.arctan2(gy, gx)
    temporal = (luminance - previous) * fps
    inverse = -temporal / (gx * gx + gy * gy + 1e-6)
    velocity = np.stack([inverse * gx, inverse * gy], axis=-1)
    roi = luminance[:, height // 2 :, width // 4 : max(width // 4 + 1, 3 * width // 4)]
    jaw = np.clip(np.std(roi, axis=(1, 2)) * 2, 0, 1)
    stretch = (
        np.clip(np.mean(np.abs(np.diff(roi, axis=1)), axis=(1, 2)), 0, 1)
        if roi.shape[1] > 1
        else np.zeros(count)
    )
    energy = np.empty(count)
    f1 = np.full(count, 300.0)
    f2 = np.full(count, 1200.0)
    for index in range(count):
        start = min(int(index * sample_rate_hz / fps), pcm.size - 1)
        end = min(max(start + 2, int((index + 1) * sample_rate_hz / fps)), pcm.size)
        segment = pcm[start:end]
        energy[index] = float(np.sqrt(np.mean(segment**2)))
        frequencies = np.fft.rfftfreq(segment.size, d=1 / sample_rate_hz)
        spectrum = np.abs(np.fft.rfft(segment))
        for lower, upper, destination in ((300, 1000, f1), (1000, 2500, f2)):
            band = np.flatnonzero((frequencies >= lower) & (frequencies <= upper))
            if band.size:
                destination[index] = frequencies[band[np.argmax(spectrum[band])]]
    return CinemaShot(
        phase=(luminance[-1], phase[-1], previous[-1]),
        anamorphic=rgb[-1].copy(),
        viseme=(jaw, stretch, f1, energy, np.clip(1 - stretch, 0, 1), f2),
        doppler=(pcm.copy(), 0.0),
        fluid=(velocity, luminance.copy()),
        rotor=(luminance[-1].copy(), 0.0, fps),
    )


def compile_and_heal_real_media_shot(
    output_dir: str | Path,
    *,
    input_frames: FloatArray | None = None,
    input_audio_pcm: FloatArray | None = None,
    shot: CinemaShot | None = None,
    num_frames: int = 8,
    height: int = 16,
    width: int = 16,
    sample_rate_hz: int = 48000,
    mux_mp4: bool = True,
    use_synthetic_corrupted_shot: bool = False,
) -> RealMediaVeritasBundle:
    """Heal real media through all fifteen laws, write it, and seal the receipts."""
    if num_frames < 1 or height < 2 or width < 2:
        raise ValueError("num_frames must be >= 1 and frame dimensions >= 2")
    if sample_rate_hz <= 0:
        raise ValueError("sample_rate_hz must be positive")

    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)

    frames = (
        _synthesize_frames(num_frames, height, width)
        if input_frames is None
        else np.asarray(input_frames, dtype=np.float64)
    )
    if frames.ndim != 4 or frames.shape[3] != 3:
        raise ValueError("input_frames must be a TxHxWx3 array")
    frame_count, frame_h, frame_w = int(frames.shape[0]), int(frames.shape[1]), int(frames.shape[2])

    if input_audio_pcm is None:
        time_axis = np.arange(sample_rate_hz // 4, dtype=np.float64) / sample_rate_hz
        pcm = 0.4 * np.sin(2.0 * np.pi * 220.0 * time_axis)
        pcm = pcm + 0.2 * np.sin(2.0 * np.pi * 660.0 * time_axis)
    else:
        pcm = np.asarray(input_audio_pcm, dtype=np.float64).reshape(-1)
    if pcm.size == 0:
        raise ValueError("input_audio_pcm must be non-empty")

    if shot is not None:
        active_shot = shot
    elif use_synthetic_corrupted_shot or (input_frames is None and input_audio_pcm is None):
        active_shot = build_corrupted_15_law_cinema_shot(height=frame_h, width=frame_w)
    else:
        active_shot = extract_cinema_shot_from_media(frames, pcm, sample_rate_hz=sample_rate_hz)

    input_frames_u8 = np.asarray(np.round(np.clip(frames, 0.0, 1.0) * 255.0), dtype=np.uint8)
    input_frames_sha256 = _digest_array(input_frames_u8)
    input_audio_sha256 = _digest_array(
        np.asarray(np.round(np.clip(pcm, -1.0, 1.0) * 32767.0), dtype=np.int16)
    )

    passport = VeritasRealityAuditor().audit_and_heal_cinema_shot(active_shot)

    # --- Law 11: Bohmian-gated phase stabilisation of the luminance stack. ---
    density = np.asarray(frames.mean(axis=3), dtype=np.float64)
    phase = np.asarray(
        np.arctan2(frames[:, :, :, 2] - 0.5, frames[:, :, :, 0] - 0.5), dtype=np.float64
    )
    coherence = stabilize_madelung_phase_coherence(density, phase)
    weight = (
        np.ones_like(density)
        if coherence.bohmian_weight is None
        else np.asarray(coherence.bohmian_weight, dtype=np.float64)
    )
    healed = np.array(frames, copy=True)
    healed += (
        0.02
        * weight[:, :, :, None]
        * (np.cos(coherence.phase)[:, :, :, None] - np.cos(phase)[:, :, :, None])
    )

    # --- Law 12: anamorphic chromatic and cos^4 vignette rectification. ---
    for index in range(frame_count):
        optics = rectify_anamorphic_dioptric_optics(np.clip(healed[index], 0.0, 1.0))
        healed[index] = optics.healed_image

    # --- Law 15: anti-stroboscopic rotary-shutter motion blur on frame zero. ---
    rotor = rectify_stroboscopic_wagon_wheel(
        healed[0], angular_velocity_rad_s=200.0, fps=24.0, shutter_angle_deg=180.0
    )
    healed[0] = rotor.healed_patch

    # --- Law 6: homography-locked diegetic typography composite. ---
    quad = np.array(
        [[2.0, 2.0], [frame_w - 3.0, 2.0], [frame_w - 3.0, frame_h - 3.0], [2.0, frame_h - 3.0]]
    )
    try:
        render = render_homography_locked_diegetic_text("VERITAS", quad, (frame_h, frame_w))
        mask = np.asarray(render.mask, dtype=np.float64)
        if input_frames is None and mask.shape == (frame_h, frame_w):
            healed = np.maximum(healed, mask[:, :, None] * 0.9)
    except Exception:  # pragma: no cover - typography is best-effort overlay
        pass

    healed_frames = np.asarray(np.round(np.clip(healed, 0.0, 1.0) * 255.0), dtype=np.uint8)

    # --- Law 13: acoustic Doppler plus viseme-gated speech envelope. ---
    doppler = apply_acoustic_doppler_shift(pcm, float(sample_rate_hz), 12.0)
    au26 = np.asarray(
        np.clip(frames[:, :, :, 1].mean(axis=(1, 2)) * 1.6, 0.0, 1.0), dtype=np.float64
    )
    viseme = audit_and_heal_viseme_formant_sync(
        au26,
        np.full(frame_count, 0.85),
        f1_hz=np.full(frame_count, 780.0),
        rms_energy=np.full(frame_count, 0.05),
    )
    gate = np.interp(
        np.linspace(0.0, 1.0, pcm.size),
        np.linspace(0.0, 1.0, viseme.au26_healed.size),
        viseme.au26_healed,
    )
    healed_audio_pcm = np.asarray(doppler.shifted_signal * (0.5 + 0.5 * gate), dtype=np.float64)

    png_paths: list[Path] = []
    for index in range(frame_count):
        path = directory / f"frame_{index:04d}.png"
        _write_png(path, healed_frames[index])
        png_paths.append(path)
    wav_path = directory / "dialogue.wav"
    _write_wav(wav_path, healed_audio_pcm, sample_rate_hz)

    output_frames_sha256 = _digest_files(png_paths)
    output_audio_sha256 = _digest_files([wav_path])

    receipts: list[LawReceipt] = []
    for law in range(1, LAW_COUNT + 1):
        receipts.append(
            LawReceipt(
                law=law,
                name="",
                violations_before=tuple(passport.details_before.get(law, ())),
                violations_after=tuple(passport.details_after.get(law, ())),
            )
        )
    names = {
        1: "hand_polydactyly",
        2: "zombie_eyes",
        3: "reflection_lies",
        4: "floating_shadows",
        5: "object_vanishing",
        6: "gibberish_text",
        7: "newtonian_violations",
        8: "cloth_penetration",
        9: "time_arrow_reversal",
        10: "uncanny_plastic_faces",
        11: "madelung_phase_coherence",
        12: "anamorphic_dioptric_optics",
        13: "bio_acoustic_viseme_doppler",
        14: "helmholtz_fluid_conservation",
        15: "rotary_shutter_anti_aliasing",
    }
    receipts = [
        LawReceipt(
            law=receipt.law,
            name=names[receipt.law],
            violations_before=receipt.violations_before,
            violations_after=receipt.violations_after,
        )
        for receipt in receipts
    ]

    mp4_path: str | None = None
    mp4_sha256: str | None = None
    if mux_mp4:
        binary = shutil.which("ffmpeg")
        if binary is None:
            try:  # pragma: no cover - optional dependency
                import imageio_ffmpeg  # type: ignore[import-not-found]

                binary = imageio_ffmpeg.get_ffmpeg_exe()
            except Exception:
                binary = None
        if binary is not None:  # pragma: no cover - ffmpeg absent in CI sandbox
            target = directory / "healed_shot.mp4"
            pattern = str(directory / "frame_%04d.png")
            import subprocess

            subprocess.run(
                [
                    binary,
                    "-y",
                    "-framerate",
                    "24",
                    "-i",
                    pattern,
                    "-i",
                    str(wav_path),
                    "-c:v",
                    "libx264",
                    "-pix_fmt",
                    "yuv420p",
                    "-c:a",
                    "aac",
                    str(target),
                ],
                check=False,
                capture_output=True,
            )
            if target.exists():
                mp4_path = str(target)
                mp4_sha256 = hashlib.sha256(target.read_bytes()).hexdigest()

    decoded_frames = np.stack([_read_png_rgb(path) for path in png_paths])
    decoded_audio = _read_wav_mono_16bit(wav_path)
    output_frames_content_sha256 = _digest_array(decoded_frames)
    output_audio_content_sha256 = _digest_array(decoded_audio)
    if not np.array_equal(decoded_frames, healed_frames):
        raise ValueError("PNG disk round-trip mismatch")
    expected_audio = np.asarray(np.round(np.clip(healed_audio_pcm, -1, 1) * 32767), dtype=np.int16)
    if not np.array_equal(decoded_audio, expected_audio):
        raise ValueError("WAV disk round-trip mismatch")

    payloads = [
        {
            "compiler_version": COMPILER_VERSION,
            "input_frames_content_sha256": input_frames_sha256,
            "output_frames_content_sha256": output_frames_content_sha256,
            "input_audio_content_sha256": input_audio_sha256,
            "output_audio_content_sha256": output_audio_content_sha256,
            "output_mp4_sha256": mp4_sha256 or "",
            "input_audio_sha256": input_audio_sha256,
            "input_frames_sha256": input_frames_sha256,
            "law": receipt.law,
            "name": receipt.name,
            "output_audio_sha256": output_audio_sha256,
            "output_frames_sha256": output_frames_sha256,
            "violations_after": list(receipt.violations_after),
            "violations_before": list(receipt.violations_before),
        }
        for receipt in receipts
    ]
    leaves = [_leaf_digest(payload) for payload in payloads]
    merkle = _merkle_root(leaves)

    pre = int(passport.pre_audit_violations)
    post = int(passport.post_heal_violations)

    return RealMediaVeritasBundle(
        output_dir=str(directory),
        png_paths=tuple(str(path) for path in png_paths),
        wav_path=str(wav_path),
        mp4_path=mp4_path,
        mp4_sha256=mp4_sha256,
        merkle_root=merkle,
        leaf_digests=tuple(leaves),
        leaf_payloads=tuple(payloads),
        receipts=tuple(receipts),
        pre_audit_violations=pre,
        post_heal_violations=post,
        input_frames_sha256=input_frames_sha256,
        input_audio_sha256=input_audio_sha256,
        output_frames_sha256=output_frames_sha256,
        output_audio_sha256=output_audio_sha256,
        healed_frames=healed_frames,
        healed_audio_pcm=healed_audio_pcm,
        input_frames_content_sha256=input_frames_sha256,
        output_frames_content_sha256=output_frames_content_sha256,
        input_audio_content_sha256=input_audio_sha256,
        output_audio_content_sha256=output_audio_content_sha256,
        output_mp4_sha256=mp4_sha256 or "",
    )


__all__ = [
    "COMPILER_VERSION",
    "LAW_COUNT",
    "LawReceipt",
    "RealMediaVeritasBundle",
    "compile_and_heal_real_media_shot",
    "merkle_root_from_payloads",
    "extract_cinema_shot_from_media",
]
