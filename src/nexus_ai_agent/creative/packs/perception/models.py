"""Typed contracts for the ``nexus.analysis.perception`` pack.

The studio has editing operations and (since Wave 5) audio/caption/motion
packs, but until now nothing in the repository *measured* media: for example
``audio.detect_beats`` derives a fixed 120 BPM grid from a duration.  This pack
adds the missing lane: **perception evidence** — silences, scene cuts and
loudness measured from the real bytes by a free, offline engine (FFmpeg), then
pinned into typed commands.

The split mirrors the slideshow pack (``AssetEvidence``) and keeps the bus
honest:

* this package is *pure* (stdlib + pydantic, no I/O): it defines the evidence
  shapes and validates them;
* the engine that produces evidence lives in
  :mod:`nexus_ai_agent.creative.perception` (the adapter, which owns every
  subprocess and every file read);
* the operations (:mod:`.operations`) only *consume* evidence, and refuse any
  evidence that is not provably about the clip/asset it is applied to.

Integrity model (what the checks do and do not prove):

* ``asset_sha256`` binds evidence to *content*: evidence measured on other
  bytes (a re-render, another file, a stale analysis) is refused;
* ``evidence_hash`` is a SHA-256 over the canonical JSON of every other field:
  it detects accidental corruption and truncation in transport.  It is **not**
  a signature — a client that can submit a command can also compute it.  That is
  acceptable by construction: the operations that consume evidence only add
  reversible markers (Level B) or return advice (Level A); a forged evidence can
  place markers in the caller's own project, which ``timeline.mark`` already
  allows.  No privilege is gained.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PERCEPTION_PACKAGE_ID = "nexus.analysis.perception"
DOMAIN = "analysis"

OPERATION_MARK_SILENCES = "analysis.mark_silences"
OPERATION_MARK_SCENE_CUTS = "analysis.mark_scene_cuts"
OPERATION_ASSESS_LOUDNESS = "analysis.assess_loudness"

EVIDENCE_SCHEMA = "nexus.analysis-evidence.v1"

#: The only analyzer identity this runtime trusts to have produced evidence.
#: A new engine is a new literal here *and* a reviewed adapter, never a free
#: string a caller can choose.
ANALYZER_FFMPEG = "nagar.local.ffmpeg.analysis.v1"

#: Hard ceilings (fail-closed, never silently truncated).
MAX_SPANS = 5000
MAX_MEDIA_US = 7 * 24 * 3600 * 1_000_000  # one week
MAX_MARKERS_PER_COMMAND = 2000
MAX_LABEL_CHARS = 60

_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_IDENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:\-]{0,127}$")
_PRINTABLE = re.compile(r"^[^\x00-\x1f\x7f]*$")


def _checked_sha(value: str) -> str:
    if not _SHA256.fullmatch(value):
        raise ValueError("expected 'sha256:' followed by 64 lowercase hex digits")
    return value


def canonical_evidence_hash(payload: dict[str, Any]) -> str:
    """``sha256:<hex>`` over the canonical JSON of ``payload`` (``evidence_hash`` excluded)."""
    body = {key: value for key, value in payload.items() if key != "evidence_hash"}
    encoded = json.dumps(
        body, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )
    return "sha256:" + hashlib.sha256(encoded.encode("ascii")).hexdigest()


class _Evidence(BaseModel):
    """Fields shared by every evidence kind; subclasses add the measurement."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    evidence_schema: Literal["nexus.analysis-evidence.v1"] = "nexus.analysis-evidence.v1"
    analyzer: Literal["nagar.local.ffmpeg.analysis.v1"] = "nagar.local.ffmpeg.analysis.v1"
    engine: str = Field(min_length=1, max_length=120)
    asset_sha256: str
    duration_us: int = Field(ge=0, le=MAX_MEDIA_US)
    evidence_hash: str

    @field_validator("engine")
    @classmethod
    def _engine_is_printable(cls, value: str) -> str:
        if not _PRINTABLE.fullmatch(value) or value != value.strip():
            raise ValueError("engine must be a trimmed printable string")
        return value

    @field_validator("asset_sha256", "evidence_hash")
    @classmethod
    def _hashes_are_well_formed(cls, value: str) -> str:
        return _checked_sha(value)

    @model_validator(mode="after")
    def _hash_matches_content(self) -> _Evidence:
        expected = canonical_evidence_hash(self.model_dump(mode="json"))
        if self.evidence_hash != expected:
            raise ValueError("evidence_hash does not match the evidence content")
        return self

    @classmethod
    def seal(cls, **fields: Any) -> Any:
        """Build evidence and compute its ``evidence_hash`` (used by the adapter).

        ``fields`` are the measurement fields; the hash is derived from the exact
        validated representation, so ``seal`` can never produce evidence that the
        validator would then reject.
        """
        draft = cls.model_construct(**{**fields, "evidence_hash": "sha256:" + "0" * 64})
        payload = draft.model_dump(mode="json")
        return cls.model_validate({**payload, "evidence_hash": canonical_evidence_hash(payload)})


class SilenceSpan(BaseModel):
    """A silent interval in *asset* time, half-open ``[start_us, end_us)``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    start_us: int = Field(ge=0, le=MAX_MEDIA_US)
    end_us: int = Field(ge=0, le=MAX_MEDIA_US)

    @model_validator(mode="after")
    def _ordered(self) -> SilenceSpan:
        if self.start_us >= self.end_us:
            raise ValueError("silence span requires start_us < end_us")
        return self


class SilenceEvidence(_Evidence):
    """Silences measured by the engine (``silencedetect``) on the asset's audio."""

    kind: Literal["silence"] = "silence"
    noise_floor_db: float = Field(gt=-100.0, le=-10.0, allow_inf_nan=False)
    min_silence_us: int = Field(ge=10_000, le=600_000_000)
    spans: tuple[SilenceSpan, ...] = Field(default=(), max_length=MAX_SPANS)

    @model_validator(mode="after")
    def _spans_ordered_and_inside(self) -> SilenceEvidence:
        previous_end = 0
        for span in self.spans:
            if span.start_us < previous_end:
                raise ValueError("silence spans must be ascending and non-overlapping")
            if span.end_us > self.duration_us:
                raise ValueError("silence span ends after the media duration")
            previous_end = span.end_us
        return self


class SceneCutEvidence(_Evidence):
    """Scene-change instants (``select='gt(scene,T)'``) in *asset* time."""

    kind: Literal["scenes"] = "scenes"
    threshold: float = Field(gt=0.0, lt=1.0, allow_inf_nan=False)
    cuts_us: tuple[int, ...] = Field(default=(), max_length=MAX_SPANS)

    @model_validator(mode="after")
    def _cuts_ascending_and_inside(self) -> SceneCutEvidence:
        previous = -1
        for cut in self.cuts_us:
            if cut <= previous:
                raise ValueError("scene cuts must be strictly ascending")
            if cut < 0 or cut > self.duration_us:
                raise ValueError("scene cut lies outside the media duration")
            previous = cut
        return self


class LoudnessEvidence(_Evidence):
    """EBU R128 summary (``ebur128``).  ``None`` means *not measurable* (no signal)."""

    kind: Literal["loudness"] = "loudness"
    integrated_lufs: float | None = Field(default=None, ge=-70.0, le=10.0, allow_inf_nan=False)
    loudness_range_lu: float | None = Field(default=None, ge=0.0, le=100.0, allow_inf_nan=False)
    true_peak_dbfs: float | None = Field(default=None, ge=-150.0, le=20.0, allow_inf_nan=False)


def _checked_ident(value: str) -> str:
    if not _IDENT.fullmatch(value):
        raise ValueError("identifier must be 1-128 chars of [A-Za-z0-9_.:-], starting alphanumeric")
    return value


class _MarkInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    track_id: str
    clip_id: str
    label_prefix: str = Field(min_length=1, max_length=MAX_LABEL_CHARS)

    @field_validator("track_id", "clip_id")
    @classmethod
    def _identifiers(cls, value: str) -> str:
        return _checked_ident(value)

    @field_validator("label_prefix")
    @classmethod
    def _label(cls, value: str) -> str:
        if not _PRINTABLE.fullmatch(value) or value != value.strip():
            raise ValueError("label_prefix must be a trimmed printable string")
        return value


class MarkSilencesInput(_MarkInput):
    label_prefix: str = Field(default="silence", min_length=1, max_length=MAX_LABEL_CHARS)
    evidence: SilenceEvidence
    edge: Literal["start", "end", "both"] = "start"


class MarkSceneCutsInput(_MarkInput):
    label_prefix: str = Field(default="scene", min_length=1, max_length=MAX_LABEL_CHARS)
    evidence: SceneCutEvidence


class AssessLoudnessInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_id: str
    evidence: LoudnessEvidence
    target_lufs: float = Field(default=-14.0, ge=-40.0, le=-5.0, allow_inf_nan=False)
    true_peak_ceiling_dbfs: float = Field(default=-1.0, ge=-9.0, le=0.0, allow_inf_nan=False)
    tolerance_lu: float = Field(default=1.0, ge=0.1, le=6.0, allow_inf_nan=False)

    @field_validator("asset_id")
    @classmethod
    def _identifier(cls, value: str) -> str:
        return _checked_ident(value)


__all__ = [
    "ANALYZER_FFMPEG",
    "DOMAIN",
    "EVIDENCE_SCHEMA",
    "MAX_LABEL_CHARS",
    "MAX_MARKERS_PER_COMMAND",
    "MAX_MEDIA_US",
    "MAX_SPANS",
    "OPERATION_ASSESS_LOUDNESS",
    "OPERATION_MARK_SCENE_CUTS",
    "OPERATION_MARK_SILENCES",
    "PERCEPTION_PACKAGE_ID",
    "AssessLoudnessInput",
    "LoudnessEvidence",
    "MarkSceneCutsInput",
    "MarkSilencesInput",
    "SceneCutEvidence",
    "SilenceEvidence",
    "SilenceSpan",
    "canonical_evidence_hash",
]
