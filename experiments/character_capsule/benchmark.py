"""Character continuity benchmark: ten scenarios, measured on real code.

What this file does *not* do
----------------------------
It never prints an estimated number. Every figure in ``benchmark_results.json`` is
computed from an actual run over the synthetic corpus in this environment; every
figure that cannot be measured here (human review, GPU time, learned-embedding
similarity) is recorded as ``NOT_MEASURED`` with the reason.

Pipeline
--------
1. generate the corpus (deterministic; corpus digest recorded);
2. fit per-dimension feature statistics and three-band thresholds on the
   calibration characters (protagonist + look-alike);
3. measure FAR/FRR/EER on the **held-out** character;
4. run the ten required scenarios plus two extra failure-mode scenarios
   (wardrobe drift, voice identity), each against the real evaluator;
5. write ``calibration.json``, ``continuity_reports.json``,
   ``benchmark_results.json`` and a generated ``BENCHMARK.md``.
"""

from __future__ import annotations

import json
import platform
import resource
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from .audio_motion import gait_features, motion_distance, voice_distance, voice_features
from .canonical import digest_of
from .continuity import (
    QUALITY_GATES,
    ContinuityEvaluator,
    VerificationState,
    build_calibration,
    equal_error_rate,
    operating_point,
    scene_distance,
    short_digest,
)
from .dataset import (
    CHARACTERS,
    OUT_PRIMARY,
    OUT_SECONDARY,
    Corpus,
    build_capsule,
    build_corpus,
    build_timeline,
    character_split,
    motion_sequence,
    voice_samples,
)
from .evidence import (
    bundle_from_files,
    load_signing_key,
    sign_bundle,
    verify_bundle,
)
from .model import CharacterCapsule
from .scene import OverrideDimension, SceneOverride, build_shot
from .vision import Observation, distance, group_stats, hex_to_rgb, observe
from .wardrobe import WardrobeModule

#: Human review is not available in this environment; it is reported as such.
HUMAN_REVIEW = "NOT_MEASURED"


@dataclass
class Scenario:
    """One benchmark scenario and the verdict it is expected to produce."""

    name: str
    description: str
    shot_id: str
    scene_id: str
    image_key: str
    expected: tuple[VerificationState, ...]
    reference_keys: tuple[str, ...]
    quoted_condition: str
    with_voice: bool = False
    with_motion: bool = False
    allow_limitations: bool = True


@dataclass
class ScenarioOutcome:
    scenario: Scenario
    report: Mapping[str, Any]
    passed: bool
    processing_ms: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario.name,
            "description": self.scenario.description,
            "condition": self.scenario.quoted_condition,
            "expected": [state.value for state in self.scenario.expected],
            "actual": self.report["verdict"],
            "passed": self.passed,
            "processing_ms": round(self.processing_ms, 3),
            "dimensions": {
                entry["dimension"]: {
                    "band": entry["band"],
                    "distance": entry["distance"],
                    "state": entry["state"],
                    "measured": entry["measured"],
                }
                for entry in self.report["dimensions"]
            },
            "conflicts": [
                {"code": conflict.get("code"), "detail": conflict.get("detail")}
                for conflict in self.report["conflicts"]
            ],
            "limitations": self.report["limitations"],
            "evidence_digest": self.report["evidence_digest"],
        }


@dataclass
class BenchmarkConfig:
    """Where the corpus and the artifacts live."""

    work_dir: Path
    out_dir: Path | None = None
    seed: str = "nagar-character-capsule-benchmark-v1"
    calibration_quantiles: tuple[float, float] = (0.95, 0.05)
    extra: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Statistics helpers
# ---------------------------------------------------------------------------


def _stats_for(observations: Sequence[Observation], dimension: str) -> dict[str, Any]:
    """Per-group calibration statistics (see ``vision.group_stats``)."""
    return group_stats(observations, dimension)


def _vector_stats(vectors: Sequence[np.ndarray]) -> dict[str, list[float]]:
    matrix = np.vstack([np.ravel(vector) for vector in vectors])
    return {
        "mean": [round(float(value), 8) for value in matrix.mean(axis=0)],
        "std": [round(float(value), 8) for value in matrix.std(axis=0)],
    }


def _pair_scores(
    corpus: Corpus,
    pairs: Mapping[str, list[tuple[str, str]]],
    *,
    observations: Mapping[str, Observation],
    stats: Mapping[str, Mapping[str, list[float]]],
) -> dict[str, dict[str, list[float]]]:
    scores: dict[str, dict[str, list[float]]] = {
        dimension: {"genuine": [], "impostor": []}
        for dimension in ("face", "body", "hair", "wardrobe", "scene")
    }
    for family in ("genuine", "impostor"):
        for left, right in pairs[family]:
            a = observations[left]
            b = observations[right]
            for dimension in ("face", "body", "hair", "wardrobe"):
                scores[dimension][family].append(
                    distance(a, b, dimension, stats=stats.get(dimension))
                )
            scores["scene"][family].append(scene_distance(a, b))
    return scores


def _voice_calibration() -> tuple[dict[str, list[float]], dict[str, dict[str, list[float]]]]:
    """Genuine/impostor voice distances from synthetic signature variants.

    Genuine pairs are *within* a character (three session variants: baseline,
    slightly faster, slightly breathier); impostor pairs are cross-character.
    Characters are split the same way as the visual calibration: the first two
    fit the threshold, the third is held out.
    """
    from .audio_motion import VoiceSignature, synthesize_voice, voice_vector

    variants: dict[str, list[np.ndarray]] = {}
    for spec in CHARACTERS:
        base = VoiceSignature.from_module(spec.voice)
        sessions = [
            base,
            VoiceSignature(
                f0_hz=base.f0_hz * 1.01,
                timbre=base.timbre,
                speaking_rate=min(base.speaking_rate * 1.05, 1.8),
            ),
            VoiceSignature(
                f0_hz=base.f0_hz * 0.99,
                timbre=base.timbre,
                speaking_rate=max(base.speaking_rate * 0.96, 0.5),
                breathiness=min(base.breathiness + 0.04, 0.3),
            ),
        ]
        variants[spec.character_id] = [voice_features(synthesize_voice(sig)) for sig in sessions]
    stats = _vector_stats(
        [voice_vector(features) for features in variants.values() for features in features[:1]]
    )
    splits = {
        "calibration": {"genuine": [], "impostor": []},
        "holdout": {"genuine": [], "impostor": []},
    }
    ids = sorted(variants)
    for index, character_id in enumerate(ids):
        split = "holdout" if index == 2 else "calibration"
        features = variants[character_id]
        for i in range(len(features)):
            for j in range(i + 1, len(features)):
                splits[split]["genuine"].append(
                    voice_distance(features[i], features[j], stats=stats)
                )
    for index, a in enumerate(ids):
        for b in ids[index + 1 :]:
            split = "holdout" if 2 in (index, ids.index(b)) else "calibration"
            splits[split]["impostor"].append(
                voice_distance(variants[a][0], variants[b][0], stats=stats)
            )
    return stats, splits


def _motion_calibration() -> tuple[dict[str, list[float]], dict[str, dict[str, list[float]]]]:
    """Genuine/impostor gait distances (three session seeds per character)."""
    from .audio_motion import motion_vector, synthesize_gait

    variants: dict[str, list[dict[str, np.ndarray]]] = {}
    for spec in CHARACTERS:
        variants[spec.character_id] = [
            gait_features(synthesize_gait(spec.motion.gait, seed=f"{spec.character_id}-{session}"))
            for session in ("a", "b", "c")
        ]
    stats = _vector_stats([motion_vector(features[0]) for features in variants.values()])
    splits = {
        "calibration": {"genuine": [], "impostor": []},
        "holdout": {"genuine": [], "impostor": []},
    }
    ids = sorted(variants)
    for index, character_id in enumerate(ids):
        split = "holdout" if index == 2 else "calibration"
        features = variants[character_id]
        for i in range(len(features)):
            for j in range(i + 1, len(features)):
                splits[split]["genuine"].append(
                    motion_distance(features[i], features[j], stats=stats)
                )
    for index, a in enumerate(ids):
        for b in ids[index + 1 :]:
            split = "holdout" if 2 in (index, ids.index(b)) else "calibration"
            splits[split]["impostor"].append(
                motion_distance(variants[a][0], variants[b][0], stats=stats)
            )
    return stats, splits


# ---------------------------------------------------------------------------
# Scenario construction
# ---------------------------------------------------------------------------


def _reference_keys(
    corpus: Corpus, character_id: str, *, kind: str = "portrait"
) -> tuple[str, ...]:
    """Canonical gallery for a character, restricted to one framing."""
    return tuple(record.key for record in corpus.references(character_id, kind=kind))


def _scenarios(corpus: Corpus) -> list[Scenario]:
    protagonist = CHARACTERS[0].character_id
    portraits = _reference_keys(corpus, protagonist)
    fullbody = _reference_keys(corpus, protagonist, kind="full_body")
    return [
        Scenario(
            name="same_character_same_camera",
            description="control: the reference frame itself",
            shot_id="SH-101",
            scene_id="SC-001",
            image_key="000001-ref-front",
            expected=(VerificationState.VERIFIED, VerificationState.VERIFIED_WITH_LIMITATIONS),
            reference_keys=portraits,
            quoted_condition="same camera, same lighting",
        ),
        Scenario(
            name="same_character_different_angle",
            description="three-quarter view of the same character",
            shot_id="SH-102",
            scene_id="SC-001",
            image_key="000001-shot-angle",
            expected=(
                VerificationState.VERIFIED,
                VerificationState.VERIFIED_WITH_LIMITATIONS,
                VerificationState.REVIEW_REQUIRED,
            ),
            reference_keys=portraits,
            quoted_condition="camera yaw 32 degrees",
        ),
        Scenario(
            name="same_character_different_lighting",
            description="warm 3400K key light on the same set",
            shot_id="SH-103",
            scene_id="SC-001",
            image_key="000001-shot-light",
            expected=(
                VerificationState.VERIFIED,
                VerificationState.VERIFIED_WITH_LIMITATIONS,
                VerificationState.INSUFFICIENT_EVIDENCE,
            ),
            reference_keys=portraits,
            quoted_condition="3400K key, intensity 1.18",
        ),
        Scenario(
            name="same_character_different_expression",
            description="smiling shot, neutral reference",
            shot_id="SH-104",
            scene_id="SC-001",
            image_key="000001-shot-expression",
            expected=(
                VerificationState.VERIFIED_WITH_LIMITATIONS,
                VerificationState.REVIEW_REQUIRED,
                VerificationState.VERIFIED,
            ),
            reference_keys=portraits,
            quoted_condition="expression smile",
        ),
        Scenario(
            name="same_character_different_outfit",
            description="planned second outfit, same identity",
            shot_id="SH-105",
            scene_id="SC-001",
            image_key="000001-shot-outfit2",
            expected=(
                VerificationState.VERIFIED_WITH_LIMITATIONS,
                VerificationState.REVIEW_REQUIRED,
            ),
            reference_keys=portraits,
            quoted_condition="outfit OUT-0002",
        ),
        Scenario(
            name="same_character_different_environment",
            description="cliff-road environment with the canonical injury present",
            shot_id="SH-106",
            scene_id="SC-003",
            image_key="000001-shot-environment",
            expected=(
                VerificationState.VERIFIED_WITH_LIMITATIONS,
                VerificationState.REVIEW_REQUIRED,
            ),
            reference_keys=portraits,
            quoted_condition="background #6b7280, 6200K",
        ),
        Scenario(
            name="same_character_different_pose",
            description="full-body frame with slouched posture",
            shot_id="SH-107",
            scene_id="SC-003",
            image_key="000001-shot-fullbody",
            expected=(
                VerificationState.VERIFIED_WITH_LIMITATIONS,
                VerificationState.INSUFFICIENT_EVIDENCE,
                VerificationState.REVIEW_REQUIRED,
            ),
            reference_keys=fullbody,
            quoted_condition="full-body framing",
        ),
        Scenario(
            name="same_character_motion_sequence",
            description="gait sequence identity across a walk cycle",
            shot_id="SH-108",
            scene_id="SC-003",
            image_key="000001-shot-fullbody",
            expected=(
                VerificationState.VERIFIED_WITH_LIMITATIONS,
                VerificationState.INSUFFICIENT_EVIDENCE,
                VerificationState.REVIEW_REQUIRED,
            ),
            reference_keys=fullbody,
            quoted_condition="24 fps gait sequence",
            with_motion=True,
        ),
        Scenario(
            name="different_character_similar_appearance",
            description="look-alike character submitted as the protagonist",
            shot_id="SH-109",
            scene_id="SC-001",
            image_key="000002-ref-front",
            expected=(VerificationState.IDENTITY_DRIFT, VerificationState.REVIEW_REQUIRED),
            reference_keys=portraits,
            quoted_condition="impostor pair",
        ),
        Scenario(
            name="character_canon_conflict",
            description="shot asserts no scar after the canonical injury event",
            shot_id="SH-110",
            scene_id="SC-003",
            image_key="000001-shot-environment",
            expected=(VerificationState.CANON_CONFLICT,),
            reference_keys=portraits,
            quoted_condition="canon assertion scar=none",
        ),
        Scenario(
            name="extra_wardrobe_drift",
            description="unplanned outfit change inside a sequence",
            shot_id="SH-111",
            scene_id="SC-002",
            image_key="000001-ref-outfit2",
            expected=(VerificationState.WARDROBE_DRIFT, VerificationState.REVIEW_REQUIRED),
            reference_keys=portraits,
            quoted_condition="OUT-0002 at shot index 0 (plan requires OUT-0001)",
        ),
        Scenario(
            name="extra_voice_identity",
            description="voice identity measured alongside the visual dimensions",
            shot_id="SH-112",
            scene_id="SC-001",
            image_key="000001-ref-front",
            expected=(VerificationState.VERIFIED_WITH_LIMITATIONS, VerificationState.VERIFIED),
            reference_keys=portraits,
            quoted_condition="reference voice vs same-identity session voice",
            with_voice=True,
        ),
    ]


def _shot_for(
    scenario: Scenario,
    *,
    capsule: CharacterCapsule,
    corpus: Corpus,
    image_asset_id: str,
) -> Any:
    timeline = build_timeline()
    scene = timeline.scene(scenario.scene_id)
    wardrobe = capsule.modules["wardrobe"]
    assert isinstance(wardrobe, WardrobeModule)
    from .wardrobe import expected_outfit

    index = 0
    if scenario.name == "extra_wardrobe_drift":
        outfit_id = OUT_SECONDARY.outfit_id
    elif scenario.scene_id == "SC-002":
        outfit_id = expected_outfit(wardrobe, sequence_id=scene.sequence_id, shot_index=index)
    elif scenario.scene_id == "SC-003":
        outfit_id = OUT_SECONDARY.outfit_id
    else:
        outfit_id = OUT_PRIMARY.outfit_id
    assertions: dict[str, str] = {}
    override = None
    if scenario.name == "same_character_different_environment":
        assertions = {"scar": "left_cheek"}
        override = SceneOverride(
            scene_id="SC-003",
            choices={OverrideDimension.EXPRESSION: "sad"},
            reason="scripted emotional beat",
        )
    if scenario.name == "character_canon_conflict":
        assertions = {"scar": "none"}
        override = SceneOverride(
            scene_id="SC-003",
            choices={OverrideDimension.EXPRESSION: "sad"},
            reason="scripted emotional beat",
        )
    if scenario.name == "different_character_similar_appearance":
        override = SceneOverride(
            scene_id="SC-001",
            choices={OverrideDimension.EXPRESSION: "neutral"},
            reason="impostor probe authored against the protagonist capsule",
        )
    return build_shot(
        shot_id=scenario.shot_id,
        scene=scene,
        capsule=capsule,
        shot_index=index,
        outfit_id=outfit_id,
        image_asset_id=image_asset_id,
        expression=(
            "smile" if scenario.name == "same_character_different_expression" else "neutral"
        ),
        canon_assertions=assertions,
        scene_override=override,
    )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def run_benchmark(config: BenchmarkConfig) -> dict[str, Any]:
    """Execute the benchmark and return (and write) the result document."""
    started = time.perf_counter()
    work = Path(config.work_dir)
    out = Path(config.out_dir) if config.out_dir else work / "results"
    out.mkdir(parents=True, exist_ok=True)
    corpus = build_corpus(work, write=True)
    capsule = build_capsule(CHARACTERS[0])
    timeline = build_timeline()

    # Observations for every frame, computed once.
    observations: dict[str, Observation] = {}
    for record in corpus.frames:
        observations[record.key] = observe(corpus.load(record))

    portrait_references = [
        observations[record.key]
        for record in corpus.frames
        if record.purpose in {"reference", "reference_alt"}
        and record.kind == "portrait"
        and observations[record.key].face_quality > 0.0
    ]
    fullbody_references = [
        observations[record.key]
        for record in corpus.frames
        if record.purpose in {"reference", "reference_alt"}
        and record.kind == "full_body"
        and observations[record.key].body_quality > 0.0
    ]
    stats: dict[str, Any] = {}
    for dimension in ("face", "hair", "wardrobe"):
        stats[dimension] = _stats_for(portrait_references, dimension)
    stats["body"] = _stats_for(fullbody_references, "body")
    # Scene statistics are measured in background-colour space, not feature space.
    reference_records = [record for record in corpus.frames if record.purpose == "reference"]
    backgrounds = np.vstack(
        [np.asarray(observations[record.key].scene["background"]) for record in reference_records]
    )
    stats["scene"] = {
        "mean": [round(float(value), 8) for value in backgrounds.mean(axis=0)],
        "std": [round(float(value), 8) for value in backgrounds.std(axis=0)],
    }
    voice_stats, voice_scores = _voice_calibration()
    motion_stats, motion_scores = _motion_calibration()
    stats["voice"] = voice_stats
    stats["motion"] = motion_stats

    pairs, pruned_probes = _pair_scores_all(corpus, observations=observations, stats=stats)
    calibration_scores = {dimension: entry["calibration"] for dimension, entry in pairs.items()}
    holdout_scores = {dimension: entry["holdout"] for dimension, entry in pairs.items()}
    calibration_scores["voice"] = voice_scores["calibration"]
    holdout_scores["voice"] = voice_scores["holdout"]
    calibration_scores["motion"] = motion_scores["calibration"]
    holdout_scores["motion"] = motion_scores["holdout"]

    calibration = build_calibration(
        dimension_scores=calibration_scores,
        stats=stats,
        dataset_digest=corpus.digest,
        seed=config.seed,
        created_at=_now(),
        holdout=holdout_scores,
        genuine_quantile=config.calibration_quantiles[0],
        impostor_quantile=config.calibration_quantiles[1],
    )
    (out / "calibration.json").write_text(
        json.dumps(calibration.to_dict(), ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    # Scenarios against the real evaluator.
    outcomes: list[ScenarioOutcome] = []
    reports: list[dict[str, Any]] = []
    for scenario in _scenarios(corpus):
        image = corpus.load_key(scenario.image_key)
        reference_images = [corpus.load_key(key) for key in scenario.reference_keys]
        shot = _shot_for(
            scenario,
            capsule=capsule,
            corpus=corpus,
            image_asset_id=f"AST-{scenario.shot_id}",
        )
        scene_environment = timeline.scene(scenario.scene_id).environment
        evaluator = ContinuityEvaluator(calibration).with_scene_reference(
            hex_to_rgb(scene_environment.background_colour)
        )
        started_shot = time.perf_counter()
        report = evaluator.evaluate(
            shot=shot,
            capsule=capsule,
            image=image,
            reference_images=reference_images,
            timeline=timeline,
            voice_samples=voice_samples(CHARACTERS[0].character_id)
            if scenario.with_voice
            else None,
            reference_voice=voice_samples(CHARACTERS[0].character_id)
            if scenario.with_voice
            else None,
            motion_sequence=motion_sequence(CHARACTERS[0].character_id)
            if scenario.with_motion
            else None,
            reference_motion=(
                motion_sequence(CHARACTERS[0].character_id) if scenario.with_motion else None
            ),
        )
        elapsed_ms = (time.perf_counter() - started_shot) * 1000.0
        report = _with_timing(report, elapsed_ms)
        passed = report.verdict in scenario.expected
        outcomes.append(
            ScenarioOutcome(
                scenario=scenario,
                report=report.to_dict(),
                passed=passed,
                processing_ms=elapsed_ms,
            )
        )
        reports.append(report.to_dict())

    (out / "continuity_reports.json").write_text(
        json.dumps(reports, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )

    # Metrics measured from the held-out split.
    holdout_metrics: dict[str, Any] = {}
    for dimension, threshold in sorted(calibration.thresholds.items()):
        entry = holdout_scores.get(dimension)
        if not entry:
            continue
        genuine = entry.get("genuine", [])
        impostor = entry.get("impostor", [])
        if not genuine or not impostor:
            holdout_metrics[dimension] = {"status": HUMAN_REVIEW, "reason": "no held-out pairs"}
            continue
        point = operating_point(genuine, impostor, threshold.accept)
        holdout_metrics[dimension] = {
            "at_accept_boundary": point,
            "eer": equal_error_rate(genuine, impostor),
            "genuine_n": len(genuine),
            "impostor_n": len(impostor),
        }

    total_ms = (time.perf_counter() - started) * 1000.0
    peak_rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
    results = {
        "schema": "character-capsule-benchmark/1",
        "seed": config.seed,
        "created_at": _now(),
        "corpus": {
            "root": str(work),
            "digest": corpus.digest,
            "frames": len(corpus.frames),
            "characters": [spec.character_id for spec in CHARACTERS],
            "audio": [f"audio/{spec.character_id}-voice.wav" for spec in CHARACTERS],
            "synthetic_only": True,
        },
        "calibration": {
            "digest": calibration.digest,
            "pruned_probes": {
                "quality": pruned_probes["quality"],
                "canon_marker": pruned_probes["canon_marker"],
                "policy": (
                    "quality: face below QUALITY_GATES['face']; "
                    "canon_marker: frame carries a canon-declared appearance change"
                ),
            },
            "thresholds": {
                key: value.to_dict() for key, value in sorted(calibration.thresholds.items())
            },
            "quality_floor": calibration.quality_floor,
        },
        "holdout_metrics": holdout_metrics,
        "scenarios": [outcome.to_dict() for outcome in outcomes],
        "summary": _summary(outcomes),
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "machine": platform.machine(),
            "cpu_count": _cpu_count(),
            "gpu": "NOT_MEASURED: no GPU device present (nvidia-smi unavailable)",
            "peak_rss_mb": round(peak_rss_mb, 1),
            "total_wall_ms": round(total_ms, 1),
            "mean_scenario_ms": round(
                float(np.mean([outcome.processing_ms for outcome in outcomes])), 3
            ),
        },
        "not_measured": {
            "human_review": HUMAN_REVIEW,
            "human_review_reason": "no human reviewer available in this environment",
            "gpu_usage": "NOT_MEASURED: no GPU device",
            "learned_face_embedding": (
                "NOT_MEASURED: no licensed face-embedding model available offline; "
                "classical descriptors used instead"
            ),
            "photographic_transfer": (
                "NOT_MEASURED: corpus is procedural; thresholds do not transfer to photographs"
            ),
        },
    }
    results["results_digest"] = digest_of(
        {key: value for key, value in results.items() if key not in {"created_at", "environment"}}
    )
    (out / "benchmark_results.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    (out / "BENCHMARK.md").write_text(render_markdown(results), encoding="utf-8")
    results["evidence"] = _write_evidence(
        out=out,
        corpus=corpus,
        capsule=capsule,
        calibration_digest=calibration.digest,
        results=results,
    )
    (out / "benchmark_results.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    return results


def _write_evidence(
    *,
    out: Path,
    corpus: Corpus,
    capsule: CharacterCapsule,
    calibration_digest: str,
    results: Mapping[str, Any],
) -> dict[str, Any]:
    """Write a content-addressed evidence bundle for the whole run.

    The bundle is signed only when a signing key is configured outside the
    repository; otherwise it is written unsigned and verification says so. A
    prototype that silently signed with a checked-in key would be worse than one
    that admits it has no key.
    """
    manifest_path = out / "corpus_manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "digest": corpus.digest,
                "frames": [
                    {
                        "key": record.key,
                        "path": record.path,
                        "purpose": record.purpose,
                        "condition": record.condition,
                        "kind": record.kind,
                        "digest": digest_of(
                            {
                                "key": record.key,
                                "purpose": record.purpose,
                                "condition": record.condition,
                                "outfit_id": record.outfit_id,
                                "background": record.background,
                                "key_kelvin": record.key_kelvin,
                                "kind": record.kind,
                                "labels": dict(record.labels),
                            }
                        ),
                    }
                    for record in corpus.frames
                ],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    # ``benchmark_results.json`` embeds this bundle's own digest, so it is
    # deliberately *not* a record: binding a document that contains the binding is
    # circular. C2PA solves the same problem by excluding the manifest bytes from
    # the hard binding; here the exclusion is one whole file, stated explicitly.
    artifacts = {
        "calibration.json": ("calibration", "application/json"),
        "continuity_reports.json": ("reports", "application/json"),
        "corpus_manifest.json": ("corpus", "application/json"),
    }
    present = {str(out / name): spec for name, spec in artifacts.items() if (out / name).exists()}
    bundle = bundle_from_files(
        bundle_id="EVD-NAGAR-CHAR-000001-RUN",
        subject={
            "character_id": capsule.character_id,
            "capsule_digest": capsule.digest,
            "calibration_digest": calibration_digest,
            "media_type": "application/json",
        },
        assertions={
            "summary": results["summary"],
            "scenarios": [
                {
                    "scenario": entry["scenario"],
                    "actual": entry["actual"],
                    "passed": entry["passed"],
                }
                for entry in results["scenarios"]
            ],
            "synthetic_marking": {
                "ai_generated": True,
                "all_subjects_synthetic": True,
                "regulation": (
                    "EU AI Act Art. 50(2) machine-readable marking applies from 2026-08-02"
                ),
            },
            "not_measured": results["not_measured"],
            "binding_scope": {
                "bound": sorted(artifacts),
                "excluded": {
                    "benchmark_results.json": (
                        "contains this bundle's digest; binding it would be circular"
                    )
                },
            },
        },
        files=present,
        lineage=[capsule.digest, corpus.digest],
        created_at=_now(),
        disclosure={
            "ai_generated": True,
            "real_person": False,
            "consent": "not applicable: every subject in this corpus is synthetic",
        },
    )
    signed = False
    try:
        key = load_signing_key()
    except Exception:  # noqa: BLE001 - a missing key is an expected state, not a failure
        key = None
    if key is not None:
        bundle = sign_bundle(bundle, signing_key=key, signed_at=_now())
        signed = True
    path = out / "evidence_bundle.json"
    path.write_text(
        json.dumps(bundle.to_dict(), ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    verification = verify_bundle(bundle)
    return {
        "bundle_id": bundle.bundle_id,
        "path": str(path),
        "digest": bundle.digest,
        "claim_digest": bundle.claim_digest,
        "signed": signed,
        "signature_algorithm": None if bundle.signature is None else bundle.signature.algorithm,
        "verification": verification.to_dict(),
        "c2pa_manifest_shape": bundle.c2pa_manifest_shape(),
    }


def _with_timing(report: Any, elapsed_ms: float) -> Any:
    from dataclasses import replace

    return replace(report, processing_ms=elapsed_ms)


def _pair_scores_all(
    corpus: Corpus,
    *,
    observations: Mapping[str, Observation],
    stats: Mapping[str, Any],
) -> tuple[dict[str, dict[str, dict[str, list[float]]]], dict[str, list[str]]]:
    """Genuine/impostor distances computed the way the evaluator computes them.

    For every non-reference frame the score against a gallery is the **minimum**
    distance over that gallery's canonical references, because that is exactly
    what ``ContinuityEvaluator`` does at verification time. Fitting thresholds on
    single reference pairs instead would calibrate a different (harder) geometry
    than the one that is deployed, which inflates every threshold.
    """
    _, holdout_ids = character_split(corpus)
    characters = sorted({record.character_id for record in corpus.frames})

    def gallery_keys(character_id: str, dimension: str) -> list[str]:
        kind = "full_body" if dimension == "body" else "portrait"
        return [record.key for record in corpus.references(character_id, kind=kind)]

    # Probes are frames the capsule was *not* built from, plus the alternative
    # full-body references (needed so the body dimension has leave-one-out pairs).
    candidates = [
        record
        for record in corpus.frames
        if record.purpose in {"eval", "drift"}
        or (record.purpose == "reference_alt" and record.kind == "full_body")
    ]
    # Quality pruning and canon-marker pruning, both declared policies rather than
    # convenience filters: (1) a frame below the quality gate is not evidence of
    # identity either way (ISO/IEC 29794-5 style pruning); (2) a frame carrying a
    # canon-declared appearance change is *expected* to differ and must not widen
    # the identity tolerance. Pruned probes are counted and reported.
    probes = []
    pruned: dict[str, list[str]] = {"quality": [], "canon_marker": []}
    for record in candidates:
        observation = observations[record.key]
        if record.labels.get("canon_marker"):
            pruned["canon_marker"].append(record.key)
            continue
        if observation.face_quality > 0.0 and observation.face_quality < QUALITY_GATES["face"]:
            pruned["quality"].append(record.key)
            continue
        probes.append(record)

    result: dict[str, dict[str, dict[str, list[float]]]] = {
        dimension: {
            "calibration": {"genuine": [], "impostor": []},
            "holdout": {"genuine": [], "impostor": []},
        }
        for dimension in ("face", "body", "hair", "wardrobe", "scene")
    }
    for record in probes:
        probe = observations[record.key]
        split = "holdout" if record.character_id in holdout_ids else "calibration"
        for dimension in ("face", "body"):
            if not probe.measurable(dimension):
                continue
            same = [
                key
                for key in gallery_keys(record.character_id, dimension)
                if key != record.key and observations[key].measurable(dimension)
            ]
            # Drift variants are deliberate identity changes: they are labelled
            # impostor *against their own character*, never genuine.
            if same and record.purpose != "drift":
                result[dimension][split]["genuine"].append(
                    min(
                        distance(observations[key], probe, dimension, stats=stats.get(dimension))
                        for key in same
                    )
                )
            other = [
                key
                for character_id in characters
                if character_id != record.character_id
                for key in gallery_keys(character_id, dimension)
                if observations[key].measurable(dimension)
            ]
            if record.purpose == "drift":
                other = other + same
            if other:
                result[dimension][split]["impostor"].append(
                    min(
                        distance(observations[key], probe, dimension, stats=stats.get(dimension))
                        for key in other
                    )
                )
    # Wardrobe is not an identity dimension either: two different characters may
    # legitimately wear the same costume. It is calibrated *within* a character —
    # same outfit vs changed outfit — which is exactly the failure the wardrobe
    # state is meant to catch.
    for character_id in characters:
        frames = [
            record
            for record in corpus.frames
            if record.character_id == character_id
            and record.purpose != "drift"
            and observations[record.key].measurable("wardrobe")
        ]
        split = "holdout" if character_id in holdout_ids else "calibration"
        for position, left in enumerate(frames):
            for right in frames[position + 1 :]:
                family = "genuine" if left.outfit_id == right.outfit_id else "impostor"
                result["wardrobe"][split][family].append(
                    distance(
                        observations[left.key],
                        observations[right.key],
                        "wardrobe",
                        stats=stats.get("wardrobe"),
                    )
                )

    # Scene is an *environment* dimension, not an identity one: two different
    # characters standing on the same set are the same scene. It is therefore
    # calibrated on declared scene membership (identical background colour),
    # never on genuine/impostor identity pairs.
    by_background: dict[str, list[str]] = {}
    for record in corpus.frames:
        by_background.setdefault(record.background, []).append(record.key)
    backgrounds = sorted(by_background)
    for index, background in enumerate(backgrounds):
        keys = by_background[background]
        split = "holdout" if corpus.frame(keys[0]).character_id in holdout_ids else "calibration"
        for position, left in enumerate(keys):
            for right in keys[position + 1 :]:
                result["scene"][split]["genuine"].append(
                    scene_distance(observations[left], observations[right])
                )
        for other in backgrounds[index + 1 :]:
            counterpart = by_background[other]
            pairs = list(zip(keys[:4], counterpart[:4], strict=False))
            for left, right in pairs:
                result["scene"][split]["impostor"].append(
                    scene_distance(observations[left], observations[right])
                )
    return result, pruned


def _summary(outcomes: Sequence[ScenarioOutcome]) -> dict[str, Any]:
    passed = sum(1 for outcome in outcomes if outcome.passed)
    by_band: dict[str, int] = {}
    for outcome in outcomes:
        for entry in outcome.report["dimensions"]:
            by_band[entry["band"]] = by_band.get(entry["band"], 0) + 1
    return {
        "scenarios": len(outcomes),
        "passed": passed,
        "failed": len(outcomes) - passed,
        "accuracy": round(passed / len(outcomes), 6) if outcomes else 0.0,
        "dimension_bands": dict(sorted(by_band.items())),
        "verdicts": sorted({outcome.report["verdict"] for outcome in outcomes}),
    }


def _cpu_count() -> int:
    import os

    return os.cpu_count() or 1


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def render_markdown(results: Mapping[str, Any]) -> str:
    """Render the measured results as Markdown (no invented numbers)."""
    lines: list[str] = [
        "# Character continuity benchmark — measured results",
        "",
        f"- generated: `{results['created_at']}`",
        f"- corpus digest: `{results['corpus']['digest']}`",
        f"- calibration digest: `{results['calibration']['digest']}`",
        f"- results digest: `{results['results_digest']}`",
        "",
        "## Thresholds (fitted on calibration characters, applied to a held-out character)",
        "",
        "| dimension | accept ≤ | reject ≥ | EER (held-out) | FAR @ accept | FRR @ accept |",
        "|---|---|---|---|---|---|",
    ]
    holdout = results.get("holdout_metrics", {})
    for dimension, threshold in results["calibration"]["thresholds"].items():
        entry = holdout.get(dimension, {})
        eer = entry.get("eer", {}).get("eer", "NOT_MEASURED")
        point = entry.get("at_accept_boundary", {})
        lines.append(
            f"| {dimension} | {threshold['accept']:.4f} | {threshold['reject']:.4f} | "
            f"{eer} | {point.get('far', 'NOT_MEASURED')} | {point.get('frr', 'NOT_MEASURED')} |"
        )
    lines += [
        "",
        "## Scenarios",
        "",
        "| scenario | condition | expected | actual | passed | face | body | wardrobe | canon |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for outcome in results["scenarios"]:
        dims = outcome["dimensions"]
        lines.append(
            "| {name} | {condition} | {expected} | {actual} | {passed} | {face} | {body} | "
            "{wardrobe} | {canon} |".format(
                name=outcome["scenario"],
                condition=outcome["condition"],
                expected=" / ".join(outcome["expected"]),
                actual=outcome["actual"],
                passed="yes" if outcome["passed"] else "NO",
                face=dims.get("face", {}).get("band", "n/a"),
                body=dims.get("body", {}).get("band", "n/a"),
                wardrobe=dims.get("wardrobe", {}).get("band", "n/a"),
                canon=dims.get("canon", {}).get("state", "n/a"),
            )
        )
    summary = results["summary"]
    lines += [
        "",
        f"Scenarios passed: **{summary['passed']}/{summary['scenarios']}** "
        f"(accuracy {summary['accuracy']:.3f}).",
        "",
        "## Environment",
        "",
        f"- python {results['environment']['python']} on {results['environment']['platform']}",
        f"- CPU count: {results['environment']['cpu_count']}",
        f"- GPU: {results['environment']['gpu']}",
        f"- peak RSS: {results['environment']['peak_rss_mb']} MB",
        f"- total wall time: {results['environment']['total_wall_ms']} ms",
        "",
        "## Not measured",
        "",
    ]
    for key, value in results["not_measured"].items():
        lines.append(f"- `{key}`: {value}")
    lines.append("")
    lines.append(
        "Thresholds are fitted on synthetic frames and are **not** valid for photographic "
        "or model-generated faces; see `VISUAL_CONTINUITY_EVALUATION.md`."
    )
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point used by ``scripts/character_capsule/run_benchmark.py``."""
    import argparse

    parser = argparse.ArgumentParser(description="run the character continuity benchmark")
    parser.add_argument("--work-dir", default="experiments/character_capsule/.work")
    parser.add_argument("--out-dir", default="")
    args = parser.parse_args(list(argv) if argv is not None else None)
    config = BenchmarkConfig(
        work_dir=Path(args.work_dir),
        out_dir=Path(args.out_dir) if args.out_dir else None,
    )
    results = run_benchmark(config)
    print(
        json.dumps(
            {
                "corpus_digest": results["corpus"]["digest"],
                "calibration_digest": results["calibration"]["digest"],
                "results_digest": results["results_digest"],
                "summary": results["summary"],
                "holdout_metrics": results["holdout_metrics"],
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0 if results["summary"]["failed"] == 0 else 1


__all__ = [
    "BenchmarkConfig",
    "HUMAN_REVIEW",
    "Scenario",
    "build_calibration",
    "main",
    "render_markdown",
    "run_benchmark",
    "short_digest",
]


if __name__ == "__main__":  # pragma: no cover - exercised through scripts/
    raise SystemExit(main())
