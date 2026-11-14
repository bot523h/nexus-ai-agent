"""The studio's native audio execution plane (OMEGA-02).

This package is the *engine* side of the audio port: real FFmpeg processes, real
signal analysis, real verification.  It is deliberately outside
``creative/studio`` (which stays import-free of engines) and outside
``creative/packs`` (which stays pure); the composition root injects an engine
into the ``CommandBus``, so a handler can call it without a boundary crossing.

Layout — each module owns exactly one concern:

``models``
    The locked stream/continuity models (:class:`CanonicalStreamProfile`,
    :class:`LoudnessProfile`, :class:`ContinuityLockSpec`) and probe/measurement
    records.  Pure pydantic, no I/O.
``process``
    :class:`ProcessGovernor` — the only place a child process is created:
    deadline, cancellation, process-group kill, bounded stdout/stderr capture,
    stdin closed.  Nothing else in the package may call ``subprocess``.
``workspace``
    :class:`RuntimeWorkspace` — isolated staging, atomic publish, boundary
    checks, content hashing.  Nothing is published except by atomic rename, and
    a published path is never a run's own input.
``ffmpeg_ops``
    :class:`FfmpegTranscoder` — probe, decode, loudness measurement, real
    two-pass ``loudnorm``, ``afftdn`` denoise, signature extraction.  It turns
    FFmpeg's behaviour (including its *silent* tolerance of damaged input) into
    typed outcomes.
``numpy_dsp``
    Bounded-memory analysis: onset envelope, tempo candidates, spectral
    centroid, RMS/crest/dynamic range, band energies.
``verify``
    :class:`ArtifactPassport` + :func:`verify_passport` — the independent check
    of the bytes on disk *after* the final encode, plus the canonical-stream and
    loudness-target gates.
``ed25519_sign``
    The signer that pairs with the repository's audited verifier
    (``creative.packs.ed25519``); every signature is verified before returning.
``continuity``
    Zero-Drift comparison of two artifacts and the lossless-assembly proof.
``execution``
    :class:`NativeAudioEngine` — implements the port: stage, run, publish (only
    after the verifier agrees), record evidence, reuse a previously verified
    artifact for a replayed request.
"""

from __future__ import annotations

from nexus_ai_agent.creative.audio_engine.continuity import (
    AssemblyProof,
    ContinuityReport,
    assert_lossless_assembly,
    compare_continuity,
)
from nexus_ai_agent.creative.audio_engine.execution import (
    AudioExecutionSettings,
    AudioSourceCatalog,
    CallbackSourceCatalog,
    LocalDirectorySourceCatalog,
    NativeAudioEngine,
    build_native_audio_engine,
)
from nexus_ai_agent.creative.audio_engine.models import (
    CANONICAL_PROFILE_ID,
    CanonicalStreamProfile,
    ContinuityLockSpec,
    LoudnessProfile,
    LoudnessTargetCheck,
    MediaProbe,
    SpectralSignature,
)
from nexus_ai_agent.creative.audio_engine.verify import (
    ArtifactPassport,
    PassportKeyring,
    VerificationReport,
    verify_passport,
)

__all__ = [
    "AssemblyProof",
    "ArtifactPassport",
    "AudioExecutionSettings",
    "AudioSourceCatalog",
    "CANONICAL_PROFILE_ID",
    "CallbackSourceCatalog",
    "CanonicalStreamProfile",
    "ContinuityLockSpec",
    "ContinuityReport",
    "LocalDirectorySourceCatalog",
    "LoudnessProfile",
    "LoudnessTargetCheck",
    "MediaProbe",
    "NativeAudioEngine",
    "PassportKeyring",
    "SpectralSignature",
    "VerificationReport",
    "assert_lossless_assembly",
    "build_native_audio_engine",
    "compare_continuity",
    "verify_passport",
]
