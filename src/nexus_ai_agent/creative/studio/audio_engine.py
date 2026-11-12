"""The port the audio pack's pure handlers call out through (OMEGA-02).

The pack substrate rule is not negotiable: ``creative/packs/audio/`` may import
only stdlib + pydantic + ``creative.studio`` and must contain no I/O
(``tests/architecture/test_audio_pack_boundary.py`` enforces it with an AST
walk).  At the same time the phase's whole point is that the audio handlers stop
*simulating* and start *executing*.

The seam that satisfies both is the one the repository already uses elsewhere
(caption engine, blob store): a typed **port** that the handler receives from its
composition root.  The handler stays pure code that validates, decides and
returns evidence; the port owns every byte that touches the disk.

The port's *contract* lives here, next to the other studio contracts, because
that is the only package the pack is allowed to import.  The implementation
(:class:`nexus_ai_agent.creative.audio_engine.execution.NativeAudioEngine`)
lives outside the studio and is injected by a composition root, so this module
imports nothing but stdlib and pydantic: no cycles, no I/O, no engine.

Failure is a first-class, typed outcome here rather than an exception in the hot
path, because "the model is not installed" is a *contract answer* — the caller
must be able to report ``UNAVAILABLE / REQUIRES_LOCAL_MODEL`` honestly instead of
either crashing or fabricating a ``READY``.

Every ``request_id`` is a stable id derived from ``(operation, asset, params)``
so a re-dispatch of the same command reuses the published artifact instead of
producing a second one (and so the bus's idempotency story is visible on disk).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field


class AudioEnginePortError(RuntimeError):
    """Base class for engine failures surfaced through the port."""


class EngineUnavailable(AudioEnginePortError):
    """No binary/engine is installed: an honest ``UNAVAILABLE``, not a failure."""


class EngineInputError(AudioEnginePortError):
    """The referenced media is missing, unreadable or not audio."""


class EngineRejected(AudioEnginePortError):
    """The engine ran but the result must not be published (verifier refusal)."""


EngineStatus = Literal["AVAILABLE", "UNAVAILABLE"]


class EngineCapability(BaseModel):
    """One capability's availability, with the reason and the remedy."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    status: EngineStatus
    code: str | None = None
    detail: str | None = None
    remedy: str | None = None

    @property
    def available(self) -> bool:
        return self.status == "AVAILABLE"


class BeatAnalysis(BaseModel):
    """Beat evidence read from a decoded signal (never a constant tempo)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["detected", "insufficient_onsets", "irregular_onsets", "empty_audio"]
    tempo_bpm: float | None = Field(default=None, gt=0.0)
    beat_period_us: int | None = Field(default=None, gt=0)
    confidence: float = Field(ge=0.0, le=1.0)
    first_beat_us: int = Field(default=0, ge=0)
    onset_count: int = Field(default=0, ge=0)
    onset_times_us: tuple[int, ...] = ()
    min_bpm: float = 40.0
    max_bpm: float = 240.0
    analyzer: str = "nagar.audio.beat.v2"


class ArtifactEvidence(BaseModel):
    """The measured facts about one published artifact (passport projection)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    asset_id: str
    output_path: str
    sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    duration_us: int | None = Field(default=None, ge=0)
    sample_rate: int | None = Field(default=None, gt=0)
    channels: int | None = Field(default=None, gt=0)
    codec: str | None = None
    integrated_lufs: float | None = None
    true_peak_dbtp: float | None = None
    loudness_range_lu: float | None = None
    loudness_checked: bool = False
    loudness_passed: bool | None = None
    loudness_code: str | None = None
    spectral_centroid_hz: float | None = None
    rms_dbfs: float | None = None
    crest_factor_db: float | None = None
    dynamic_range_db: float | None = None
    acoustic_signature_sha256: str | None = None
    verified: bool = False
    passport_id: str | None = None
    passport_signature: str | None = None
    continuity_lock_hash: str | None = None
    canonical_profile_ok: bool = True
    elapsed_seconds: float = Field(default=0.0, ge=0.0)
    reused_existing: bool = False
    transaction_id: str | None = None
    parameters: dict[str, object] = Field(default_factory=dict)
    #: What the run actually consumed, measured.  ``None`` for a reused artifact
    #: (nothing ran) and for a state-only request.  ``rss`` inside the report is
    #: explicitly ``NOT_MEASURED`` on a host without ``/proc``.
    budget: dict[str, object] | None = None


class LoudnessRequest(BaseModel):
    """Everything ``audio.normalize_loudness`` needs to run for real."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str = Field(min_length=1)
    source_asset_id: str = Field(min_length=1)
    output_asset_id: str = Field(min_length=1)
    source_sha256: str | None = None
    profile_id: str
    target_lufs: float
    true_peak_dbtp: float
    target_lra_lu: float
    two_pass: bool = True
    transaction_id: str | None = None
    continuity_lock_hash: str | None = None


class DenoiseRequest(BaseModel):
    """Everything ``audio.remove_noise`` needs to run for real."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str = Field(min_length=1)
    source_asset_id: str = Field(min_length=1)
    output_asset_id: str = Field(min_length=1)
    source_sha256: str | None = None
    strength: float = Field(ge=0.0, le=1.0)
    preserve_speech: bool = True
    transaction_id: str | None = None
    continuity_lock_hash: str | None = None


class BeatRequest(BaseModel):
    """Everything ``audio.detect_beats`` needs to run for real."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str = Field(min_length=1)
    source_asset_id: str = Field(min_length=1)
    source_sha256: str | None = None
    min_bpm: float = Field(gt=0.0)
    max_bpm: float = Field(gt=0.0)
    sensitivity: float = Field(default=0.5, ge=0.0, le=1.0)


class SourcePrecheck(BaseModel):
    """Cheap, pre-execution facts about the source (size/hash/suffix/probe)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    asset_id: str
    path: str | None = None
    exists: bool = False
    sha256: str | None = None
    size_bytes: int | None = None
    sample_rate: int | None = None
    channels: int | None = None
    duration_us: int | None = None
    codec: str | None = None
    readable: bool = False
    detail: str | None = None


class SourceRequest(BaseModel):
    """The minimal ask: which asset, and optionally which bytes were promised."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    asset_id: str = Field(min_length=1)
    expected_sha256: str | None = None


@dataclass
class AudioEngineHealth:
    """A snapshot of what this deployment can actually do."""

    binary: str | None = None
    ffmpeg_version: str | None = None
    capabilities: dict[str, EngineCapability] = field(default_factory=dict)

    @property
    def ready(self) -> bool:
        return any(capability.available for capability in self.capabilities.values())

    def as_evidence(self) -> dict[str, object]:
        return {
            "binary_name": self.binary.rsplit("/", 1)[-1] if self.binary else None,
            "ffmpeg_version": self.ffmpeg_version,
            "capabilities": {
                name: {
                    "status": capability.status,
                    "code": capability.code,
                    "detail": capability.detail,
                    "remedy": capability.remedy,
                }
                for name, capability in sorted(self.capabilities.items())
            },
        }


@runtime_checkable
class Cancellable(Protocol):
    """The *shape* of a cancellation token, declared at the port boundary.

    The concrete token lives in the engine (``audio_engine.process.CancelToken``)
    and the studio layer must not import it — that would make the contract
    depend on one implementation.  A structural protocol keeps the seam one-way:
    anything with ``cancel()`` and a ``cancelled`` property satisfies it, and an
    engine that accepts its own richer token still conforms.
    """

    @property
    def cancelled(self) -> bool: ...

    def cancel(self, reason: str = ...) -> None: ...


@runtime_checkable
class AudioEnginePort(Protocol):
    """The execution seam: every method owns real bytes, none fabricates."""

    def health(self) -> AudioEngineHealth: ...

    def precheck(self, request: SourceRequest) -> SourcePrecheck: ...

    def detect_beats(
        self, request: BeatRequest, *, cancel: Cancellable | None = None
    ) -> BeatAnalysis: ...

    def normalize_loudness(
        self, request: LoudnessRequest, *, cancel: Cancellable | None = None
    ) -> ArtifactEvidence: ...

    def remove_noise(
        self, request: DenoiseRequest, *, cancel: Cancellable | None = None
    ) -> ArtifactEvidence: ...


__all__ = [
    "ArtifactEvidence",
    "AudioEngineHealth",
    "AudioEnginePort",
    "AudioEnginePortError",
    "BeatAnalysis",
    "BeatRequest",
    "Cancellable",
    "DenoiseRequest",
    "EngineCapability",
    "EngineInputError",
    "EngineRejected",
    "EngineStatus",
    "EngineUnavailable",
    "LoudnessRequest",
    "SourcePrecheck",
    "SourceRequest",
]
