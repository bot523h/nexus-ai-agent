"""Real-Media PNG/WAV/MP4 Compiler, RFC 6962 Merkle Tree & Fenced NEXUS Spine.

Wave 4 of NAGAR MORPHEUS:
Lossless PNG and 48 kHz PCM16 WAV I/O, deterministic FFmpeg MP4 muxing,
same-domain decoded content SHA-256 alongside on-disk file SHA-256,
exact RFC 6962 unbalanced Merkle tree with inclusion proofs, and two-phase
fenced staging adapter with atomic promotion.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import wave
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image


class SpineError(Exception):
    """Base exception for Morpheus spine errors."""


class FencingTokenStaleError(SpineError):
    """Fencing token mismatch indicates superseded attempt."""


class BundleVerificationError(SpineError):
    """Verification of staged bundle on disk failed."""


class MerkleVerificationError(SpineError):
    """RFC 6962 Merkle proof verification failed."""


# ── Pure-Stdlib / Pillow Lossless Media I/O & FFmpeg Mux ─────────────────────


def write_png_frame(rgb: np.ndarray, path: str | Path) -> None:
    """Write lossless 8-bit RGB PNG frame to disk."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if rgb.ndim != 3 or rgb.shape[2] != 3 or rgb.dtype != np.uint8:
        raise ValueError(f"rgb must be (H, W, 3) uint8 array, got {rgb.shape}, {rgb.dtype}")
    img = Image.fromarray(rgb, mode="RGB")
    img.save(p, format="PNG")


def read_png_frame(path: str | Path) -> np.ndarray:
    """Read lossless 8-bit RGB PNG frame from disk into uint8 array."""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"PNG file not found: {p}")
    with Image.open(p) as img:
        rgb_img = img.convert("RGB")
        return np.asarray(rgb_img, dtype=np.uint8)


def write_wav_pcm16(
    samples: np.ndarray,
    path: str | Path,
    sample_rate: int = 48000,
) -> None:
    """Write 16-bit signed PCM audio WAV file using stdlib wave."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if samples.dtype != np.int16:
        raise ValueError(f"samples must be int16, got {samples.dtype}")
    channels = 1 if samples.ndim == 1 else samples.shape[1]
    with wave.open(str(p), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)  # 16-bit
        wf.setframerate(sample_rate)
        wf.writeframes(samples.tobytes())


def read_wav_pcm16(path: str | Path) -> tuple[np.ndarray, int]:
    """Read 16-bit signed PCM audio WAV file from disk."""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"WAV file not found: {p}")
    with wave.open(str(p), "rb") as wf:
        channels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        sample_rate = wf.getframerate()
        if sampwidth != 2:
            raise ValueError(f"Expected 16-bit WAV (sampwidth=2), got {sampwidth}")
        n_frames = wf.getnframes()
        raw_bytes = wf.readframes(n_frames)

    arr = np.frombuffer(raw_bytes, dtype=np.int16)
    if channels > 1:
        arr = arr.reshape(-1, channels)
    return arr, sample_rate


def mux_mp4_ffmpeg(
    frame_pattern: str,
    audio_path: str | Path,
    output_mp4: str | Path,
    fps: int = 24,
) -> None:
    """Mux PNG sequence and WAV audio into deterministic H.264/MP4 via FFmpeg."""
    out_p = Path(output_mp4)
    out_p.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-y",
        "-framerate",
        str(fps),
        "-i",
        str(frame_pattern),
        "-i",
        str(audio_path),
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        str(out_p),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise SpineError(f"FFmpeg mux failed with code {proc.returncode}:\n{proc.stderr}")
    if not out_p.is_file() or out_p.stat().st_size == 0:
        raise SpineError(f"Output MP4 missing or empty: {out_p}")


# ── True RFC 6962 Unbalanced Merkle Tree (0x00 Leaf, 0x01 Internal Node) ───


def rfc6962_leaf_hash(data: bytes) -> bytes:
    """Compute RFC 6962 leaf hash: SHA-256(0x00 || data)."""
    h = hashlib.sha256()
    h.update(b"\x00")
    h.update(data)
    return h.digest()


def rfc6962_node_hash(left: bytes, right: bytes) -> bytes:
    """Compute RFC 6962 internal node hash: SHA-256(0x01 || left || right)."""
    h = hashlib.sha256()
    h.update(b"\x01")
    h.update(left)
    h.update(right)
    return h.digest()


def largest_power_of_two_less_than(n: int) -> int:
    """Return largest power of two strictly less than n for n > 1."""
    if n <= 1:
        raise ValueError("n must be > 1")
    k = 1
    while (k << 1) < n:
        k <<= 1
    return k


def compute_rfc6962_merkle_root(leaves: list[bytes]) -> bytes:
    """Compute RFC 6962 Merkle tree root hash over a list of raw leaf byte payloads.

    Section 2.1:
      MTH({}) = SHA-256("")
      MTH({d_0}) = SHA-256(0x00 || d_0)
      For n > 1:
        k = 2^(floor(log2(n - 1)))
        MTH(D[0..n]) = SHA-256(0x01 || MTH(D[0..k]) || MTH(D[k..n]))
    Never duplicates odd leaves!
    """
    n = len(leaves)
    if n == 0:
        return hashlib.sha256(b"").digest()
    if n == 1:
        return rfc6962_leaf_hash(leaves[0])
    k = largest_power_of_two_less_than(n)
    left_root = compute_rfc6962_merkle_root(leaves[:k])
    right_root = compute_rfc6962_merkle_root(leaves[k:])
    return rfc6962_node_hash(left_root, right_root)


def generate_rfc6962_inclusion_proof(
    leaves: list[bytes],
    index: int,
) -> tuple[str, list[tuple[str, str]]]:
    """Generate RFC 6962 audit path / inclusion proof for leaf at index.

    Returns (root_hex, proof) where proof is list of ('L'|'R', sibling_hash_hex).
    'L' means sibling is on the left, 'R' means sibling is on the right.
    """
    n = len(leaves)
    if not (0 <= index < n):
        raise ValueError(f"Leaf index {index} out of bounds for {n} leaves")

    proof: list[tuple[str, str]] = []

    def _subproof(sub_leaves: list[bytes], m: int) -> bytes:
        sn = len(sub_leaves)
        if sn == 1:
            return rfc6962_leaf_hash(sub_leaves[0])
        sk = largest_power_of_two_less_than(sn)
        if m < sk:
            right_h = compute_rfc6962_merkle_root(sub_leaves[sk:])
            proof.append(("R", right_h.hex()))
            left_h = _subproof(sub_leaves[:sk], m)
            return rfc6962_node_hash(left_h, right_h)
        left_h = compute_rfc6962_merkle_root(sub_leaves[:sk])
        proof.append(("L", left_h.hex()))
        right_h = _subproof(sub_leaves[sk:], m - sk)
        return rfc6962_node_hash(left_h, right_h)

    root = _subproof(leaves, index)
    # Reverse proof so caller evaluates from leaf up to root
    proof.reverse()
    return root.hex(), proof


def verify_rfc6962_inclusion_proof(
    leaf_bytes: bytes,
    index: int,
    total_leaves: int,
    proof: list[tuple[str, str]],
    expected_root_hex: str,
) -> bool:
    """Verify an RFC 6962 inclusion proof from leaf up to root."""
    if not (0 <= index < total_leaves):
        return False
    current_hash = rfc6962_leaf_hash(leaf_bytes)

    # Reconstruct path from leaf up to root
    for direction, sibling_hex in proof:
        sibling = bytes.fromhex(sibling_hex)
        if direction == "L":
            current_hash = rfc6962_node_hash(sibling, current_hash)
        elif direction == "R":
            current_hash = rfc6962_node_hash(current_hash, sibling)
        else:
            return False

    return current_hash.hex() == expected_root_hex


# ── Fenced Staged Spine Adapter & Asset Version Records ─────────────────────


@dataclass(frozen=True)
class MorpheusBundleDigests:
    """Cryptographic digests separating decoded content from disk encodings."""

    input_frames_content_sha256: str
    output_frames_content_sha256: str
    output_frames_file_sha256: str
    input_audio_content_sha256: str
    output_audio_content_sha256: str
    output_audio_file_sha256: str
    output_mp4_sha256: str
    merkle_root_hex: str


@dataclass(frozen=True)
class MorpheusAssetVersionRecord:
    """Authoritative asset version record produced upon atomic promotion."""

    asset_id: str
    attempt_id: str
    fencing_token: str
    digests: MorpheusBundleDigests
    published_dir: str
    published_mp4_path: str
    manifest_sha256: str


@dataclass(frozen=True)
class MorpheusStageRequest:
    """Request payload for staging and publishing a Morpheus render bundle."""

    asset_id: str
    attempt_id: str
    fencing_token: str
    frames_uint8: tuple[np.ndarray, ...]
    audio_pcm16: np.ndarray
    fps: int = 24
    sample_rate: int = 48000
    audit_metrics: dict[str, float] | None = None


def verify_morpheus_bundle_on_disk(staging_dir: Path) -> MorpheusBundleDigests:
    """Perform post-disk byte and content forensic verification of staged bundle."""
    manifest_p = staging_dir / "manifest.json"
    if not manifest_p.is_file():
        raise BundleVerificationError(f"Missing manifest.json in {staging_dir}")
    with manifest_p.open("r", encoding="utf-8") as f:
        manifest = json.load(f)

    # 1. Verify PNG frames on disk and decoded content
    frame_paths = sorted(staging_dir.glob("frame_*.png"))
    if not frame_paths:
        raise BundleVerificationError("No PNG frames found in staging directory")

    hasher_file_frames = hashlib.sha256()
    hasher_content_frames = hashlib.sha256()
    for fp in frame_paths:
        hasher_file_frames.update(fp.read_bytes())
        dec = read_png_frame(fp)
        hasher_content_frames.update(dec.tobytes())

    out_frames_file_sha = hasher_file_frames.hexdigest()
    out_frames_content_sha = hasher_content_frames.hexdigest()

    if out_frames_file_sha != manifest["digests"]["output_frames_file_sha256"]:
        raise BundleVerificationError("output_frames_file_sha256 mismatch against disk")
    if out_frames_content_sha != manifest["digests"]["output_frames_content_sha256"]:
        raise BundleVerificationError("output_frames_content_sha256 mismatch against disk")

    # 2. Verify WAV audio on disk and decoded content
    wav_p = staging_dir / "audio.wav"
    if not wav_p.is_file():
        raise BundleVerificationError("Missing audio.wav in staging directory")

    out_audio_file_sha = hashlib.sha256(wav_p.read_bytes()).hexdigest()
    dec_audio, _ = read_wav_pcm16(wav_p)
    out_audio_content_sha = hashlib.sha256(dec_audio.tobytes()).hexdigest()

    if out_audio_file_sha != manifest["digests"]["output_audio_file_sha256"]:
        raise BundleVerificationError("output_audio_file_sha256 mismatch against disk")
    if out_audio_content_sha != manifest["digests"]["output_audio_content_sha256"]:
        raise BundleVerificationError("output_audio_content_sha256 mismatch against disk")

    # 3. Verify MP4 file
    mp4_p = staging_dir / "video.mp4"
    if not mp4_p.is_file():
        raise BundleVerificationError("Missing video.mp4 in staging directory")
    out_mp4_sha = hashlib.sha256(mp4_p.read_bytes()).hexdigest()
    if out_mp4_sha != manifest["digests"]["output_mp4_sha256"]:
        raise BundleVerificationError("output_mp4_sha256 mismatch against disk")

    return MorpheusBundleDigests(
        input_frames_content_sha256=manifest["digests"]["input_frames_content_sha256"],
        output_frames_content_sha256=out_frames_content_sha,
        output_frames_file_sha256=out_frames_file_sha,
        input_audio_content_sha256=manifest["digests"]["input_audio_content_sha256"],
        output_audio_content_sha256=out_audio_content_sha,
        output_audio_file_sha256=out_audio_file_sha,
        output_mp4_sha256=out_mp4_sha,
        merkle_root_hex=manifest["digests"]["merkle_root_hex"],
    )


class MorpheusStagedSpineAdapter:
    """Fenced staging adapter guaranteeing atomic promotion and zero partial writes."""

    def __init__(
        self,
        workspace_root: Path,
        active_fencing_token_getter: Callable[[], str],
        pre_publish_tamper_hook: Callable[[Path], None] | None = None,
    ) -> None:
        self.workspace_root = workspace_root.resolve()
        self.active_fencing_token_getter = active_fencing_token_getter
        self.pre_publish_tamper_hook = pre_publish_tamper_hook

    def stage_and_publish(self, request: MorpheusStageRequest) -> MorpheusAssetVersionRecord:
        """Stage media bundle under attempt isolation, verify, and atomically publish."""
        # 1. Phase 1 Fencing Check: verify active owner before staging begins
        active_token_1 = self.active_fencing_token_getter()
        if active_token_1 != request.fencing_token:
            msg = (
                f"Fencing token stale before staging: active={active_token_1} "
                f"!= request={request.fencing_token}"
            )
            raise FencingTokenStaleError(msg)

        staging_dir = (
            self.workspace_root / ".staging" / f"{request.attempt_id}_{request.fencing_token}"
        )
        if staging_dir.exists():
            shutil.rmtree(staging_dir)
        staging_dir.mkdir(parents=True, exist_ok=True)

        try:
            # 2. Compute input content digests
            in_frames_hasher = hashlib.sha256()
            for frame in request.frames_uint8:
                in_frames_hasher.update(frame.tobytes())
            in_frames_sha = in_frames_hasher.hexdigest()
            in_audio_sha = hashlib.sha256(request.audio_pcm16.tobytes()).hexdigest()

            # 3. Write PNG frames to staging
            frame_pattern = str(staging_dir / "frame_%04d.png")
            out_file_frames_hasher = hashlib.sha256()
            out_content_frames_hasher = hashlib.sha256()

            for idx, frame in enumerate(request.frames_uint8):
                fp = staging_dir / f"frame_{idx:04d}.png"
                write_png_frame(frame, fp)
                out_file_frames_hasher.update(fp.read_bytes())
                # Re-read and decode back
                dec_frame = read_png_frame(fp)
                out_content_frames_hasher.update(dec_frame.tobytes())

            out_frames_file_sha = out_file_frames_hasher.hexdigest()
            out_frames_content_sha = out_content_frames_hasher.hexdigest()

            # 4. Write WAV audio to staging
            wav_path = staging_dir / "audio.wav"
            write_wav_pcm16(request.audio_pcm16, wav_path, sample_rate=request.sample_rate)
            out_audio_file_sha = hashlib.sha256(wav_path.read_bytes()).hexdigest()
            dec_audio, _ = read_wav_pcm16(wav_path)
            out_audio_content_sha = hashlib.sha256(dec_audio.tobytes()).hexdigest()

            # 5. Mux MP4 via FFmpeg
            mp4_path = staging_dir / "video.mp4"
            mux_mp4_ffmpeg(frame_pattern, wav_path, mp4_path, fps=request.fps)
            out_mp4_sha = hashlib.sha256(mp4_path.read_bytes()).hexdigest()

            # 6. Assemble RFC 6962 Merkle tree
            merkle_leaves: list[bytes] = [
                f"in_frames:{in_frames_sha}".encode(),
                f"out_frames_content:{out_frames_content_sha}".encode(),
                f"out_frames_file:{out_frames_file_sha}".encode(),
                f"in_audio:{in_audio_sha}".encode(),
                f"out_audio_content:{out_audio_content_sha}".encode(),
                f"out_audio_file:{out_audio_file_sha}".encode(),
                f"out_mp4:{out_mp4_sha}".encode(),
            ]
            if request.audit_metrics:
                for k, v in sorted(request.audit_metrics.items()):
                    merkle_leaves.append(f"metric:{k}:{v:.8f}".encode())

            merkle_root = compute_rfc6962_merkle_root(merkle_leaves).hex()

            digests = MorpheusBundleDigests(
                input_frames_content_sha256=in_frames_sha,
                output_frames_content_sha256=out_frames_content_sha,
                output_frames_file_sha256=out_frames_file_sha,
                input_audio_content_sha256=in_audio_sha,
                output_audio_content_sha256=out_audio_content_sha,
                output_audio_file_sha256=out_audio_file_sha,
                output_mp4_sha256=out_mp4_sha,
                merkle_root_hex=merkle_root,
            )

            manifest_dict = {
                "asset_id": request.asset_id,
                "attempt_id": request.attempt_id,
                "fencing_token": request.fencing_token,
                "fps": request.fps,
                "sample_rate": request.sample_rate,
                "digests": {
                    "input_frames_content_sha256": digests.input_frames_content_sha256,
                    "output_frames_content_sha256": digests.output_frames_content_sha256,
                    "output_frames_file_sha256": digests.output_frames_file_sha256,
                    "input_audio_content_sha256": digests.input_audio_content_sha256,
                    "output_audio_content_sha256": digests.output_audio_content_sha256,
                    "output_audio_file_sha256": digests.output_audio_file_sha256,
                    "output_mp4_sha256": digests.output_mp4_sha256,
                    "merkle_root_hex": digests.merkle_root_hex,
                },
            }
            manifest_bytes = json.dumps(manifest_dict, indent=2, sort_keys=True).encode("utf-8")
            manifest_path = staging_dir / "manifest.json"
            manifest_path.write_bytes(manifest_bytes)
            manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()

            # 7. Optional pre-publish tamper hook (for Popperian tamper tests)
            if self.pre_publish_tamper_hook is not None:
                self.pre_publish_tamper_hook(staging_dir)

            # 8. Post-disk verification
            verify_morpheus_bundle_on_disk(staging_dir)

            # 9. Phase 2 Fencing Check: verify active owner immediately before atomic promotion
            active_token_2 = self.active_fencing_token_getter()
            if active_token_2 != request.fencing_token:
                msg2 = (
                    f"Fencing token stale before promotion: active={active_token_2} "
                    f"!= request={request.fencing_token}"
                )
                raise FencingTokenStaleError(msg2)

            # 10. Atomic promotion into published root
            published_dir = self.workspace_root / "published" / request.asset_id
            if published_dir.exists():
                shutil.rmtree(published_dir)
            published_dir.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staging_dir, published_dir)

            return MorpheusAssetVersionRecord(
                asset_id=request.asset_id,
                attempt_id=request.attempt_id,
                fencing_token=request.fencing_token,
                digests=digests,
                published_dir=str(published_dir),
                published_mp4_path=str(published_dir / "video.mp4"),
                manifest_sha256=manifest_sha256,
            )

        finally:
            if staging_dir.exists():
                shutil.rmtree(staging_dir)
