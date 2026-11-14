"""Audio execution port — compatibility re-export of the studio contract.

OMEGA-02 first declared this port under ``application/ports``, which the audio
pack cannot import: ``creative/packs/audio/**`` may only depend on stdlib,
pydantic and ``creative.studio`` (``tests/architecture/test_audio_pack_boundary.py``).
Declaring the contract next to the other studio contracts lets the pack type its
seam without crossing a layer boundary; the *implementation* stays in
``creative/audio_engine`` and is injected by the composition root.

New code should import ``nexus_ai_agent.creative.studio.audio_engine``.  This
module exists so application-layer callers that already use the old path keep
working; it defines nothing of its own.
"""

from __future__ import annotations

from nexus_ai_agent.creative.studio.audio_engine import (
    ArtifactEvidence,
    AudioEngineHealth,
    AudioEnginePort,
    AudioEnginePortError,
    BeatAnalysis,
    BeatRequest,
    DenoiseRequest,
    EngineCapability,
    EngineInputError,
    EngineRejected,
    EngineStatus,
    EngineUnavailable,
    LoudnessRequest,
    SourcePrecheck,
    SourceRequest,
)

__all__ = [
    "ArtifactEvidence",
    "AudioEngineHealth",
    "AudioEnginePort",
    "AudioEnginePortError",
    "BeatAnalysis",
    "BeatRequest",
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
