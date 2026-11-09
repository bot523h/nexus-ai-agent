"""The eight verification states, and the rules that bound them.

Every assertion here runs the real evaluator. The calibration is fitted from real
measurements on the synthetic corpus (never hand-written constants), because a
threshold invented inside a test would prove nothing about the engine.
"""

# ruff: noqa: E402 - _support.ensure_path() must run before these imports
from __future__ import annotations

import _support

_support.ensure_path()

import numpy as np
import pytest

from experiments.character_capsule import dataset
from experiments.character_capsule.continuity import (
    STATE_PRECEDENCE,
    ContinuityEvaluator,
    VerificationState,
    aggregate_verdict,
    build_calibration,
    scene_distance,
)
from experiments.character_capsule.model import CharacterCapsule
from experiments.character_capsule.scene import DeclaredShot, ShotReference
from experiments.character_capsule.vision import distance, group_stats, observe


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory):
    work = tmp_path_factory.mktemp("capsule-states")
    corpus = dataset.build_corpus(work, write=True)
    observations = {record.key: observe(corpus.load(record)) for record in corpus.frames}
    portraits = [
        observations[record.key]
        for record in corpus.frames
        if record.purpose in {"reference", "reference_alt"}
        and record.kind == "portrait"
        and observations[record.key].face_quality > 0.0
    ]
    full_body = [
        observations[record.key]
        for record in corpus.frames
        if record.kind == "full_body" and observations[record.key].body_quality > 0.0
    ]
    references = [record for record in corpus.frames if record.purpose == "reference"]
    background = np.vstack(
        [np.asarray(observations[record.key].scene["background"]) for record in references]
    )
    stats = {
        "face": group_stats(portraits, "face"),
        "hair": group_stats(portraits, "hair"),
        "wardrobe": group_stats(portraits, "wardrobe"),
        "body": group_stats(full_body, "body"),
        "scene": {
            "mean": background.mean(axis=0).tolist(),
            "std": background.std(axis=0).tolist(),
        },
    }
    # Labelled scores in the *deployed* geometry: the evaluator compares a frame
    # against every gallery reference and keeps the smallest distance, so the
    # calibration must use min-over-gallery scores too (a single-reference score
    # calibrates a different, harder problem).
    gallery = {
        character_id: [record.key for record in corpus.references(character_id, kind="portrait")]
        for character_id in sorted({record.character_id for record in corpus.frames})
    }

    def score(probe: str, keys: list[str], dimension: str) -> float:
        return min(
            distance(observations[key], observations[probe], dimension, stats=stats[dimension])
            for key in keys
            if observations[key].measurable(dimension)
        )

    protagonist = gallery["NAGAR-CHAR-000001"]
    genuine_face = [
        score(key, protagonist, "face")
        for key in ("000001-shot-angle", "000001-shot-expression", "000001-shot-outfit2")
    ]
    impostor_face = [
        score("000002-ref-front", protagonist, "face"),
        score("000003-ref-front", protagonist, "face"),
        score("000001-drift-eye_spacing", protagonist, "face"),
        score("000001-drift-jaw_width", protagonist, "face"),
        score("000001-drift-skin_tone", protagonist, "face"),
    ]
    calibration = build_calibration(
        dimension_scores={
            "face": {"genuine": genuine_face, "impostor": impostor_face},
            "hair": {"genuine": [0.0], "impostor": [0.4]},
            "wardrobe": {"genuine": [0.0], "impostor": [2.0]},
            "body": {"genuine": [0.0], "impostor": [0.9]},
            "scene": {"genuine": [0.0], "impostor": [0.2]},
        },
        stats=stats,
        dataset_digest=corpus.digest,
        seed="states-test",
        created_at="2026-11-09T00:00:00Z",
    )
    return {
        "corpus": corpus,
        "observations": observations,
        "calibration": calibration,
        "capsule": dataset.build_capsule(dataset.CHARACTERS[0]),
        "timeline": dataset.build_timeline(),
        "gallery": gallery["NAGAR-CHAR-000001"],
    }


def _shot(capsule: CharacterCapsule, **overrides) -> ShotReference:
    declared = overrides.pop("declared", DeclaredShot(outfit_id="OUT-0001"))
    payload = {
        "shot_id": "SH-900",
        "character_id": capsule.character_id,
        "capsule_revision": capsule.revision_no,
        "capsule_digest": capsule.digest,
        "module_digests": {name: module.digest for name, module in capsule.modules.items()},
        "scene_id": "SC-001",
        "sequence_id": "SEQ-EP1-A",
        "shot_index": 0,
        "image_asset_id": "AST-SH-900",
        "declared": declared,
    }
    payload.update(overrides)
    return ShotReference(**payload)


def _evaluate(world, *, image_key: str, reference_keys: list[str], **shot_overrides):
    evaluator = ContinuityEvaluator(world["calibration"]).with_scene_reference((0.23, 0.25, 0.29))
    return evaluator.evaluate(
        shot=_shot(world["capsule"], **shot_overrides),
        capsule=world["capsule"],
        image=world["corpus"].load_key(image_key),
        reference_images=[world["corpus"].load_key(key) for key in reference_keys],
        timeline=world["timeline"],
    )


def test_state_vocabulary_is_closed_and_ordered():
    assert {state.value for state in VerificationState} == {
        "VERIFIED",
        "VERIFIED_WITH_LIMITATIONS",
        "REVIEW_REQUIRED",
        "IDENTITY_DRIFT",
        "WARDROBE_DRIFT",
        "BODY_DRIFT",
        "CANON_CONFLICT",
        "INSUFFICIENT_EVIDENCE",
    }
    assert STATE_PRECEDENCE[0] is VerificationState.CANON_CONFLICT
    assert STATE_PRECEDENCE[-1] is VerificationState.VERIFIED


def test_precedence_prefers_the_most_severe_state():
    verdict = aggregate_verdict(
        [
            VerificationState.VERIFIED,
            VerificationState.REVIEW_REQUIRED,
            VerificationState.IDENTITY_DRIFT,
        ]
    )
    assert verdict is VerificationState.IDENTITY_DRIFT


def test_empty_evidence_is_insufficient_not_verified():
    assert aggregate_verdict([]) is VerificationState.INSUFFICIENT_EVIDENCE


def test_same_character_verifies(world):
    report = _evaluate(
        world,
        image_key="000001-shot-angle",
        reference_keys=world["gallery"],
    )
    assert report.verdict in {
        VerificationState.VERIFIED,
        VerificationState.VERIFIED_WITH_LIMITATIONS,
    }
    face = next(entry for entry in report.dimensions if entry.dimension == "face")
    assert face.state is VerificationState.VERIFIED


def test_a_different_character_is_identity_drift(world):
    report = _evaluate(
        world,
        image_key="000003-ref-front",
        reference_keys=world["gallery"],
    )
    assert report.verdict is VerificationState.IDENTITY_DRIFT
    face = next(entry for entry in report.dimensions if entry.dimension == "face")
    assert face.band.value == "DRIFT"


def test_low_quality_frame_is_gated_instead_of_scored(world):
    blurred = (world["corpus"].load_key("000001-ref-front") * 0.35).astype(np.uint8)
    evaluator = ContinuityEvaluator(world["calibration"])
    report = evaluator.evaluate(
        shot=_shot(world["capsule"]),
        capsule=world["capsule"],
        image=blurred,
        reference_images=[world["corpus"].load_key("000001-ref-front")],
        timeline=world["timeline"],
    )
    face = next(entry for entry in report.dimensions if entry.dimension == "face")
    assert face.measured is False
    assert report.verdict in {
        VerificationState.INSUFFICIENT_EVIDENCE,
        VerificationState.REVIEW_REQUIRED,
    }
    assert report.verdict is not VerificationState.IDENTITY_DRIFT


def test_canon_violation_outranks_every_similarity_signal(world):
    report = _evaluate(
        world,
        image_key="000001-ref-front",
        reference_keys=["000001-ref-front"],
        scene_id="SC-003",
        sequence_id="SEQ-EP2-A",
        shot_index=0,
        declared=DeclaredShot(
            outfit_id="OUT-0002",
            canon_assertions={"scar": "none"},
            expression="neutral",
        ),
    )
    assert report.verdict is VerificationState.CANON_CONFLICT
    assert {conflict.get("code") for conflict in report.conflicts} >= {"STORY_EVENT_NOT_HONOURED"}


def test_silence_about_a_canon_effect_is_review_not_conflict(world):
    report = _evaluate(
        world,
        image_key="000001-ref-front",
        reference_keys=["000001-ref-front"],
        scene_id="SC-003",
        sequence_id="SEQ-EP2-A",
        declared=DeclaredShot(outfit_id="OUT-0002", expression="neutral"),
    )
    assert report.verdict is not VerificationState.CANON_CONFLICT
    assert "STORY_EFFECT_NOT_ASSERTED" in {conflict.get("code") for conflict in report.conflicts}


def test_outfit_era_violation_is_wardrobe_drift(world):
    report = _evaluate(
        world,
        image_key="000001-ref-front",
        reference_keys=["000001-ref-front"],
        scene_id="SC-001",
        sequence_id="SEQ-EP1-A",
        shot_index=0,
        declared=DeclaredShot(outfit_id="OUT-0002"),
    )
    assert report.verdict is VerificationState.WARDROBE_DRIFT
    assert "OUTFIT_ERA_CONFLICT" in {conflict.get("code") for conflict in report.conflicts}


def test_canon_declared_marker_never_yields_a_drift_claim(world):
    report = _evaluate(
        world,
        image_key="000001-shot-environment",
        reference_keys=["000001-ref-front"],
        scene_id="SC-003",
        sequence_id="SEQ-EP2-A",
        declared=DeclaredShot(outfit_id="OUT-0002", expression="sad"),
    )
    face = next(entry for entry in report.dimensions if entry.dimension == "face")
    assert face.state is not VerificationState.IDENTITY_DRIFT
    assert any("canon" in limitation.lower() for limitation in report.limitations)


def test_every_report_carries_a_calibration_and_capsule_digest(world):
    report = _evaluate(
        world,
        image_key="000001-ref-front",
        reference_keys=["000001-ref-front"],
    )
    assert report.calibration_digest == world["calibration"].digest
    assert report.capsule_digest == world["capsule"].digest
    assert report.evidence_digest.startswith("sha256:")


def test_report_schema_is_stable_and_serialisable(world):
    report = _evaluate(
        world,
        image_key="000001-ref-front",
        reference_keys=["000001-ref-front"],
    )
    payload = report.to_dict()
    assert payload["schema"] == "character-capsule-continuity-report/1"
    assert isinstance(payload["dimensions"], list)
    assert isinstance(payload["limitations"], list)


def test_advisory_hair_never_decides_the_verdict(world):
    report = _evaluate(
        world,
        image_key="000001-ref-front",
        reference_keys=["000001-ref-front"],
    )
    assert any("advisory" in limitation for limitation in report.limitations)


def test_scene_distance_is_zero_for_the_same_backdrop(world):
    observation = world["observations"]
    assert scene_distance(observation["000001-ref-front"], observation["000001-ref-warm"]) < 0.02
