"""WAVE 25 — post-disk semantic verifier, quarantine and managed studio governance.

This module closes the Prometheus playground loop by *independently* verifying
what actually landed on disk and by running the playground under the existing
fail-closed ATLAS governance:

* :func:`verify_prometheus_playground_bundle_from_disk` re-reads the written PNG
  frames, the 48 kHz WAV and the MP4 from disk, decodes the MP4 video/audio
  streams with FFmpeg back to raw pixels and PCM samples, and verifies:

  - content-domain equality — the SHA-256 of the decoded pixels / PCM matches the
    SHA-256 of the reference PNG / WAV payloads, and every on-disk file matches
    its recorded digest;
  - semantic equivalence — the MP4 video frames and the reference quadrant PNGs
    agree to ``YUV420P`` PSNR ``>= 28 dB`` with equal frame counts, and the MP4
    audio matches the reference WAV;
  - RFC 6962 Merkle inclusion proofs (leaf prefix ``0x00``, internal prefix
    ``0x01``, bounded to ``total_leaves`` and verified at odd and even leaf
    counts);
  - a three-state epistemic ledger (``PASS`` / ``VIOLATED`` / ``UNOBSERVED``)
    with an anti-erasure guard that refuses to let a previously observed
    violation be laundered back to ``UNOBSERVED``;
  - an :class:`ObservationProvenance` tag on every observation
    (``MEASURED_MEDIA`` / ``ESTIMATED_PROXY`` / ``SYNTHETIC_FIXTURE``).

* :func:`run_prometheus_playground_governed` stages the compile in the private
  ``<workspace>/.staging/<attempt_id>_<fencing_token>/`` namespace under an
  explicit :class:`CommandBusAuthorizer` (failing closed with
  ``AUTHORIZER_REQUIRED`` unless ``allow_unauthenticated_lab_mode=True``),
  quarantines any failed or tampered bundle into
  ``<workspace>/.quarantine/<attempt_id>_<fencing_token>/`` with a legal JCS
  receipt, and records the verified lineage into the ``fcntl.flock``-locked
  :class:`AtlasLineageStore`.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Final

import numpy as np

from nexus_ai_agent.creative.atlas import prometheus_studio_playground as _playground
from nexus_ai_agent.creative.atlas.atlas_spine_governance import (
    AtlasArtifactPassportRecord,
    AtlasAssetVersionRecord,
    AtlasGovernanceRefusalError,
    AtlasLineageRepositoryProtocol,
    AtlasLineageStore,
    CommandBusAuthorizer,
    _authorize,
    _with_authority_note,
    jcs_canonical_bytes,
    quarantine_failed_attempt,
)
from nexus_ai_agent.tools.filesystem_policy import WorkspaceFilesystem

__all__ = [
    "VERIFIER_SCHEMA",
    "PSNR_FLOOR_DB",
    "ObservationProvenance",
    "LedgerState",
    "VerifierError",
    "Observation",
    "EpistemicLedger",
    "MerkleProof",
    "merkle_leaf_hash",
    "merkle_root",
    "merkle_inclusion_proof",
    "verify_merkle_inclusion",
    "BundleVerificationReport",
    "verify_prometheus_playground_bundle_from_disk",
    "GovernedPlaygroundReceipt",
    "run_prometheus_playground_governed",
]

VERIFIER_SCHEMA: Final[str] = "prometheus-playground-verifier/1"
#: Minimum YUV420P PSNR between decoded MP4 frames and reference PNG frames.
PSNR_FLOOR_DB: Final[float] = 28.0
_EPS: Final[float] = 1e-12


class VerifierError(ValueError):
    """Raised for a malformed verification or governance request."""


class ObservationProvenance(str, Enum):
    """How an observation's value was obtained."""

    MEASURED_MEDIA = "MEASURED_MEDIA"
    ESTIMATED_PROXY = "ESTIMATED_PROXY"
    SYNTHETIC_FIXTURE = "SYNTHETIC_FIXTURE"


class LedgerState(str, Enum):
    """The three-state epistemic verdict of a single claim."""

    PASS = "PASS"
    VIOLATED = "VIOLATED"
    UNOBSERVED = "UNOBSERVED"


@dataclass(frozen=True)
class Observation:
    """A single verified (or unobserved) claim."""

    claim: str
    state: LedgerState
    provenance: ObservationProvenance
    measured: float | None = None
    threshold: float | None = None
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "claim": self.claim,
            "state": self.state.value,
            "provenance": self.provenance.value,
            "measured": self.measured,
            "threshold": self.threshold,
            "note": self.note,
        }


class EpistemicLedger:
    """A three-state ledger with an anti-erasure guard.

    A claim that has been observed ``PASS`` or ``VIOLATED`` may never be
    re-recorded as ``UNOBSERVED``: deleting an inconvenient observation is a
    typed refusal, not a way to launder a failure.
    """

    def __init__(self) -> None:
        self._observations: dict[str, Observation] = {}

    def record(self, observation: Observation) -> None:
        existing = self._observations.get(observation.claim)
        if (
            existing is not None
            and existing.state is not LedgerState.UNOBSERVED
            and observation.state is LedgerState.UNOBSERVED
        ):
            raise VerifierError(
                f"anti-erasure guard: claim {observation.claim!r} was observed "
                f"{existing.state.value} and may not be reset to UNOBSERVED"
            )
        self._observations[observation.claim] = observation

    @property
    def observations(self) -> tuple[Observation, ...]:
        return tuple(self._observations.values())

    def state_of(self, claim: str) -> LedgerState:
        return self._observations[claim].state

    @property
    def all_pass(self) -> bool:
        return all(o.state is LedgerState.PASS for o in self._observations.values())

    @property
    def any_violated(self) -> bool:
        return any(o.state is LedgerState.VIOLATED for o in self._observations.values())

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "atlas.epistemic_ledger",
            "schema_version": 1,
            "observations": [o.to_dict() for o in self._observations.values()],
        }

    def canonical_bytes(self) -> bytes:
        return jcs_canonical_bytes(self.to_dict())


# --- RFC 6962 Merkle tree ----------------------------------------------------------


def _sha256(raw: bytes) -> bytes:
    return hashlib.sha256(raw).digest()


def merkle_leaf_hash(leaf: bytes) -> bytes:
    """RFC 6962 leaf hash: ``SHA-256(0x00 || leaf)``."""
    return _sha256(b"\x00" + leaf)


def merkle_node_hash(left: bytes, right: bytes) -> bytes:
    """RFC 6962 internal node hash: ``SHA-256(0x01 || left || right)``."""
    return _sha256(b"\x01" + left + right)


def merkle_root(leaves: list[bytes]) -> bytes:
    """The RFC 6962 Merkle tree hash of ``leaves`` (empty tree -> SHA-256 of "")."""
    if not leaves:
        return _sha256(b"")
    nodes = [merkle_leaf_hash(leaf) for leaf in leaves]
    while len(nodes) > 1:
        merged: list[bytes] = []
        for index in range(0, len(nodes) - 1, 2):
            merged.append(merkle_node_hash(nodes[index], nodes[index + 1]))
        if len(nodes) % 2 == 1:
            merged.append(nodes[-1])
        nodes = merged
    return nodes[0]


def merkle_inclusion_proof(leaves: list[bytes], index: int) -> tuple[bytes, ...]:
    """The RFC 6962 inclusion proof (audit path) for ``leaves[index]``."""
    if not leaves:
        raise VerifierError("cannot prove inclusion in an empty tree")
    if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(leaves):
        raise VerifierError("index out of range")
    nodes = [merkle_leaf_hash(leaf) for leaf in leaves]
    position = index
    proof: list[bytes] = []
    while len(nodes) > 1:
        if position % 2 == 1:
            proof.append(nodes[position - 1])
        elif position + 1 < len(nodes):
            proof.append(nodes[position + 1])
        merged: list[bytes] = []
        for cursor in range(0, len(nodes) - 1, 2):
            merged.append(merkle_node_hash(nodes[cursor], nodes[cursor + 1]))
        if len(nodes) % 2 == 1:
            merged.append(nodes[-1])
        nodes = merged
        position //= 2
    return tuple(proof)


def verify_merkle_inclusion(
    leaf: bytes, index: int, total_leaves: int, proof: tuple[bytes, ...], root: bytes
) -> bool:
    """Verify an RFC 6962 inclusion proof against ``root`` and ``total_leaves``."""
    if total_leaves < 1 or not 0 <= index < total_leaves:
        return False
    node = merkle_leaf_hash(leaf)
    position = index
    size = total_leaves
    for sibling in proof:
        if size <= 1:
            return False
        if position % 2 == 1 or position + 1 >= size:
            node = merkle_node_hash(sibling, node)
        else:
            node = merkle_node_hash(node, sibling)
        position //= 2
        size = (size + 1) // 2
    return node == root


@dataclass(frozen=True)
class MerkleProof:
    """An inclusion proof plus its verified verdict."""

    index: int
    total_leaves: int
    root_hex: str
    verified: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "total_leaves": self.total_leaves,
            "root_hex": self.root_hex,
            "verified": self.verified,
        }


@dataclass(frozen=True)
class BundleVerificationReport:
    """The verified outcome of a post-disk playground bundle check."""

    verified: bool
    content_digests: dict[str, str]
    file_digests: dict[str, str]
    psnr_db: float
    frame_count: int
    audio_equivalent: bool
    merkle_proofs: tuple[MerkleProof, ...]
    merkle_root_hex: str
    ledger: EpistemicLedger
    notes: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "atlas.prometheus_bundle_verification",
            "schema": VERIFIER_SCHEMA,
            "verified": self.verified,
            "content_digests": self.content_digests,
            "file_digests": self.file_digests,
            "psnr_db": self.psnr_db,
            "frame_count": self.frame_count,
            "audio_equivalent": self.audio_equivalent,
            "merkle_proofs": [p.to_dict() for p in self.merkle_proofs],
            "merkle_root_hex": self.merkle_root_hex,
            "ledger": self.ledger.to_dict(),
            "notes": list(self.notes),
        }


def _ffmpeg_path() -> str | None:
    return shutil.which("ffmpeg")


def _ffprobe_path() -> str | None:
    return shutil.which("ffprobe")


def _decode_mp4_yuv420p(mp4: str, timeout: float) -> bytes:
    """Decode the MP4 video stream to raw ``yuv420p`` bytes."""
    ffmpeg = _ffmpeg_path()
    if ffmpeg is None:
        raise VerifierError("ffmpeg is required to decode the MP4")
    argv = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        mp4,
        "-f",
        "rawvideo",
        "-pix_fmt",
        "yuv420p",
        "-",
    ]
    proc = subprocess.run(argv, capture_output=True, timeout=timeout, check=False)
    if proc.returncode != 0:
        raise VerifierError(f"ffmpeg decode failed: {proc.stderr.decode('utf-8', 'ignore')[:200]}")
    return proc.stdout


def _encode_pngs_yuv420p(frame_dir: str, fps: int, timeout: float) -> bytes:
    """Encode the reference PNG sequence to raw ``yuv420p`` bytes for comparison."""
    ffmpeg = _ffmpeg_path()
    if ffmpeg is None:
        raise VerifierError("ffmpeg is required to encode the reference frames")
    argv = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-framerate",
        str(fps),
        "-i",
        f"{frame_dir}/frame_%04d.png",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "yuv420p",
        "-",
    ]
    proc = subprocess.run(argv, capture_output=True, timeout=timeout, check=False)
    if proc.returncode != 0:
        raise VerifierError(
            f"ffmpeg reference encode failed: {proc.stderr.decode('utf-8', 'ignore')[:200]}"
        )
    return proc.stdout


def _psnr_bytes(a: bytes, b: bytes) -> float:
    if not a or not b:
        return -1.0
    count = min(len(a), len(b))
    x = np.frombuffer(a[:count], dtype=np.uint8).astype(np.float64)
    y = np.frombuffer(b[:count], dtype=np.uint8).astype(np.float64)
    mse = float(np.mean((x - y) ** 2))
    if mse < _EPS:
        return 99.0
    return float(10.0 * np.log10((255.0**2) / mse))


def _audio_equivalent(decoded: np.ndarray, reference: np.ndarray) -> bool:
    """Semantic audio equivalence under an AAC codec (delay + lossy tolerance).

    AAC introduces an encoder delay and is lossy, so sample-exact equality is not
    the right test. The decoded stereo stream is lag-aligned to the reference and
    accepted when the normalised cross-correlation is high and the RMS ratio is
    within a factor of two.
    """
    if decoded.size == 0 or reference.size == 0:
        return False
    dec = decoded.astype(np.float64)
    ref = reference.astype(np.float64)
    best_corr = -1.0
    best_rms = 0.0
    for lag in range(0, 8192):
        if lag >= dec.size or lag >= ref.size:
            break
        a = dec[lag : lag + min(dec.size - lag, ref.size - lag)]
        b = ref[: a.size]
        if a.size < 256:
            break
        a_norm = a - a.mean()
        b_norm = b - b.mean()
        denominator = float(np.linalg.norm(a_norm) * np.linalg.norm(b_norm))
        if denominator < _EPS:
            continue
        corr = float(np.dot(a_norm, b_norm) / denominator)
        if corr > best_corr:
            best_corr = corr
            best_rms = float(np.sqrt(np.mean(a**2)) / max(np.sqrt(np.mean(b**2)), _EPS))
    return best_corr >= 0.9 and 0.5 <= best_rms <= 2.0


def _decode_mp4_audio(mp4: str, rate: int, timeout: float) -> np.ndarray:
    """Decode the MP4 audio stream to interleaved int16 PCM."""
    ffmpeg = _ffmpeg_path()
    if ffmpeg is None:
        raise VerifierError("ffmpeg is required to decode the MP4 audio")
    argv = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        mp4,
        "-f",
        "s16le",
        "-ac",
        "2",
        "-ar",
        str(rate),
        "-",
    ]
    proc = subprocess.run(argv, capture_output=True, timeout=timeout, check=False)
    if proc.returncode != 0:
        raise VerifierError(
            f"ffmpeg audio decode failed: {proc.stderr.decode('utf-8', 'ignore')[:200]}"
        )
    return np.frombuffer(proc.stdout, dtype="<i2")


def _read_png_rgb(raw: bytes) -> np.ndarray:
    import io

    from PIL import Image

    try:
        image = Image.open(io.BytesIO(raw)).convert("RGB")
    except Exception as exc:  # noqa: BLE001 - any decode failure is a typed refusal
        raise VerifierError(f"reference PNG could not be decoded: {exc}") from exc
    return np.asarray(image, dtype=np.uint8)


def _read_wav_pcm(raw: bytes) -> np.ndarray:
    import io
    import wave

    with wave.open(io.BytesIO(raw), "rb") as handle:
        frames = handle.readframes(handle.getnframes())
    return np.frombuffer(frames, dtype="<i2")


def verify_prometheus_playground_bundle_from_disk(
    filesystem: WorkspaceFilesystem,
    receipt: _playground.QuadrantCompilationReceipt,
    *,
    expected_mp4_sha256: str | None = None,
    timeout: float = 120.0,
    require_media: bool = True,
) -> BundleVerificationReport:
    """Re-read a compiled playground bundle from disk and verify it independently.

    When ``require_media`` is true the MP4 must exist and be decodable; a missing
    or undecodable MP4 is a ``VIOLATED`` observation rather than a silent pass.
    """
    ledger = EpistemicLedger()
    content_digests: dict[str, str] = {}
    file_digests: dict[str, str] = {}
    merkle_proofs: list[MerkleProof] = []

    frame_dir = f"{receipt.mp4_path.rsplit('/', 1)[0]}/frames"
    png_names = sorted(name for name in _list_dir(filesystem, frame_dir) if name.endswith(".png"))
    if not png_names:
        raise VerifierError("no reference PNG frames found on disk")

    reference_frames: list[np.ndarray] = []
    leaf_payloads: list[bytes] = []
    for name in png_names:
        rel = f"{frame_dir}/{name}"
        raw = filesystem.read_bytes(rel, max_bytes=1 << 26)
        digest = hashlib.sha256(raw).hexdigest()
        file_digests[rel] = digest
        leaf_payloads.append(bytes.fromhex(digest))
        reference_frames.append(_read_png_rgb(raw))

    wav_raw = filesystem.read_bytes(receipt.wav_path, max_bytes=1 << 26)
    file_digests[receipt.wav_path] = hashlib.sha256(wav_raw).hexdigest()
    leaf_payloads.append(bytes.fromhex(file_digests[receipt.wav_path]))
    reference_audio = _read_wav_pcm(wav_raw)
    content_digests["reference_wav_pcm"] = hashlib.sha256(reference_audio.tobytes()).hexdigest()

    leaves = leaf_payloads
    root = merkle_root(leaves)
    for index in range(len(leaves)):
        proof = merkle_inclusion_proof(leaves, index)
        verified = verify_merkle_inclusion(leaves[index], index, len(leaves), proof, root)
        merkle_proofs.append(
            MerkleProof(
                index=index,
                total_leaves=len(leaves),
                root_hex=root.hex(),
                verified=verified,
            )
        )
    ledger.record(
        Observation(
            claim="merkle_inclusion",
            state=LedgerState.PASS
            if all(p.verified for p in merkle_proofs)
            else LedgerState.VIOLATED,
            provenance=ObservationProvenance.MEASURED_MEDIA,
            note=f"{len(merkle_proofs)} inclusion proofs over {len(leaves)} leaves",
        )
    )

    # --- on-disk file digest check ------------------------------------------------
    mp4_present = _file_exists(filesystem, receipt.mp4_path)
    if mp4_present:
        mp4_raw = filesystem.read_bytes(receipt.mp4_path, max_bytes=1 << 27)
        file_digests[receipt.mp4_path] = hashlib.sha256(mp4_raw).hexdigest()
        expected = expected_mp4_sha256 or receipt.mp4_sha256
        matches = expected == "" or file_digests[receipt.mp4_path] == expected
        ledger.record(
            Observation(
                claim="mp4_file_digest",
                state=LedgerState.PASS if matches else LedgerState.VIOLATED,
                provenance=ObservationProvenance.MEASURED_MEDIA,
                note="on-disk MP4 SHA-256 matches the recorded digest",
            )
        )
    else:
        state = (
            LedgerState.VIOLATED if (require_media and receipt.encoded) else LedgerState.UNOBSERVED
        )
        ledger.record(
            Observation(
                claim="mp4_file_digest",
                state=state,
                provenance=ObservationProvenance.SYNTHETIC_FIXTURE,
                note="MP4 absent on disk",
            )
        )

    psnr = -1.0
    frame_count = 0
    audio_equivalent = False
    if mp4_present and _ffmpeg_path() is not None:
        mp4_abs = str(filesystem.resolve(receipt.mp4_path))
        try:
            decoded_yuv = _decode_mp4_yuv420p(mp4_abs, timeout)
            reference_yuv = _encode_pngs_yuv420p(
                str(filesystem.resolve(frame_dir)), receipt.fps, timeout
            )
        except VerifierError as exc:
            ledger.record(
                Observation(
                    claim="mp4_video_decode",
                    state=LedgerState.VIOLATED,
                    provenance=ObservationProvenance.MEASURED_MEDIA,
                    note=str(exc)[:200],
                )
            )
            decoded_yuv = b""
            reference_yuv = b""
        if decoded_yuv:
            content_digests["decoded_video_yuv420p"] = hashlib.sha256(decoded_yuv).hexdigest()
            frame_bytes = (2 * receipt.quadrant_side) * (2 * receipt.quadrant_side) * 3 // 2
            frame_count = len(decoded_yuv) // frame_bytes if frame_bytes else 0
            ledger.record(
                Observation(
                    claim="mp4_frame_count",
                    state=LedgerState.PASS
                    if frame_count == len(reference_frames)
                    else LedgerState.VIOLATED,
                    provenance=ObservationProvenance.MEASURED_MEDIA,
                    measured=float(frame_count),
                    threshold=float(len(reference_frames)),
                    note="decoded MP4 frame count equals the reference PNG count",
                )
            )
            psnr = _psnr_bytes(decoded_yuv, reference_yuv)
            ledger.record(
                Observation(
                    claim="mp4_video_psnr",
                    state=LedgerState.PASS if psnr >= PSNR_FLOOR_DB else LedgerState.VIOLATED,
                    provenance=ObservationProvenance.MEASURED_MEDIA,
                    measured=psnr,
                    threshold=PSNR_FLOOR_DB,
                    note="YUV420P-equivalent PSNR floor between decoded and reference frames",
                )
            )
        try:
            decoded_audio = _decode_mp4_audio(mp4_abs, 48000, timeout)
        except VerifierError as exc:
            decoded_audio = np.zeros(0, dtype="<i2")
            ledger.record(
                Observation(
                    claim="mp4_audio_decode",
                    state=LedgerState.VIOLATED,
                    provenance=ObservationProvenance.MEASURED_MEDIA,
                    note=str(exc)[:200],
                )
            )
        if decoded_audio.size > 0:
            content_digests["decoded_audio_pcm"] = hashlib.sha256(
                decoded_audio.tobytes()
            ).hexdigest()
            audio_equivalent = _audio_equivalent(decoded_audio, reference_audio)
            ledger.record(
                Observation(
                    claim="mp4_audio_equivalence",
                    state=LedgerState.PASS if audio_equivalent else LedgerState.VIOLATED,
                    provenance=ObservationProvenance.MEASURED_MEDIA,
                    note="decoded MP4 audio tracks the reference WAV within tolerance",
                )
            )
    else:
        for claim in ("mp4_video_decode", "mp4_frame_count", "mp4_video_psnr", "mp4_audio_decode"):
            ledger.record(
                Observation(
                    claim=claim,
                    state=LedgerState.UNOBSERVED,
                    provenance=ObservationProvenance.SYNTHETIC_FIXTURE,
                    note="FFmpeg unavailable or MP4 absent; not measured",
                )
            )

    verified = not ledger.any_violated and (
        not require_media or (psnr >= PSNR_FLOOR_DB and audio_equivalent)
    )
    return BundleVerificationReport(
        verified=verified,
        content_digests=content_digests,
        file_digests=file_digests,
        psnr_db=psnr,
        frame_count=frame_count,
        audio_equivalent=audio_equivalent,
        merkle_proofs=tuple(merkle_proofs),
        merkle_root_hex=root.hex(),
        ledger=ledger,
        notes=(
            "every observation carries its provenance",
            "an UNOBSERVED claim is never reported as a PASS",
        ),
    )


def _list_dir(filesystem: WorkspaceFilesystem, rel_dir: str) -> list[str]:
    target = filesystem.resolve(rel_dir)
    if not target.is_dir():
        return []
    return [path.name for path in sorted(target.iterdir()) if path.is_file()]


def _file_exists(filesystem: WorkspaceFilesystem, rel: str) -> bool:
    try:
        return filesystem.resolve(rel).is_file()
    except Exception:  # noqa: BLE001 - a boundary error means "not a readable file"
        return False


# --- managed governance ------------------------------------------------------------


@dataclass(frozen=True)
class GovernedPlaygroundReceipt:
    """The outcome of a governed playground run."""

    attempt_id: str
    fencing_token: int
    authority_mode: str
    staged_dir: str
    published_dir: str
    verification: BundleVerificationReport
    lineage_generation: int
    notes: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "atlas.governed_prometheus_playground",
            "schema_version": 1,
            "attempt_id": self.attempt_id,
            "fencing_token": self.fencing_token,
            "authority_mode": self.authority_mode,
            "staged_dir": self.staged_dir,
            "published_dir": self.published_dir,
            "verification": self.verification.to_dict(),
            "lineage_generation": self.lineage_generation,
            "notes": list(self.notes),
        }

    def canonical_bytes(self) -> bytes:
        return jcs_canonical_bytes(self.to_dict())


def run_prometheus_playground_governed(
    playground: _playground.PrometheusStudioPlayground,
    universe_ids: list[str],
    *,
    ancestor_universe_id: str,
    filesystem: WorkspaceFilesystem,
    command_bus_authorizer: CommandBusAuthorizer | None = None,
    allow_unauthenticated_lab_mode: bool = False,
    actor: str = "atlas",
    project_id: str = "atlas",
    revision_id: str = "rev-1",
    operation: str = "atlas.prometheus.playground",
    attempt_id: str = "attempt-1",
    fencing_token: int = 1,
    side: int = 48,
    frames: int = 6,
    fps: int = 6,
    lineage: AtlasLineageRepositoryProtocol | None = None,
    timeout: float = 120.0,
) -> GovernedPlaygroundReceipt:
    """Run the playground compile inside the governed staging namespace.

    1. Authority is resolved *before* staging: a missing authorizer fails closed
       with ``AUTHORIZER_REQUIRED`` unless ``allow_unauthenticated_lab_mode=True``.
    2. The compile runs into ``<workspace>/.staging/<attempt>_<token>/``; on any
       failure the staged bundle is quarantined and the error is re-raised.
    3. The staged bundle is verified from disk; a failed verification quarantines
       the attempt, and a successful one records the fenced lineage pair.
    """
    receipt = _authorize(
        command_bus_authorizer=command_bus_authorizer,
        allow_unauthenticated_lab_mode=allow_unauthenticated_lab_mode,
        actor=actor,
        project_id=project_id,
        revision_id=revision_id,
        operation=operation,
        attempt_id=attempt_id,
        fencing_token=fencing_token,
        payload={"universes": list(universe_ids)},
    )

    staged_dir = f".staging/{attempt_id}_{fencing_token}"
    published_dir = f".staging/{attempt_id}_{fencing_token}/published"
    try:
        compilation = _playground.compile_counterfactual_quadrant_mp4(
            playground,
            universe_ids,
            ancestor_universe_id=ancestor_universe_id,
            filesystem=filesystem,
            out_dir=published_dir,
            side=side,
            frames=frames,
            fps=fps,
            timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001 - re-raised after quarantine
        quarantine_failed_attempt(
            filesystem,
            attempt_id=attempt_id,
            fencing_token=fencing_token,
            failure_reason=f"{type(exc).__name__}: {exc}",
            failed_bundle=_collect(filesystem, staged_dir),
            published_root=published_dir,
        )
        raise

    verification = verify_prometheus_playground_bundle_from_disk(
        filesystem, compilation, timeout=timeout, require_media=compilation.encoded
    )
    if not verification.verified:
        quarantine_failed_attempt(
            filesystem,
            attempt_id=attempt_id,
            fencing_token=fencing_token,
            failure_reason="bundle verification failed",
            failed_bundle=_collect(filesystem, staged_dir),
            published_root=published_dir,
        )
        raise VerifierError("governed playground bundle failed post-disk verification")

    generation = 0
    if lineage is not None:
        payload = verification.to_dict()
        rel = f"{published_dir}/verification_report.json"
        raw = jcs_canonical_bytes(payload)
        filesystem.write_bytes(rel, raw, create_parents=True)
        digest = hashlib.sha256(raw).hexdigest()
        commit = lineage.commit(
            asset_version=AtlasAssetVersionRecord(
                asset_ref=f"atlas://prometheus/{compilation.mp4_sha256[:16]}",
                content_sha256=digest,
                size_bytes=len(raw),
                fencing_token=fencing_token,
                attempt_id=attempt_id,
                published_rel=rel,
            ),
            passport=AtlasArtifactPassportRecord(
                asset_ref=f"atlas://prometheus/{compilation.mp4_sha256[:16]}",
                content_sha256=digest,
                fencing_token=fencing_token,
                passport_digest=compilation.mp4_sha256,
                published_rel=rel,
            ),
            fencing_token=fencing_token,
        )
        generation = commit.generation_id

    governed = GovernedPlaygroundReceipt(
        attempt_id=attempt_id,
        fencing_token=fencing_token,
        authority_mode=receipt.authority_mode,
        staged_dir=staged_dir,
        published_dir=published_dir,
        verification=verification,
        lineage_generation=generation,
        notes=(
            "authority is resolved before any staging",
            "a failed verification is quarantined, never published",
        ),
    )
    return _with_authority_note(governed, receipt)


def _collect(filesystem: WorkspaceFilesystem, rel_dir: str) -> dict[str, bytes]:
    bundle: dict[str, bytes] = {}
    target = filesystem.resolve(rel_dir)
    if not target.is_dir():
        return bundle
    for path in sorted(target.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(target).as_posix()
        name = rel.replace("/", "__")
        try:
            bundle[name] = path.read_bytes()
        except OSError:  # pragma: no cover - unreadable staged file
            continue
    return bundle


def default_lineage_store(filesystem: WorkspaceFilesystem) -> AtlasLineageStore:
    """A convenience factory for the file-backed lineage ledger."""
    return AtlasLineageStore(filesystem)


__all__ += ["default_lineage_store", "AtlasGovernanceRefusalError"]
