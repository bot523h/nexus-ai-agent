"""Continuity verification: per-dimension measurement, calibration, verdicts.

The invariant this module defends
---------------------------------
A hash proves a file did not change. It proves nothing about whether the next
render is the same character, and a single similarity score with a guessed
threshold proves even less. So the verifier here:

1. measures **independent dimensions** (face identity, body, hair, wardrobe,
   voice, motion, expression proxy, scene compatibility, canon) and refuses to
   let one stand in for another;
2. takes its decision boundaries from a **calibration artifact** produced from
   labelled data, with a three-band decision (match / review / drift) instead of
   one magic number;
3. separates **INSUFFICIENT_EVIDENCE** (nothing measurable, e.g. a profile frame
   where the face cannot be localised) from **drift**;
4. keeps **human review** in the loop for the ambiguous band — the system never
   upgrades a review-band result to VERIFIED on its own;
5. reports **NOT_MEASURED** for dimensions with no source (no voice sample
   supplied, no licensed embedder installed) instead of silently scoring zero.

The verdict vocabulary is closed; ``VerificationState`` is the only set of values
a report may carry.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import numpy as np

from .audio_motion import (
    gait_features,
    motion_distance,
    voice_distance,
    voice_features,
)
from .canon import CanonEngine
from .canonical import SCOPE_CALIBRATION, canonical_json, digest_of, scoped_digest
from .errors import ThresholdsMissing, ValidationError
from .model import CharacterCapsule
from .scene import ShotReference, Timeline, validate_capsule_pins
from .vision import Observation, distance, observe, score_from_distance
from .wardrobe import WardrobeModule, check_wardrobe, validate_outfit_era

CALIBRATION_SCHEMA = "character-capsule-calibration/1"

#: Dimensions the verifier knows about. Order is the report order.
DIMENSIONS: tuple[str, ...] = (
    "face",
    "body",
    "hair",
    "wardrobe",
    "voice",
    "motion",
    "expression",
    "scene",
    "canon",
)

#: Dimensions that must be measurable for a shot to be fully verifiable.
#: Dimensions that must be measurable for a frame to be fully verifiable, per framing.
REQUIRED_BY_FRAMING: Mapping[str, tuple[str, ...]] = {
    "portrait": ("face", "hair", "wardrobe"),
    "full_body": ("body",),
}

#: Minimum measured quality before a dimension may be *scored* at all.
#: ISO/IEC 29794-5 (and the OFIQ reference implementation) recommend pruning
#: low-quality samples rather than scoring them; scoring them is how a degraded
#: frame turns into a false identity-drift alarm. A gated dimension is reported
#: as NOT_MEASURED and the frame can only reach ``INSUFFICIENT_EVIDENCE``.
QUALITY_GATES: Mapping[str, float] = {
    "face": 0.35,
    "hair": 0.30,
    "wardrobe": 0.25,
    "body": 0.50,
}

#: Dimensions reported for the reviewer but never decision-bearing: the corpus has
#: no hair-change sample, so no hair threshold can be calibrated honestly and the
#: measurement is advisory only.
ADVISORY_DIMENSIONS: frozenset[str] = frozenset({"hair"})

#: Dimensions that stay optional (reported, never required to reach VERIFIED).
OPTIONAL_DIMENSIONS: tuple[str, ...] = ("voice", "motion", "expression", "scene")

REQUIRED_DIMENSIONS: tuple[str, ...] = ("face", "wardrobe")


class VerificationState(str, Enum):
    """The closed verdict vocabulary."""

    VERIFIED = "VERIFIED"
    VERIFIED_WITH_LIMITATIONS = "VERIFIED_WITH_LIMITATIONS"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    IDENTITY_DRIFT = "IDENTITY_DRIFT"
    WARDROBE_DRIFT = "WARDROBE_DRIFT"
    BODY_DRIFT = "BODY_DRIFT"
    CANON_CONFLICT = "CANON_CONFLICT"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


#: Aggregate precedence: the first state present decides the shot verdict.
STATE_PRECEDENCE: tuple[VerificationState, ...] = (
    VerificationState.CANON_CONFLICT,
    VerificationState.IDENTITY_DRIFT,
    VerificationState.WARDROBE_DRIFT,
    VerificationState.BODY_DRIFT,
    VerificationState.REVIEW_REQUIRED,
    VerificationState.INSUFFICIENT_EVIDENCE,
    VerificationState.VERIFIED_WITH_LIMITATIONS,
    VerificationState.VERIFIED,
)


#: Declared measurement tolerance. A distance below this is treated as noise, so
#: a band can never be narrower than this even when repeats measure exactly zero.
#: When the declared p95/p05 bands cross, the equal-error point may be used as the
#: boundary — but only when it is genuinely informative. Above this value the
#: dimension is reported NOT_MEASURED instead of being given a fabricated band.
MAX_FALLBACK_EER = 0.25

MIN_ACCEPT = 0.10

#: Declared minimum separation between the accept and reject boundaries.
MIN_BAND = 0.10


class Band(str, Enum):
    MATCH = "MATCH"
    REVIEW = "REVIEW"
    DRIFT = "DRIFT"
    NOT_MEASURED = "NOT_MEASURED"


@dataclass(frozen=True)
class DimensionThreshold:
    """Three-band boundary for one dimension, in distance space."""

    accept: float
    reject: float

    def __post_init__(self) -> None:
        if self.accept < 0 or self.reject < 0:
            raise ValidationError("thresholds must be non-negative")
        if self.accept > self.reject:
            raise ValidationError(
                "a threshold's accept boundary must not exceed its reject boundary",
                accept=self.accept,
                reject=self.reject,
            )

    def band(self, value: float) -> Band:
        if value <= self.accept:
            return Band.MATCH
        if value >= self.reject:
            return Band.DRIFT
        return Band.REVIEW

    def to_dict(self) -> dict[str, float]:
        return {"accept": self.accept, "reject": self.reject}


#: Declared scope of ``CalibrationArtifact.digest``.
CALIBRATION_DIGEST_SCOPE = {
    "algorithm": "sha256",
    "excluded": {
        "created_at": "wall clock; the same corpus must yield the same calibration digest",
    },
}


@dataclass(frozen=True)
class CalibrationArtifact:
    """Measured, reproducible decision boundaries — not hand-tuned guesses."""

    thresholds: Mapping[str, DimensionThreshold]
    stats: Mapping[str, Mapping[str, list[float]]]
    metrics: Mapping[str, Mapping[str, Any]]
    dataset_digest: str
    seed: str
    created_at: str
    quality_floor: float = 0.02
    schema: str = CALIBRATION_SCHEMA
    #: Which characters were fitted and which were held out, recorded inside the
    #: artefact so the "no leakage" claim can be checked instead of trusted (F23).
    split: Mapping[str, Any] = field(default_factory=dict)
    #: Probes dropped before fitting, and why. Hiding a pruned probe would make the
    #: fitted split unreproducible from the corpus.
    pruning: Mapping[str, Any] = field(default_factory=dict)

    def threshold(self, dimension: str) -> DimensionThreshold:
        try:
            return self.thresholds[dimension]
        except KeyError:
            raise ThresholdsMissing(
                f"no calibrated threshold for dimension {dimension!r}", dimension=dimension
            ) from None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "seed": self.seed,
            "created_at": self.created_at,
            "digest_scope": dict(CALIBRATION_DIGEST_SCOPE),
            "dataset_digest": self.dataset_digest,
            "quality_floor": self.quality_floor,
            "split": dict(self.split),
            "pruning": dict(self.pruning),
            "thresholds": {key: value.to_dict() for key, value in sorted(self.thresholds.items())},
            "stats": {key: dict(value) for key, value in sorted(self.stats.items())},
            "metrics": {key: dict(value) for key, value in sorted(self.metrics.items())},
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> CalibrationArtifact:
        if payload.get("schema") != CALIBRATION_SCHEMA:
            raise ValidationError("unsupported calibration schema", schema=payload.get("schema"))
        try:
            return cls(
                thresholds={
                    key: DimensionThreshold(**value) for key, value in payload["thresholds"].items()
                },
                stats=payload["stats"],
                metrics=payload["metrics"],
                dataset_digest=payload["dataset_digest"],
                seed=payload["seed"],
                created_at=payload["created_at"],
                quality_floor=float(payload.get("quality_floor", 0.02)),
                split=dict(payload.get("split", {})),
                pruning=dict(payload.get("pruning", {})),
            )
        except KeyError as exc:
            raise ValidationError("calibration artifact is incomplete", missing=str(exc)) from exc

    @property
    def digest(self) -> str:
        """Content digest of the calibration.

        ``created_at`` is excluded: two runs over the same corpus must produce the
        same calibration digest, otherwise every report that binds it (and every
        evidence bundle that binds those reports) changes with the wall clock, and
        reproducibility claims cannot be re-checked. The exclusion is published in
        ``digest_scope`` so it is visible rather than implied.
        """
        return scoped_digest(SCOPE_CALIBRATION, self.digest_payload())

    def digest_payload(self) -> dict[str, Any]:
        return {
            key: value
            for key, value in self.to_dict().items()
            if key not in CALIBRATION_DIGEST_SCOPE["excluded"]
        }


@dataclass(frozen=True)
class DimensionResult:
    """One dimension's measured outcome for one shot."""

    dimension: str
    band: Band
    distance: float | None
    score: float | None
    state: VerificationState
    detail: str
    measured: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "dimension": self.dimension,
            "band": self.band.value,
            "distance": None if self.distance is None else round(float(self.distance), 6),
            "score": None if self.score is None else round(float(self.score), 6),
            "state": self.state.value,
            "detail": self.detail,
            "measured": self.measured,
        }


@dataclass(frozen=True)
class ContinuityReport:
    """The verifier's independent output for one shot."""

    shot_id: str
    character_id: str
    capsule_revision: int
    capsule_digest: str
    verdict: VerificationState
    dimensions: tuple[DimensionResult, ...]
    conflicts: tuple[dict[str, Any], ...]
    calibration_digest: str
    limitations: tuple[str, ...] = ()
    processing_ms: float = 0.0
    evidence_digest: str = ""
    #: What the shot pinned, and what was actually verified. ``validate_capsule_pins``
    #: guarantees the two agree; keeping both makes the agreement checkable later.
    pins: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "character-capsule-continuity-report/1",
            "shot_id": self.shot_id,
            "character_id": self.character_id,
            "capsule_revision": self.capsule_revision,
            "capsule_digest": self.capsule_digest,
            "verdict": self.verdict.value,
            "dimensions": [result.to_dict() for result in self.dimensions],
            "conflicts": list(self.conflicts),
            "calibration_digest": self.calibration_digest,
            "limitations": list(self.limitations),
            "processing_ms": round(float(self.processing_ms), 3),
            "evidence_digest": self.evidence_digest,
            "pins": dict(self.pins),
        }

    def dimension(self, name: str) -> DimensionResult:
        for result in self.dimensions:
            if result.dimension == name:
                return result
        raise ValidationError(f"report has no dimension {name!r}")

    def states(self) -> tuple[VerificationState, ...]:
        return tuple(result.state for result in self.dimensions)


def aggregate_verdict(states: Iterable[VerificationState]) -> VerificationState:
    """Apply the documented precedence to a set of dimension states."""
    present = set(states)
    for state in STATE_PRECEDENCE:
        if state in present:
            return state
    return VerificationState.INSUFFICIENT_EVIDENCE


#: Fields excluded from the evidence digest. Timing is a property of the machine,
#: not of the finding: including it made every published report fail its own digest
#: as soon as the runner measured it (finding F25).
EVIDENCE_DIGEST_EXCLUSIONS = ("processing_ms", "evidence_digest")


def report_digest(report: Mapping[str, Any], *, calibration_digest: str) -> str:
    """Recomputable digest of a published report (timing excluded by construction)."""
    body = {key: value for key, value in report.items() if key not in EVIDENCE_DIGEST_EXCLUSIONS}
    return digest_of({"report": body, "thresholds": calibration_digest})


def verify_report_evidence(report: Mapping[str, Any]) -> bool:
    """True when ``report["evidence_digest"]`` matches the report's own content."""
    recorded = report.get("evidence_digest") or ""
    if not recorded:
        return False
    expected = report_digest(report, calibration_digest=str(report.get("calibration_digest", "")))
    return hmac_compare(recorded, expected)


def hmac_compare(left: str, right: str) -> bool:
    import hmac as _hmac

    return _hmac.compare_digest(str(left), str(right))


def expression_curvature(observation: Observation) -> float | None:
    """Mouth-curvature proxy measured from the face crop's dark structure.

    Positive curvature describes a smile, negative a frown, ~0 a neutral mouth.
    This is a *proxy*: it is reported as a review-band signal, never as proof.
    """
    if observation.face_quality <= 0.0:
        return None
    geometry = observation.face["geometry"]
    # geometry[5] is the vertical centre of the mouth band inside the normalised
    # crop; the mouth's own curvature is encoded by the crop's lower-band mass.
    mouth_centre = float(geometry[5])
    if not np.isfinite(mouth_centre):
        return None
    return float((0.5 - mouth_centre) * 2.0)


def _declared_appearance_markers(capsule: CharacterCapsule, scene_order: int | None) -> set[str]:
    """Mutable canon facts that legitimately change the face from this scene on.

    When the story order is unknown (no timeline was supplied) every mutable marker
    is treated as potentially active. That keeps the face dimension from claiming
    ``IDENTITY_DRIFT`` on a change the story declared, degrading it to
    ``REVIEW_REQUIRED`` — the honest answer when the ordering cannot be checked.
    """
    canon = capsule.modules.get("canon")
    facts = getattr(canon, "facts", None)
    if not isinstance(facts, Mapping):
        return set()
    markers: set[str] = set()
    for fact in facts.values():
        if getattr(fact, "kind", None) is not None and fact.kind.value != "MUTABLE":
            continue
        added = getattr(fact, "added_at_scene", None)
        if scene_order is None:
            markers.add(f"{fact.predicate}={fact.value}")
            continue
        if added is None or added > scene_order:
            continue
        markers.add(f"{fact.predicate}={fact.value}")
    return markers


EXPRESSION_SIGN: dict[str, int] = {
    "neutral": 0,
    "smile": 1,
    "sad": -1,
    "angry": 0,
    "surprised": 0,
}


class ContinuityEvaluator:
    """Compares a shot's frame (and optional voice/motion) with a capsule."""

    def __init__(self, calibration: CalibrationArtifact) -> None:
        self.calibration = calibration
        self._scene_reference: tuple[float, ...] | None = None

    # -- distances ---------------------------------------------------------
    def _dimension(self, name: str, value: float) -> DimensionResult:
        try:
            threshold = self.calibration.threshold(name)
        except ThresholdsMissing:
            # An inseparable (or missing) calibration is evidence about the
            # measurement, not a licence to guess a boundary.
            return self._not_measured(
                name,
                f"no calibrated threshold for {name}: the calibration distributions overlap "
                "or the dimension has no labelled pairs, so no boundary is defensible",
            )
        band = threshold.band(value)
        if band is Band.MATCH:
            state = {
                "face": VerificationState.VERIFIED,
                "body": VerificationState.VERIFIED,
                "hair": VerificationState.VERIFIED,
                "wardrobe": VerificationState.VERIFIED,
                "voice": VerificationState.VERIFIED,
                "motion": VerificationState.VERIFIED,
            }.get(name, VerificationState.VERIFIED)
        elif band is Band.DRIFT:
            state = {
                "face": VerificationState.IDENTITY_DRIFT,
                "body": VerificationState.BODY_DRIFT,
                "wardrobe": VerificationState.WARDROBE_DRIFT,
                "hair": VerificationState.IDENTITY_DRIFT,
                "voice": VerificationState.IDENTITY_DRIFT,
                "motion": VerificationState.BODY_DRIFT,
            }.get(name, VerificationState.REVIEW_REQUIRED)
        else:
            state = VerificationState.REVIEW_REQUIRED
        return DimensionResult(
            dimension=name,
            band=band,
            distance=value,
            score=score_from_distance(value),
            state=state,
            detail=(
                f"{name} distance {value:.4f} vs accept<={threshold.accept:.4f} / "
                f"reject>={threshold.reject:.4f}"
            ),
        )

    def _not_measured(self, name: str, reason: str) -> DimensionResult:
        return DimensionResult(
            dimension=name,
            band=Band.NOT_MEASURED,
            distance=None,
            score=None,
            state=VerificationState.VERIFIED_WITH_LIMITATIONS,
            detail=f"{name}: NOT_MEASURED ({reason})",
            measured=False,
        )

    @staticmethod
    def _quality_ok(observation: Observation, dimension: str) -> bool:
        """Measurable *and* above the quality gate for this dimension."""
        if not observation.measurable(dimension):
            return False
        gate = QUALITY_GATES.get(dimension, 0.0)
        if dimension == "face":
            return observation.face_quality >= gate
        if dimension == "body":
            return observation.body_quality >= gate
        # hair/wardrobe have no independent quality score; face resolution is
        # used as the proxy because both are measured from the same crop.
        return observation.face_quality >= gate

    # -- public API --------------------------------------------------------
    def evaluate(
        self,
        *,
        shot: ShotReference,
        capsule: CharacterCapsule,
        image: np.ndarray,
        reference_images: Sequence[np.ndarray],
        timeline: Timeline | None = None,
        voice_samples: np.ndarray | None = None,
        reference_voice: np.ndarray | None = None,
        motion_sequence: np.ndarray | None = None,
        reference_motion: np.ndarray | None = None,
        processing_ms: float = 0.0,
    ) -> ContinuityReport:
        """Produce one independent continuity report for ``shot``.

        The shot's pins are checked against the capsule before anything is
        measured: verifying frame X against a capsule the shot was never authored
        against produces a confident answer to the wrong question.

        Without a ``timeline`` the scene order is unknown, so the story-dependent
        dimensions (wardrobe plan, canon) are reported ``NOT_MEASURED`` instead of
        being silently evaluated at scene 1.
        """
        if not reference_images:
            raise ValidationError("continuity evaluation needs at least one reference image")
        validate_capsule_pins(shot, capsule)
        candidate = observe(image)
        references = [observe(reference) for reference in reference_images]
        results: list[DimensionResult] = []
        limitations: list[str] = []

        # -- face ----------------------------------------------------------
        if not self._quality_ok(candidate, "face"):
            results.append(
                DimensionResult(
                    dimension="face",
                    band=Band.NOT_MEASURED,
                    distance=None,
                    score=None,
                    state=VerificationState.INSUFFICIENT_EVIDENCE,
                    detail=(
                        f"face below the quality gate "
                        f"(quality={candidate.face_quality:.3f}, "
                        f"gate={QUALITY_GATES['face']:.2f})"
                        + (f": {candidate.face_reason}" if candidate.face_reason else "")
                    ),
                    measured=False,
                )
            )
            limitations.append("face identity could not be measured on this frame")
        else:
            face_distances = [
                distance(
                    reference,
                    candidate,
                    "face",
                    stats=self.calibration.stats.get("face"),
                )
                for reference in references
                if self._quality_ok(reference, "face")
            ]
            if not face_distances:
                results.append(
                    DimensionResult(
                        dimension="face",
                        band=Band.NOT_MEASURED,
                        distance=None,
                        score=None,
                        state=VerificationState.INSUFFICIENT_EVIDENCE,
                        detail="no reference frame had a measurable face",
                        measured=False,
                    )
                )
                limitations.append("reference gallery had no measurable face")
            else:
                results.append(self._dimension("face", min(face_distances)))

        # -- body / hair / wardrobe ---------------------------------------
        for name in ("body", "hair", "wardrobe"):
            if not self._quality_ok(candidate, name):
                results.append(
                    DimensionResult(
                        dimension=name,
                        band=Band.NOT_MEASURED,
                        distance=None,
                        score=None,
                        state=VerificationState.VERIFIED_WITH_LIMITATIONS,
                        detail=(
                            f"{name} below the quality gate on this frame "
                            f"(framing={candidate.framing}, quality={candidate.body_quality:.3f}, "
                            f"gate={QUALITY_GATES.get(name, 0.0):.2f})"
                        ),
                        measured=False,
                    )
                )
                limitations.append(f"{name} cannot be measured at framing {candidate.framing!r}")
                continue
            measurable_references = [
                reference for reference in references if self._quality_ok(reference, name)
            ]
            if not measurable_references:
                results.append(
                    self._not_measured(name, "no reference frame had a measurable sample")
                )
                continue
            distances = [
                distance(reference, candidate, name, stats=self.calibration.stats.get(name))
                for reference in measurable_references
            ]
            value = min(distances)
            if name in ADVISORY_DIMENSIONS or name not in self.calibration.thresholds:
                from .vision import score_from_distance as _score

                results.append(
                    DimensionResult(
                        dimension=name,
                        band=Band.NOT_MEASURED,
                        distance=value,
                        score=_score(value),
                        state=VerificationState.VERIFIED_WITH_LIMITATIONS,
                        detail=(
                            f"{name} distance {value:.4f}; no calibrated threshold "
                            "(advisory dimension, reported for review only)"
                        ),
                        measured=True,
                    )
                )
                limitations.append(f"{name} is advisory: no calibrated threshold exists")
                continue
            results.append(self._dimension(name, value))

        # -- expression proxy --------------------------------------------
        curvature = expression_curvature(candidate)
        expected = EXPRESSION_SIGN.get(shot.declared.expression, 0)
        if curvature is None:
            results.append(self._not_measured("expression", "face not measurable"))
        elif expected == 0:
            # A curvature proxy cannot separate "neutral" from "angry" from
            # "surprised"; claiming it could would manufacture evidence. The
            # dimension is reported as unmeasured instead of forcing a review.
            results.append(
                DimensionResult(
                    dimension="expression",
                    band=Band.NOT_MEASURED,
                    distance=abs(curvature),
                    score=score_from_distance(abs(curvature)),
                    state=VerificationState.VERIFIED_WITH_LIMITATIONS,
                    detail=(
                        f"expression proxy {curvature:+.3f}; declared "
                        f"{shot.declared.expression!r} has no sign expectation, "
                        "so no expression claim is made"
                    ),
                    measured=False,
                )
            )
            limitations.append("expression is a curvature proxy, not an expression classifier")
        else:
            observed_sign = int(np.sign(curvature)) if abs(curvature) > 0.02 else 0
            if observed_sign == expected:
                results.append(
                    DimensionResult(
                        dimension="expression",
                        band=Band.MATCH,
                        distance=0.0,
                        score=1.0,
                        state=VerificationState.VERIFIED,
                        detail=f"expression proxy {curvature:+.3f} agrees with declared sign",
                    )
                )
            else:
                results.append(
                    DimensionResult(
                        dimension="expression",
                        band=Band.REVIEW,
                        distance=abs(curvature),
                        score=score_from_distance(abs(curvature)),
                        state=VerificationState.REVIEW_REQUIRED,
                        detail=(
                            f"expression proxy {curvature:+.3f} disagrees with declared "
                            f"{shot.declared.expression!r}"
                        ),
                    )
                )

        # -- voice ---------------------------------------------------------
        if voice_samples is None or reference_voice is None:
            results.append(self._not_measured("voice", "no voice sample supplied"))
            limitations.append("voice identity was not measured (no audio evidence in this shot)")
        else:
            voice_stats = self.calibration.stats.get("voice")
            reference_features = voice_features(reference_voice)
            candidate_features = voice_features(voice_samples)
            value = voice_distance(
                reference_features,
                candidate_features,
                stats=voice_stats,
            )
            results.append(self._dimension("voice", value))

        # -- motion --------------------------------------------------------
        if motion_sequence is None or reference_motion is None:
            results.append(self._not_measured("motion", "no motion sequence supplied"))
            limitations.append("motion identity was not measured (single frame, no sequence)")
        else:
            reference_features = gait_features(reference_motion)
            candidate_features = gait_features(motion_sequence)
            value = motion_distance(
                reference_features,
                candidate_features,
                stats=self.calibration.stats.get("motion"),
            )
            results.append(self._dimension("motion", value))

        # -- scene compatibility ------------------------------------------
        results.append(self._scene_result(candidate))

        # -- wardrobe plan + canon ----------------------------------------
        conflicts: list[dict[str, Any]] = []
        wardrobe = capsule.modules.get("wardrobe")
        scene_order: int | None = None
        if timeline is not None:
            scene_order = timeline.scene(shot.scene_id).order
        if scene_order is None and (
            isinstance(wardrobe, WardrobeModule) or "canon" in capsule.modules
        ):
            results.append(
                self._not_measured(
                    "scene_context",
                    "no timeline supplied: the shot's story position is unknown, so the "
                    "wardrobe plan and canon cannot be evaluated at the right scene",
                )
            )
            limitations.append(
                "scene order unavailable: wardrobe-plan and canon dimensions were not evaluated"
            )
        if isinstance(wardrobe, WardrobeModule) and scene_order is not None:
            finding = check_wardrobe(
                wardrobe,
                sequence_id=shot.sequence_id,
                shot_index=shot.shot_index,
                actual_outfit_id=shot.declared.outfit_id,
                scene_id=shot.scene_id,
            )
            if finding is not None:
                conflicts.append(finding.to_dict())
            if scene_order is not None:
                try:
                    validate_outfit_era(
                        wardrobe, outfit_id=shot.declared.outfit_id, scene_order=scene_order
                    )
                except ValidationError as exc:
                    conflicts.append({"code": "OUTFIT_ERA_CONFLICT", "detail": str(exc)})
        canon_conflicts: tuple[Any, ...] = ()
        if "canon" in capsule.modules and scene_order is not None:
            engine = CanonEngine(capsule)
            canon_conflicts = engine.check_shot(
                shot_id=shot.shot_id,
                scene_order=scene_order,
                assertions=shot.declared.canon_assertions,
                outfit_id=shot.declared.outfit_id,
            )
            conflicts.extend(conflict.to_dict() for conflict in canon_conflicts)
        # Wardrobe-era rules belong to the wardrobe dimension: an outfit worn
        # before its era is a wardrobe continuity failure, not a story-canon
        # contradiction. Keeping them apart is what lets the report name the
        # right failure instead of collapsing both into CANON_CONFLICT.
        canon_codes = {conflict.get("code") for conflict in conflicts}
        wardrobe_codes = canon_codes & {"OUTFIT_ERA_CONFLICT", "UNKNOWN_OUTFIT", "WARDROBE_DRIFT"}
        canon_violations = canon_codes & {
            "IMMUTABLE_FACT_CONTRADICTION",
            "STORY_EVENT_NOT_HONOURED",
            "POST_DEATH_PRESENCE",
        }
        canon_unasserted = canon_codes & {"STORY_EFFECT_NOT_ASSERTED"}
        if canon_violations:
            results.append(
                DimensionResult(
                    dimension="canon",
                    band=Band.DRIFT,
                    distance=None,
                    score=0.0,
                    state=VerificationState.CANON_CONFLICT,
                    detail="; ".join(
                        sorted(
                            {
                                conflict.get("detail", "")
                                for conflict in conflicts
                                if conflict.get("code") in canon_violations
                            }
                        )
                    )
                    or "canon conflict",
                )
            )
        elif canon_unasserted:
            results.append(
                DimensionResult(
                    dimension="canon",
                    band=Band.REVIEW,
                    distance=None,
                    score=None,
                    state=VerificationState.REVIEW_REQUIRED,
                    detail="; ".join(
                        sorted(
                            {
                                conflict.get("detail", "")
                                for conflict in conflicts
                                if conflict.get("code") in canon_unasserted
                            }
                        )
                    ),
                )
            )
            limitations.append("a persistent canon effect is not asserted by this shot")
        else:
            results.append(
                DimensionResult(
                    dimension="canon",
                    band=Band.MATCH,
                    distance=0.0,
                    score=1.0,
                    state=VerificationState.VERIFIED,
                    detail="no canon contradiction found",
                )
            )
        # A canon-declared appearance change (e.g. the scar the story gives the
        # character) is expected to move facial similarity. It may never be
        # silently verified, but it is not evidence of a different person either.
        declared_markers = _declared_appearance_markers(capsule, scene_order)
        if declared_markers:
            adjusted: list[DimensionResult] = []
            for result in results:
                if (
                    result.dimension == "face"
                    and result.state is VerificationState.IDENTITY_DRIFT
                    and declared_markers
                ):
                    adjusted.append(
                        DimensionResult(
                            dimension=result.dimension,
                            band=Band.REVIEW,
                            distance=result.distance,
                            score=result.score,
                            state=VerificationState.REVIEW_REQUIRED,
                            detail=(
                                f"{result.detail}; canon declares "
                                + ", ".join(sorted(declared_markers))
                                + " from this scene, so facial change is expected"
                            ),
                            measured=result.measured,
                        )
                    )
                    limitations.append(
                        "canon-declared appearance marker present: face similarity "
                        "cannot separate a scar from a different person"
                    )
                else:
                    adjusted.append(result)
            results = adjusted

        # A wardrobe-plan or outfit-era violation surfaces as its own state.
        state_override: VerificationState | None = None
        if wardrobe_codes:
            state_override = VerificationState.WARDROBE_DRIFT
        required = REQUIRED_BY_FRAMING.get(candidate.framing, REQUIRED_BY_FRAMING["portrait"])
        missing_required = [
            result.dimension
            for result in results
            if result.dimension in required and not result.measured
        ]
        if missing_required and not any(
            result.state
            in {
                VerificationState.CANON_CONFLICT,
                VerificationState.IDENTITY_DRIFT,
                VerificationState.WARDROBE_DRIFT,
                VerificationState.BODY_DRIFT,
            }
            for result in results
        ):
            verdict = VerificationState.INSUFFICIENT_EVIDENCE
        else:
            verdict = aggregate_verdict(result.state for result in results)
        if state_override is not None and verdict in {
            VerificationState.VERIFIED,
            VerificationState.VERIFIED_WITH_LIMITATIONS,
            VerificationState.REVIEW_REQUIRED,
        }:
            verdict = state_override
        if verdict is VerificationState.VERIFIED_WITH_LIMITATIONS:
            unmeasured = [
                result.dimension
                for result in results
                if not result.measured and result.dimension not in required
            ]
            if unmeasured:
                limitations.append("unmeasured dimensions: " + ", ".join(sorted(unmeasured)))
        if verdict is VerificationState.VERIFIED:
            # A fully verified shot still carries the honest scope statement.
            limitations = limitations or ()
        report = ContinuityReport(
            shot_id=shot.shot_id,
            character_id=capsule.character_id,
            capsule_revision=capsule.revision_no,
            capsule_digest=capsule.digest,
            verdict=verdict,
            dimensions=tuple(results),
            conflicts=tuple(conflicts),
            calibration_digest=self.calibration.digest,
            limitations=tuple(limitations),
            processing_ms=processing_ms,
            pins={
                "shot_capsule_revision": shot.capsule_revision,
                "verified_capsule_revision": capsule.revision_no,
                "shot_capsule_digest": shot.capsule_digest,
                "verified_capsule_digest": capsule.digest,
                "module_pins_verified": sorted(shot.module_digests),
                "scene_order": scene_order,
            },
        )
        evidence = report_digest(report.to_dict(), calibration_digest=self.calibration.digest)
        return ContinuityReport(
            shot_id=report.shot_id,
            character_id=report.character_id,
            capsule_revision=report.capsule_revision,
            capsule_digest=report.capsule_digest,
            verdict=report.verdict,
            dimensions=report.dimensions,
            conflicts=report.conflicts,
            calibration_digest=report.calibration_digest,
            limitations=report.limitations,
            processing_ms=report.processing_ms,
            evidence_digest=evidence,
            pins=report.pins,
        )

    def _scene_result(self, candidate: Observation) -> DimensionResult:
        """Scene compatibility from measured background statistics."""
        if not self._scene_reference:
            return self._not_measured("scene", "no declared scene environment supplied")
        measured = candidate.scene["background"]
        reference = np.asarray(self._scene_reference, dtype=np.float64)
        delta = float(np.sqrt(float(np.mean((measured - reference) ** 2))))
        threshold = self.calibration.threshold("scene")
        band = threshold.band(delta)
        state = (
            VerificationState.VERIFIED if band is Band.MATCH else VerificationState.REVIEW_REQUIRED
        )
        return DimensionResult(
            dimension="scene",
            band=band,
            distance=delta,
            score=score_from_distance(delta),
            state=state,
            detail=(
                f"background colour distance {delta:.4f} vs accept<={threshold.accept:.4f} / "
                f"reject>={threshold.reject:.4f}"
            ),
        )

    def with_scene_reference(self, background_rgb: Sequence[float]) -> ContinuityEvaluator:
        clone = ContinuityEvaluator(self.calibration)
        clone._scene_reference = tuple(float(value) for value in background_rgb)
        return clone


def scene_distance(reference: Observation, candidate: Observation) -> float:
    """RMS difference of measured background colour (scene compatibility)."""
    a = np.asarray(reference.scene["background"], dtype=np.float64)
    b = np.asarray(candidate.scene["background"], dtype=np.float64)
    return float(np.sqrt(float(np.mean((a - b) ** 2))))


def evaluate_pairs(
    references: Sequence[Observation],
    candidates: Sequence[Observation],
    dimension: str,
    *,
    stats: Mapping[str, list[float]] | None = None,
) -> list[float]:
    """All cross distances between two groups (used by calibration)."""
    out: list[float] = []
    for reference in references:
        for candidate in candidates:
            out.append(distance(reference, candidate, dimension, stats=stats))
    return out


def operating_point(
    genuine: Sequence[float], impostor: Sequence[float], threshold: float
) -> dict[str, float]:
    """FAR/FRR/TAR at one threshold — the only honest way to state a number."""
    genuine_array = np.asarray(genuine, dtype=np.float64)
    impostor_array = np.asarray(impostor, dtype=np.float64)
    if genuine_array.size == 0 or impostor_array.size == 0:
        raise ValidationError("operating point needs both genuine and impostor scores")
    frr = float(np.mean(genuine_array > threshold))
    far = float(np.mean(impostor_array <= threshold))
    return {
        "threshold": float(threshold),
        "far": round(far, 6),
        "frr": round(frr, 6),
        "tar": round(1.0 - frr, 6),
        "genuine_n": int(genuine_array.size),
        "impostor_n": int(impostor_array.size),
    }


def equal_error_rate(genuine: Sequence[float], impostor: Sequence[float]) -> dict[str, float]:
    """Scan candidate thresholds for the point where FAR and FRR cross."""
    genuine_array = np.sort(np.asarray(genuine, dtype=np.float64))
    impostor_array = np.sort(np.asarray(impostor, dtype=np.float64))
    if genuine_array.size == 0 or impostor_array.size == 0:
        raise ValidationError("EER needs both genuine and impostor scores")
    candidates = np.unique(np.concatenate([genuine_array, impostor_array]))
    best = None
    for threshold in candidates:
        point = operating_point(genuine_array, impostor_array, float(threshold))
        gap = abs(point["far"] - point["frr"])
        if best is None or gap < best["gap"]:
            best = {
                "gap": gap,
                "eer": round((point["far"] + point["frr"]) / 2.0, 6),
                "threshold": float(threshold),
                "far": point["far"],
                "frr": point["frr"],
            }
    assert best is not None
    return best


def build_calibration(
    *,
    dimension_scores: Mapping[str, Mapping[str, Sequence[float]]],
    stats: Mapping[str, Mapping[str, list[float]]],
    dataset_digest: str,
    seed: str,
    created_at: str,
    metrics: Mapping[str, Mapping[str, Any]] | None = None,
    quality_floor: float = 0.02,
    genuine_quantile: float = 0.95,
    impostor_quantile: float = 0.05,
    holdout: Mapping[str, Mapping[str, Sequence[float]]] | None = None,
    split: Mapping[str, Any] | None = None,
    pruning: Mapping[str, Any] | None = None,
) -> CalibrationArtifact:
    """Derive three-band thresholds from labelled genuine/impostor distances.

    Three declared outcomes per dimension (ADR-0010):

    ``QUANTILE``
        The usual case: ``accept`` is the ``genuine_quantile`` of genuine
        distances and ``reject`` the ``impostor_quantile`` of impostor distances,
        so at most ``1 - genuine_quantile`` of genuine pairs reach the review band
        and at most ``impostor_quantile`` of impostors are accepted.
    ``EER_FALLBACK``
        The p95/p05 bands cross, but the distributions are still ordered on
        average with an equal-error rate of at most ``MAX_FALLBACK_EER``. The
        boundaries are placed symmetrically around that operating point and the
        decision is labelled, so a reader sees the weaker fit instead of a
        silently invented band.
    ``NOT_MEASURED``
        The measurement cannot separate the classes in the calibration split. No
        threshold is emitted for the dimension; the numbers that prove the
        inseparability are recorded and the verifier reports the dimension as
        ``NOT_MEASURED``. (The previous code placed both boundaries at the
        midpoint of an overlap, which produces a threshold that classifies coin
        flips and makes every downstream verdict arbitrary.)

    ``holdout`` scores (a disjoint character) yield the FAR/FRR that are reported;
    the calibration split's own scores are never used for reporting.
    """
    thresholds: dict[str, DimensionThreshold] = {}
    measured: dict[str, dict[str, Any]] = {}
    for dimension, scores in dimension_scores.items():
        genuine = np.asarray(scores.get("genuine", []), dtype=np.float64)
        impostor = np.asarray(scores.get("impostor", []), dtype=np.float64)
        if genuine.size == 0 or impostor.size == 0:
            measured[dimension] = {
                "status": "NOT_MEASURED",
                "reason": "NO_LABELLED_PAIRS",
                "genuine_n": int(genuine.size),
                "impostor_n": int(impostor.size),
            }
            continue
        accept = max(float(np.quantile(genuine, genuine_quantile)), MIN_ACCEPT)
        reject = float(np.quantile(impostor, impostor_quantile))
        eer = equal_error_rate(genuine, impostor)
        band_fit: dict[str, Any] = {
            "mode": "QUANTILE",
            "raw_accept": round(accept, 6),
            "raw_reject": round(reject, 6),
        }
        if accept > reject:
            if eer["eer"] > MAX_FALLBACK_EER:
                measured[dimension] = {
                    "status": "NOT_MEASURED",
                    "reason": "CALIBRATION_INSEPARABLE",
                    "mode": "NONE",
                    "genuine_n": int(genuine.size),
                    "impostor_n": int(impostor.size),
                    "eer": eer["eer"],
                    "raw_accept": round(accept, 6),
                    "raw_reject": round(reject, 6),
                    "detail": (
                        "the calibration split cannot separate genuine from impostor distances "
                        f"(equal-error rate {eer['eer']:.3f} > {MAX_FALLBACK_EER:.2f}), so no "
                        "threshold is defensible"
                    ),
                }
                continue
            boundary = float(eer["threshold"])
            accept = max(boundary - MIN_BAND / 2.0, MIN_ACCEPT)
            reject = max(boundary + MIN_BAND / 2.0, accept + MIN_BAND)
            band_fit = {
                "mode": "EER_FALLBACK",
                "raw_accept": band_fit["raw_accept"],
                "raw_reject": band_fit["raw_reject"],
                "eer_threshold": round(boundary, 6),
                "eer": eer["eer"],
                "detail": (
                    "the p95/p05 bands cross; boundaries sit at the equal-error point "
                    f"(EER {eer['eer']:.3f} <= {MAX_FALLBACK_EER:.2f})"
                ),
            }
        elif accept >= reject - MIN_BAND:
            midpoint = (accept + reject) / 2.0
            band_fit["min_band_applied"] = True
            accept = midpoint - MIN_BAND / 2.0
            reject = midpoint + MIN_BAND / 2.0
        thresholds[dimension] = DimensionThreshold(accept=accept, reject=max(accept, reject))
        entry: dict[str, Any] = {
            "calibration_split": {
                "genuine_n": int(genuine.size),
                "impostor_n": int(impostor.size),
                "genuine_mean": round(float(genuine.mean()), 6),
                "genuine_p95": round(float(np.quantile(genuine, 0.95)), 6),
                "impostor_mean": round(float(impostor.mean()), 6),
                "impostor_p05": round(float(np.quantile(impostor, 0.05)), 6),
                "eer": eer["eer"],
                "eer_threshold": round(float(eer["threshold"]), 6),
                "accept": round(accept, 6),
                "reject": round(reject, 6),
            },
            "band_fit": band_fit,
        }
        if holdout and dimension in holdout:
            held = holdout[dimension]
            held_genuine = held.get("genuine", [])
            held_impostor = held.get("impostor", [])
            if not held_genuine or not held_impostor:
                entry["holdout"] = {
                    "status": "NOT_MEASURED",
                    "reason": "held-out split has no pair of one class for this dimension",
                }
            else:
                entry["holdout"] = {
                    "accept_boundary": operating_point(
                        held_genuine, held_impostor, thresholds[dimension].accept
                    ),
                    "reject_boundary": operating_point(
                        held_genuine, held_impostor, thresholds[dimension].reject
                    ),
                    "eer": equal_error_rate(held_genuine, held_impostor),
                }
        entry.update((metrics or {}).get(dimension, {}))
        measured[dimension] = entry
    return CalibrationArtifact(
        thresholds=thresholds,
        stats=stats,
        metrics=measured,
        dataset_digest=dataset_digest,
        seed=seed,
        created_at=created_at,
        quality_floor=quality_floor,
        split=dict(split or {}),
        pruning=dict(pruning or {}),
    )


def calibration_summary(calibration: CalibrationArtifact) -> str:
    """One-line-per-dimension human summary of the artifact (used by the report)."""
    lines = [
        f"calibration {short_digest(calibration.digest)} dataset={calibration.dataset_digest}",
    ]
    for dimension, threshold in sorted(calibration.thresholds.items()):
        metrics = calibration.metrics.get(dimension, {})
        split = metrics.get("calibration_split", {})
        lines.append(
            f"  {dimension:<10} accept<={threshold.accept:.4f} reject>={threshold.reject:.4f} "
            f"eer={split.get('eer', 'n/a')}"
        )
    return "\n".join(lines)


def short_digest(value: str, length: int = 12) -> str:
    return value.split(":", 1)[-1][:length]


def calibration_fingerprint(calibration: CalibrationArtifact) -> str:
    return digest_of(calibration.to_dict())


def load_calibration(payload: Mapping[str, Any]) -> CalibrationArtifact:
    return CalibrationArtifact.from_dict(payload)


def calibration_json(calibration: CalibrationArtifact) -> str:
    return canonical_json(calibration.to_dict())
