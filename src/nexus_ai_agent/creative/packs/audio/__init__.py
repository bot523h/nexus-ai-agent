"""``nexus.audio.studio`` — Local studio audio processing, loudness normalization,
and beat sync (Wave 5 substrate).

This package provides the pure substrate for Nagar audio studio editing:
* typed data models (:class:`BeatMarker`, :class:`BeatGridRef`, :class:`DetectBeatsInput`,
  :class:`NormalizeLoudnessInput`, :class:`DuckMusicInput`, :class:`BeatSyncCutInput`,
  :class:`RemoveNoiseInput`, :class:`DeessInput`, :class:`EqVoiceInput`,
  :class:`TimeStretchInput`);
* pure capability operations: ``audio.detect_beats`` (Level A),
  ``audio.duck_music`` (Level B), ``audio.beat_sync_cut`` (Level B),
  ``audio.remove_noise`` (Level B), ``audio.deess`` (Level B),
  ``audio.eq_voice`` (Level B), ``audio.time_stretch`` (Level B),
  and ``audio.normalize_loudness`` (Level C).

In accordance with Nagar substrate architecture, this package has zero heavy dependencies
(no torch, librosa, or soundfile imports).
"""

from __future__ import annotations

from nexus_ai_agent.creative.packs.audio.models import (
    AUDIO_PACKAGE_ID,
    DOMAIN,
    OPERATION_BEAT_SYNC_CUT,
    OPERATION_DETECT_BEATS,
    OPERATION_DUCK_MUSIC,
    OPERATION_NORMALIZE_LOUDNESS,
    BeatGridRef,
    BeatMarker,
    BeatSyncCutInput,
    DetectBeatsInput,
    DuckMusicInput,
    NormalizeLoudnessInput,
)
from nexus_ai_agent.creative.packs.audio.operations import (
    build_audio_registry,
    register_audio_operations,
)

__all__ = [
    "AUDIO_PACKAGE_ID",
    "DOMAIN",
    "OPERATION_BEAT_SYNC_CUT",
    "OPERATION_DETECT_BEATS",
    "OPERATION_DUCK_MUSIC",
    "OPERATION_NORMALIZE_LOUDNESS",
    "BeatGridRef",
    "BeatMarker",
    "BeatSyncCutInput",
    "DetectBeatsInput",
    "DuckMusicInput",
    "NormalizeLoudnessInput",
    "build_audio_registry",
    "register_audio_operations",
]
