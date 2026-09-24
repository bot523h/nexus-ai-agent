"""Shared pure primitives for the vision packs (``nexus.vision.portrait`` × ``nexus.vision.scene``).

Why this module exists (task-153 acceptance: shared-abstraction vs duplication,
decided with evidence — full comparison in the mission report):

* the TDD catalogue (``docs/NAGAR_70_OPERATIONS_TDD.md`` §1.1) declares
  ``SubjectRef`` / ``MaskRef`` / ``TransformCurve`` as **cross-cutting** types,
  not per-pack types — both vision families speak mask/track/curve;
* the substrate convention (``motion.apply_mask``) already treats a mask as an
  ``AssetRecord`` with ``media_kind="image"``; both packs derive such assets
  with the same digest-seed → ``sha256`` recipe used by all five existing packs;
* everything here is stdlib + pydantic + studio (the pack boundary allow-list,
  enforced by ``tests/architecture/test_*_pack_boundary.py``) — no framework,
  no orchestration, no I/O.

What is deliberately NOT abstracted: the operation handlers themselves.  Each
handler keeps its own typed input model and its own provenance record, exactly
like the five shipped packs — a generic "effect-op factory" was one of the five
compared strategies and was rejected (overbuilding; no evidence).

Time semantics: ``timecode_us`` (integer microseconds) is authoritative and
``frame_number`` is always derived via ``studio.models.frame_number_for`` —
never recomputed by hand.

Honesty label: the deterministic stand-in "detector" below is a **contract
placeholder**, not a neural model.  Every analysis output carries
``detector=nagar.vision.deterministic_contract_v1`` and
``model_digest=sha256:contract-no-model`` so a caller can never mistake its
evidence for real inference (TDD §2.1: approximate results stay explicit).
"""

from __future__ import annotations

import hashlib
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from nexus_ai_agent.creative.studio.models import (
    AssetRecord,
    TimeBase,
    frame_number_for,
)

#: Label of the deterministic contract stand-in detector (see module docstring).
DETECTOR_ID = "nagar.vision.deterministic_contract_v1"

#: Explicit "there is no neural model behind this analysis" digest.
MODEL_DIGEST = "sha256:contract-no-model"

#: Microseconds per second (integer time arithmetic — no float drift).
MICROSECONDS_PER_SECOND = 1_000_000

#: Default command-envelope timebase (30 fps) for derived frame numbers.
DEFAULT_TIMEBASE = TimeBase(numerator=30, denominator=1)

#: ``sample_policy`` → sampling interval in integer microseconds.
SAMPLE_INTERVALS_US: dict[str, int] = {
    "sparse": 2_000_000,
    "standard": 500_000,
    "dense": 250_000,
}

SamplePolicy = Literal["sparse", "standard", "dense"]

#: Fixed normalized mask-asset resolution used by both packs (pure contract).
MASK_RESOLUTION = (1024, 1024)


def digest_seed_hash(*parts: object) -> str:
    """Deterministic ``sha256:`` content hash over the canonical seed parts.

    Mirrors the digest-seed convention of the shipped packs (``timeline.trim``,
    ``color.apply_lut``, ``motion.add_glow``, …): identical inputs always derive
    identical content hashes, and any parameter change moves the hash.
    """
    seed = ":".join(str(part) for part in parts)
    return "sha256:" + hashlib.sha256(seed.encode("utf-8")).hexdigest()


def derive_asset(
    *,
    asset_id: str,
    media_kind: Literal["video", "audio", "image", "caption"],
    seed_parts: tuple[object, ...],
    duration_us: int,
    parent_asset_ids: tuple[str, ...],
    provenance: dict[str, Any],
) -> AssetRecord:
    """Build a content-addressed derived asset record (the packs' shared recipe)."""
    return AssetRecord(
        asset_id=asset_id,
        media_kind=media_kind,
        content_sha256=digest_seed_hash(*seed_parts),
        duration_us=duration_us,
        parent_asset_ids=parent_asset_ids,
        provenance=provenance,
    )


class LandmarkPoint(BaseModel):
    """One normalized landmark point of a face sample (0..1 image space)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    x: float = Field(ge=0.0, le=1.0)
    y: float = Field(ge=0.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)


class FaceSample(BaseModel):
    """Landmarks of one sampled frame inside a face track."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    timecode_us: int = Field(ge=0)
    frame_number: int = Field(ge=0)
    points: tuple[LandmarkPoint, ...] = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)


class FaceTrackSet(BaseModel):
    """TDD ``FaceTrackSet``: per-frame landmark evidence for one subject.

    Chaining convention (mirrors ``audio.align_music``'s "the beat grid travels
    as plain numbers"): later operations take the ``track_id`` back as an
    evidence reference plus their own plain parameters — the substrate stays
    pure and stateless.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    track_id: str = Field(min_length=1)
    subject_id: str = Field(min_length=1)
    source_asset_id: str = Field(min_length=1)
    detector: str = DETECTOR_ID
    model_digest: str = MODEL_DIGEST
    landmark_count: int = Field(ge=1)
    samples: tuple[FaceSample, ...] = Field(min_length=0)
    confidence: float = Field(ge=0.0, le=1.0)


class ObjectTrackSample(BaseModel):
    """One sampled bounding box of an object track (normalized image space)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    timecode_us: int = Field(ge=0)
    frame_number: int = Field(ge=0)
    x: float = Field(ge=0.0, le=1.0)
    y: float = Field(ge=0.0, le=1.0)
    width: float = Field(gt=0.0, le=1.0)
    height: float = Field(gt=0.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)


class ObjectTrack(BaseModel):
    """TDD ``ObjectTrack``: sampled boxes + confidence for one semantic seed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    track_id: str = Field(min_length=1)
    source_asset_id: str = Field(min_length=1)
    semantic_seed: str = Field(min_length=1)
    detector: str = DETECTOR_ID
    model_digest: str = MODEL_DIGEST
    samples: tuple[ObjectTrackSample, ...] = Field(min_length=0)
    confidence: float = Field(ge=0.0, le=1.0)


class MaskEvidence(BaseModel):
    """TDD ``MaskRef`` evidence describing a derived mask asset."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    mask_id: str = Field(min_length=1)
    source_asset_id: str = Field(min_length=1)
    coordinate_space: Literal["normalized"] = "normalized"
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    coverage_ratio: float = Field(ge=0.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    detector: str = DETECTOR_ID
    model_digest: str = MODEL_DIGEST


class TransformCurveSample(BaseModel):
    """One 2-D similarity-transform keyframe (integer-microsecond timeline)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    timecode_us: int = Field(ge=0)
    frame_number: int = Field(ge=0)
    dx: float
    dy: float
    scale: float = Field(gt=0.0)
    rotation_deg: float


class TransformCurve(BaseModel):
    """TDD ``TransformCurve``: a deterministic stabilization/reframe plan."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    curve_id: str = Field(min_length=1)
    source_asset_id: str = Field(min_length=1)
    policy: str = Field(min_length=1)
    samples: tuple[TransformCurveSample, ...] = Field(min_length=0)
    confidence: float = Field(ge=0.0, le=1.0)


class ShotBoundary(BaseModel):
    """One detected shot boundary (cut or gradual transition start)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    timecode_us: int = Field(ge=0)
    frame_number: int = Field(ge=0)
    cut_score: float = Field(ge=0.0, le=1.0)
    kind: Literal["cut", "gradual"]


class ShotBoundarySet(BaseModel):
    """TDD ``ShotBoundarySet`` over one clip."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_asset_id: str = Field(min_length=1)
    threshold: float = Field(ge=0.0, le=1.0)
    detector: str = DETECTOR_ID
    model_digest: str = MODEL_DIGEST
    boundaries: tuple[ShotBoundary, ...] = Field(min_length=0)


class CandidateMoment(BaseModel):
    """TDD candidate moment: pinned timecode + evidence range + confidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_id: str = Field(min_length=1)
    timecode_us: int = Field(ge=0)
    frame_number: int = Field(ge=0)
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_start_us: int = Field(ge=0)
    evidence_end_us: int = Field(ge=0)
    subject_id: str = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_range(self) -> CandidateMoment:
        if self.evidence_start_us >= self.evidence_end_us:
            raise ValueError("evidence range requires start < end (half-open µs range)")
        return self


class CandidateMomentSet(BaseModel):
    """TDD ``CandidateMomentSet`` for ``scene.find_subject_moment``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_asset_id: str = Field(min_length=1)
    event: str = Field(min_length=1)
    minimum_confidence: float = Field(ge=0.0, le=1.0)
    detector: str = DETECTOR_ID
    model_digest: str = MODEL_DIGEST
    candidates: tuple[CandidateMoment, ...] = Field(min_length=0)


def sample_offsets_us(duration_us: int, policy: SamplePolicy) -> list[int]:
    """Deterministic sampling offsets for a clip duration and policy.

    Half-open convention: offsets are ``0, step, 2*step, …`` strictly below
    ``duration_us`` (a zero-duration clip samples nothing).
    """
    step = SAMPLE_INTERVALS_US[policy]
    duration = max(0, duration_us)
    return list(range(0, duration, step)) if duration > 0 else []


def frame_at(timecode_us: int, timebase: TimeBase = DEFAULT_TIMEBASE) -> int:
    """Derived frame number for a microsecond timecode (integer math)."""
    return frame_number_for(timecode_us, timebase)


def _unit_values(*parts: object, count: int) -> list[float]:
    """Deterministic floats in [0, 1) derived from the seed parts."""
    needed = count * 2
    data = bytearray()
    counter = 0
    while len(data) < needed:
        data += hashlib.sha512(f"{counter}:".encode() + ":".join(map(str, parts)).encode()).digest()
        counter += 1
    return [((data[i] << 8) + data[i + 1]) / 65536.0 for i in range(0, needed, 2)]


def face_track_from_asset(
    *,
    source_asset_id: str,
    content_sha256: str,
    duration_us: int,
    policy: SamplePolicy,
    landmark_count: int = 68,
    subject_id: str = "subject_01",
    max_faces: int = 1,
) -> FaceTrackSet:
    """Derive the deterministic contract ``FaceTrackSet`` for one clip.

    The landmark geometry is seeded by the asset's content hash — the same
    inputs always produce the same track (``deterministic=True`` contract), and
    the output declares ``detector=DETECTOR_ID`` so nobody reads it as model
    inference.
    """
    offsets = sample_offsets_us(duration_us, policy)
    samples: list[FaceSample] = []
    for index, offset in enumerate(offsets):
        rng = (*content_sha256, source_asset_id, "face", index, landmark_count)
        values = _unit_values(*rng, count=landmark_count * 3)
        points = tuple(
            LandmarkPoint(
                x=round(0.2 + 0.6 * values[3 * i], 6),
                y=round(0.2 + 0.6 * values[3 * i + 1], 6),
                confidence=round(0.75 + 0.25 * values[3 * i + 2], 6),
            )
            for i in range(landmark_count)
        )
        samples.append(
            FaceSample(
                timecode_us=offset,
                frame_number=frame_at(offset),
                points=points,
                confidence=round(0.8 + 0.2 * (1.0 - index / max(1, len(offsets))), 6),
            )
        )
    track_id = f"face-track-{digest_seed_hash(content_sha256, source_asset_id, policy)[7:19]}"
    mean_confidence = (
        round(sum(s.confidence for s in samples) / len(samples), 6) if samples else 0.0
    )
    return FaceTrackSet(
        track_id=track_id,
        subject_id=subject_id,
        source_asset_id=source_asset_id,
        landmark_count=landmark_count,
        samples=tuple(samples),
        confidence=mean_confidence if max_faces >= 1 else 0.0,
    )


def object_track_from_asset(
    *,
    source_asset_id: str,
    content_sha256: str,
    duration_us: int,
    policy: SamplePolicy,
    semantic_seed: str,
) -> ObjectTrack:
    """Derive the deterministic contract ``ObjectTrack`` for one clip."""
    offsets = sample_offsets_us(duration_us, policy)
    samples: list[ObjectTrackSample] = []
    for index, offset in enumerate(offsets):
        values = _unit_values(
            content_sha256, source_asset_id, "object", semantic_seed, index, count=6
        )
        samples.append(
            ObjectTrackSample(
                timecode_us=offset,
                frame_number=frame_at(offset),
                x=round(0.1 + 0.5 * values[0], 6),
                y=round(0.1 + 0.5 * values[1], 6),
                width=round(0.05 + 0.2 * values[2], 6),
                height=round(0.05 + 0.2 * values[3], 6),
                confidence=round(0.7 + 0.3 * values[4], 6),
            )
        )
    track_id = f"object-track-{digest_seed_hash(content_sha256, semantic_seed)[7:19]}"
    mean_confidence = (
        round(sum(s.confidence for s in samples) / len(samples), 6) if samples else 0.0
    )
    return ObjectTrack(
        track_id=track_id,
        source_asset_id=source_asset_id,
        semantic_seed=semantic_seed,
        samples=tuple(samples),
        confidence=mean_confidence,
    )


def transform_curve_from_offsets(
    *,
    curve_id: str,
    source_asset_id: str,
    policy: str,
    duration_us: int,
    offsets_xy_scale_rot: list[tuple[int, float, float, float, float]],
    confidence: float,
) -> TransformCurve:
    """Assemble a :class:`TransformCurve` from raw keyframes (shared by packs)."""
    samples = tuple(
        TransformCurveSample(
            timecode_us=offset,
            frame_number=frame_at(offset),
            dx=round(dx, 6),
            dy=round(dy, 6),
            scale=round(scale, 6),
            rotation_deg=round(rotation, 6),
        )
        for offset, dx, dy, scale, rotation in offsets_xy_scale_rot
        if 0 <= offset <= max(0, duration_us)
    )
    return TransformCurve(
        curve_id=curve_id,
        source_asset_id=source_asset_id,
        policy=policy,
        samples=samples,
        confidence=confidence,
    )


__all__ = [
    "CandidateMoment",
    "CandidateMomentSet",
    "DETECTOR_ID",
    "FaceSample",
    "FaceTrackSet",
    "LandmarkPoint",
    "MASK_RESOLUTION",
    "MaskEvidence",
    "MODEL_DIGEST",
    "ObjectTrack",
    "ObjectTrackSample",
    "SAMPLE_INTERVALS_US",
    "SamplePolicy",
    "ShotBoundary",
    "ShotBoundarySet",
    "TransformCurve",
    "TransformCurveSample",
    "derive_asset",
    "digest_seed_hash",
    "face_track_from_asset",
    "frame_at",
    "object_track_from_asset",
    "sample_offsets_us",
    "transform_curve_from_offsets",
]
