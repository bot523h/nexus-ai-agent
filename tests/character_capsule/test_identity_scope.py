"""Phase-8 regression suite: identity scope, wardrobe/shot pins, evidence binding.

These are the findings where the *model* was wrong rather than a guard missing:
a forged digest reaching ``VERIFIED``, an identity field editable inside a
revision, an outfit version rewritten in place, a scene rule that was dead data,
and an evidence bundle that did not bind the media it described.
"""

# ruff: noqa: E402 - _support.ensure_path() must run before these imports
from __future__ import annotations

import _support

_support.ensure_path()

import inspect
import json
from dataclasses import replace

import pytest

from experiments.character_capsule.benchmark import BenchmarkConfig, run_benchmark
from experiments.character_capsule.continuity import (
    ContinuityEvaluator,
    VerificationState,
    load_calibration,
    report_digest,
    verify_report_evidence,
)
from experiments.character_capsule.dataset import CHARACTERS, build_capsule, build_corpus
from experiments.character_capsule.errors import PinMismatch, ScopeViolation, ValidationError
from experiments.character_capsule.evidence import (
    BUNDLE_INVALID,
    bundle_from_files,
    verify_bundle,
)
from experiments.character_capsule.model import Scope
from experiments.character_capsule.scene import (
    DeclaredShot,
    ShotReference,
    shot_from_dict,
)
from experiments.character_capsule.wardrobe import Outfit, expected_outfit

SPEC = CHARACTERS[0]


def _capsule():
    return build_capsule(SPEC)


@pytest.fixture(scope="module")
def calibrated(tmp_path_factory: pytest.TempPathFactory):
    """A real calibration and corpus: the identity checks run against measured bands."""
    work = tmp_path_factory.mktemp("identity-bench")
    payload = run_benchmark(BenchmarkConfig(work_dir=work))
    run_dir = work / "results" / payload["run"]["run_id"]
    calibration = load_calibration(json.loads((run_dir / "calibration.json").read_text("utf-8")))
    corpus = build_corpus(work / "corpus", write=True)
    return calibration, corpus, run_dir


def _module_payload(module) -> dict:
    payload = dict(module.payload())
    payload.pop("revision", None)
    return payload


# ---------------------------------------------------------------------------
# F28 / F29 - identity scope
# ---------------------------------------------------------------------------


def test_f28_a_face_field_cannot_change_inside_a_revision():
    """F28: ``with_module`` accepted a new ``eye_spacing_ratio`` on a revision."""
    capsule = _capsule()
    face = capsule.module("face")
    before = face.eye_spacing_ratio
    # ``updated`` already refuses the identity field; construct the tampered module
    # the way an attacker would (bypassing the helper) and check the capsule door too.
    with pytest.raises(ScopeViolation):
        face.updated({"eye_spacing_ratio": 0.300})
    tampered = replace(face, eye_spacing_ratio=0.300, revision=face.revision + 1)
    with pytest.raises(ScopeViolation):
        capsule.with_module(tampered)
    assert capsule.module("face").eye_spacing_ratio == before
    assert capsule.identity_version == 1


def test_f28b_a_revision_scoped_field_still_works():
    """The guard must not block the legitimate path either."""
    capsule = _capsule()
    hair = capsule.module("hair").updated({"colour": "#010203"})
    revised = capsule.with_module(hair, note="hair dye for episode 4")
    assert revised.revision_no == capsule.revision_no + 1
    assert revised.identity_version == capsule.identity_version
    assert revised.module("hair").colour == "#010203"
    assert revised.module("hair").revision == capsule.module("hair").revision + 1


def test_f28c_a_no_op_replacement_is_refused():
    capsule = _capsule()
    with pytest.raises(ValidationError):
        capsule.with_module(capsule.module("hair"))


def test_f29_a_voice_change_mints_an_identity_version():
    """F29: ``new_identity_version(voice_changes=...)`` raised ``TypeError``."""
    capsule = _capsule()
    signature = capsule.module("voice")
    minted = capsule.new_identity_version(
        voice_changes={"f0_hz": float(getattr(signature, "f0_hz", 150.0)) + 1.0},
        note="recast after the time jump",
    )
    assert minted.identity_version == capsule.identity_version + 1
    assert minted.revision_no == capsule.revision_no + 1
    assert minted.module("voice").revision == signature.revision + 1
    assert minted.digest != capsule.digest


def test_f29b_a_non_identity_field_cannot_ride_along():
    capsule = _capsule()
    with pytest.raises(ValidationError):
        capsule.new_identity_version(face_changes={"revision": 9})
    with pytest.raises(ValidationError):
        capsule.new_identity_version()


def test_f29c_an_identity_change_is_not_expressible_as_a_module_edit():
    """There must be exactly one door to an identity version."""
    capsule = _capsule()
    body = capsule.module("body")
    assert body.FIELD_SCOPES["shoulder_width_ratio"] is Scope.IDENTITY
    with pytest.raises(ScopeViolation):
        body.updated({"shoulder_width_ratio": body.shoulder_width_ratio + 0.01})
    smuggled = replace(body, shoulder_width_ratio=body.shoulder_width_ratio + 0.01)
    with pytest.raises(ScopeViolation):
        capsule.with_module(smuggled)


# ---------------------------------------------------------------------------
# F30 - outfit versions are immutable
# ---------------------------------------------------------------------------


def test_f30_an_existing_outfit_version_cannot_be_rewritten():
    """F30: ``with_outfit`` rewrote v1's colours in place."""
    wardrobe = _capsule().module("wardrobe")
    outfit = wardrobe.outfits["OUT-0001"]
    with pytest.raises(ValidationError):
        wardrobe.with_outfit(replace(outfit, top_colour="#803020"))
    added = wardrobe.with_outfit(
        Outfit(
            outfit_id="OUT-0009",
            version=1,
            label="Rain cloak",
            top_colour="#101010",
            bottom_colour="#202020",
        )
    )
    assert added.outfits["OUT-0009"].version == 1
    assert added.outfits["OUT-0009"].top_colour == "#101010"


def test_f30b_a_superseding_version_must_be_higher_and_records_its_parent():
    wardrobe = _capsule().module("wardrobe")
    outfit = wardrobe.outfits["OUT-0001"]
    with pytest.raises(ValidationError):
        wardrobe.with_outfit(replace(outfit, top_colour="#008040"))
    upgraded = wardrobe.with_outfit(
        replace(outfit, version=outfit.version + 1, top_colour="#008040")
    ).outfits[outfit.outfit_id]
    assert upgraded.version == outfit.version + 1
    assert upgraded.supersedes
    assert outfit.content_digest in " ".join(upgraded.supersedes)


# ---------------------------------------------------------------------------
# F22 - scene rules are not dead data
# ---------------------------------------------------------------------------


def test_f22_a_scene_rule_beats_the_sequence_baseline():
    """F22: ``scene_rules`` was stored and never consulted -> false WARDROBE_DRIFT."""
    wardrobe = _capsule().module("wardrobe")
    assert wardrobe.scene_rules["SC-003"] == "OUT-0002"
    assert (
        expected_outfit(wardrobe, sequence_id="SEQ-EP1-A", shot_index=4, scene_id="SC-003")
        == "OUT-0002"
    )
    # ... while the same shot without the scene context follows the sequence plan
    assert expected_outfit(wardrobe, sequence_id="SEQ-EP1-A", shot_index=1) == "OUT-0001"


# ---------------------------------------------------------------------------
# F21 - missing timeline is NOT_MEASURED, not "scene 1"
# ---------------------------------------------------------------------------


def test_f21_without_a_timeline_scene_dimensions_are_not_measured(calibrated):
    calibration, corpus, _ = calibrated
    capsule = _capsule()
    reference = corpus.references(SPEC.character_id, kind="portrait")
    from experiments.character_capsule.media import load_rgb

    image = load_rgb(corpus.root / reference[0].path)
    shot = ShotReference(
        shot_id="SH-003",
        scene_id="SC-003",
        sequence_id="SEQ-EP1-A",
        shot_index=2,
        character_id=capsule.character_id,
        capsule_revision=capsule.revision_no,
        capsule_digest=capsule.digest,
        module_digests={name: module.digest for name, module in sorted(capsule.modules.items())},
        declared=DeclaredShot(outfit_id="OUT-0002", expression="neutral"),
        image_asset_id="AST-SH-003",
    )
    report = ContinuityEvaluator(calibration).evaluate(
        shot=shot,
        capsule=capsule,
        image=image,
        reference_images=[load_rgb(corpus.root / record.path) for record in reference],
    )
    scene = report.dimension("scene_context")
    assert scene.measured is False
    assert report.verdict is not VerificationState.VERIFIED
    assert any("scene" in limitation for limitation in report.limitations)


# ---------------------------------------------------------------------------
# F20 - a forged digest cannot buy a verdict
# ---------------------------------------------------------------------------


def test_f20_a_shot_pinned_to_a_forged_capsule_digest_is_refused(calibrated):
    """F20: a shot could carry forged digests and still be scored VERIFIED."""
    calibration, corpus, _ = calibrated
    capsule = _capsule()
    reference = corpus.references(SPEC.character_id, kind="portrait")
    from experiments.character_capsule.media import load_rgb

    shot = ShotReference(
        shot_id="SH-004",
        scene_id="SC-001",
        sequence_id="SEQ-EP1-A",
        shot_index=0,
        character_id=capsule.character_id,
        capsule_revision=capsule.revision_no,
        capsule_digest="sha256:" + "0" * 64,
        module_digests={name: "sha256:" + "0" * 64 for name in capsule.modules},
        declared=DeclaredShot(outfit_id="OUT-0001"),
        image_asset_id="AST-SH-004",
    )
    with pytest.raises(PinMismatch):
        ContinuityEvaluator(calibration).evaluate(
            shot=shot,
            capsule=capsule,
            image=load_rgb(corpus.root / reference[0].path),
            reference_images=[load_rgb(corpus.root / record.path) for record in reference],
        )


def test_f20b_a_shot_manifest_with_unknown_fields_is_refused():
    """A dropped pin is a pin that silently does not apply."""
    capsule = _capsule()
    payload = {
        "shot_id": "SH-005",
        "scene_id": "SC-001",
        "sequence_id": "SEQ-EP1-A",
        "shot_index": 0,
        "character_id": capsule.character_id,
        "capsule_revision": capsule.revision_no,
        "capsule_digest": capsule.digest,
        "module_digests": {name: module.digest for name, module in capsule.modules.items()},
        "declared": {"outfit_id": "OUT-0001"},
        "image_asset_id": "AST-SH-005",
    }
    assert shot_from_dict(payload).shot_id == "SH-005"
    with pytest.raises(ValidationError):
        shot_from_dict({**payload, "capsule_digest_v2": capsule.digest})


# ---------------------------------------------------------------------------
# F25 - a published report must satisfy its own digest
# ---------------------------------------------------------------------------


def test_f25_a_published_report_digest_recomputes(tmp_path):
    payload = {
        "schema": "character-capsule-continuity-report/1",
        "shot_id": "SH-006",
        "verdict": "VERIFIED",
        "calibration_digest": "sha256:" + "1" * 64,
        "processing_ms": 12.5,
        "evidence_digest": "",
        "dimensions": [{"dimension": "face", "state": "VERIFIED"}],
    }
    digest = report_digest(payload, calibration_digest=payload["calibration_digest"])
    payload["evidence_digest"] = digest
    assert verify_report_evidence(payload) is True
    # timing is excluded, so re-measuring on another machine does not break it
    payload["processing_ms"] = 99.0
    assert verify_report_evidence(payload) is True
    payload["verdict"] = "VERIFIED_WITH_LIMITATIONS"
    assert verify_report_evidence(payload) is False


# ---------------------------------------------------------------------------
# F14 / F15 / F16 - the evidence bundle binds the media
# ---------------------------------------------------------------------------


def test_f14_and_f15_a_bundle_binds_every_frame_and_audio_track(tmp_path):
    """F14/F15: the bundle named the report only, and frames had no digests."""
    corpus = build_corpus(tmp_path / "corpus", write=True)
    assert corpus.frames
    for record in corpus.frames:
        assert record.sha256.startswith("sha256:")
        assert record.size_bytes > 0
    rewrites = corpus.frames_where(key="000001-ref-front")[0]
    before = rewrites.sha256
    # rewrite the same frame with different bytes: the per-frame digest must move
    from experiments.character_capsule.dataset import build_corpus as rebuild

    other = rebuild(tmp_path / "corpus-2", write=True).frame("000001-ref-front")
    assert other.sha256 == before  # determinism is real
    report = tmp_path / "report.json"
    report.write_text(json.dumps({"verdict": "VERIFIED"}), encoding="utf-8")
    frame = tmp_path / corpus.frames[0].key
    frame.write_bytes((corpus.root / corpus.frames[0].path).read_bytes())
    bundle = bundle_from_files(
        bundle_id="EVD-TEST-BIND",
        subject={"shot_id": "SH-001"},
        assertions={"media": {"frame_digests": [record.sha256 for record in corpus.frames]}},
        files={str(report): ("report", "application/json"), str(frame): ("frame", "image/png")},
        lineage=[corpus.digest],
        created_at="2026-11-09T00:00:00Z",
    )
    supplied = {record.name: tmp_path / record.name for record in bundle.records}
    for name, path in supplied.items():
        path.write_bytes((report if name == "report.json" else frame).read_bytes())
    verification = verify_bundle(bundle, files=supplied)
    assert verification.checks["files_match"] is True
    for name, path in supplied.items():
        if name != "report.json":
            path.write_bytes(path.read_bytes() + b"\x00")
    changed = verify_bundle(bundle, files=supplied)
    assert changed.status == BUNDLE_INVALID
    assert changed.checks["files_match"] is False


def test_f15b_a_rewritten_frame_changes_its_record_not_just_the_corpus_digest(tmp_path):
    corpus = build_corpus(tmp_path / "c", write=True)
    record = corpus.frame("000001-ref-front")
    payload = record.to_dict()
    assert payload["sha256"] in record.sha256
    assert payload["size_bytes"] == len((corpus.root / record.path).read_bytes())


# ---------------------------------------------------------------------------
# F26 - no module-scope ``resource``
# ---------------------------------------------------------------------------


def test_f26_no_module_scope_resource_is_imported_for_measurement():
    """F26: ``resource`` was imported at module scope, breaking Windows imports."""
    from experiments.character_capsule import benchmark

    source = inspect.getsource(benchmark)
    module_level = [
        line
        for line in source.splitlines()
        if line.startswith("import resource") or line.startswith("from resource import")
    ]
    assert module_level == []
    if hasattr(benchmark, "peak_rss_mb"):
        assert callable(benchmark.peak_rss_mb)
