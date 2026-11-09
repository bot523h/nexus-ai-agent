"""End-to-end benchmark run: twelve scenarios, real numbers, no invented ones.

This is the test that keeps the deliverable honest. It runs the benchmark for real
(a few seconds on a 2-vCPU box), then asserts the properties that make the numbers
trustworthy rather than flattering:

* every scenario carries a verdict from the closed vocabulary;
* every metric is either a measured value or the string ``NOT_MEASURED``;
* the corpus digest is reproducible across runs;
* the evidence bundle is written and its recorded files match.
"""

# ruff: noqa: E402 - _support.ensure_path() must run before these imports
from __future__ import annotations

import _support

_support.ensure_path()

import json

import pytest

from experiments.character_capsule.benchmark import BenchmarkConfig, run_benchmark
from experiments.character_capsule.continuity import VerificationState
from experiments.character_capsule.evidence import EvidenceBundle, verify_bundle

REQUIRED_SCENARIOS = {
    "same_character_same_camera",
    "same_character_different_angle",
    "same_character_different_lighting",
    "same_character_different_expression",
    "same_character_different_outfit",
    "same_character_different_environment",
    "same_character_different_pose",
    "same_character_motion_sequence",
    "different_character_similar_appearance",
    "character_canon_conflict",
}


@pytest.fixture(scope="module")
def results(tmp_path_factory: pytest.TempPathFactory):
    work = tmp_path_factory.mktemp("capsule-bench")
    return run_benchmark(BenchmarkConfig(work_dir=work)), work


def test_every_required_scenario_is_present_and_measured(results):
    payload, _ = results
    names = {entry["scenario"] for entry in payload["scenarios"]}
    assert REQUIRED_SCENARIOS <= names
    assert len(payload["scenarios"]) == 12


def test_every_verdict_belongs_to_the_closed_vocabulary(results):
    payload, _ = results
    vocabulary = {state.value for state in VerificationState}
    for entry in payload["scenarios"]:
        assert entry["actual"] in vocabulary


def test_no_metric_is_invented(results):
    payload, _ = results
    for entry in payload["holdout_metrics"].values():
        point = entry.get("at_accept_boundary")
        if point is None:
            assert entry.get("status") == "NOT_MEASURED"
            continue
        for key in ("far", "frr", "tar"):
            assert 0.0 <= float(point[key]) <= 1.0
        assert 0.0 <= float(entry["eer"]["eer"]) <= 1.0
    for value in payload["not_measured"].values():
        assert isinstance(value, str) and value


def test_environment_claims_are_measured_not_assumed(results):
    payload, _ = results
    environment = payload["environment"]
    assert environment["python"]
    assert environment["peak_rss_mb"] > 0
    assert "NOT_MEASURED" in environment["gpu"]


def test_calibration_records_its_own_dataset_and_pruning(results):
    payload, _ = results
    assert payload["calibration"]["digest"].startswith("sha256:")
    pruning = payload["calibration"]["pruned_probes"]
    assert isinstance(pruning["quality"], list)
    assert isinstance(pruning["canon_marker"], list)
    assert pruning["policy"]


def test_corpus_digest_is_reproducible(tmp_path):
    first = run_benchmark(BenchmarkConfig(work_dir=tmp_path / "run-a"))
    second = run_benchmark(BenchmarkConfig(work_dir=tmp_path / "run-b"))
    assert first["corpus"]["digest"] == second["corpus"]["digest"]
    assert [s["actual"] for s in first["scenarios"]] == [s["actual"] for s in second["scenarios"]]


def _run_dir(payload, work):
    """The one directory this run owns; a run publishes into ``results/<run_id>``."""
    run_dir = work / "results" / payload["run"]["run_id"]
    assert run_dir.is_dir()
    return run_dir


def test_evidence_bundle_is_written_and_verifies(results):
    payload, work = results
    run_dir = _run_dir(payload, work)
    bundle_path = run_dir / "evidence_bundle.json"
    assert bundle_path.is_file()
    bundle = EvidenceBundle.from_dict(json.loads(bundle_path.read_text(encoding="utf-8")))
    assert bundle.lineage
    # Every recorded artifact must be supplied: a partial check is INCOMPLETE, so
    # a verifier that silently skips missing files would hide a completeness bug.
    files = {record.name: run_dir / record.name for record in bundle.records}
    assert all(path.is_file() for path in files.values())
    verification = verify_bundle(bundle, files=files)
    assert verification.checks["hard_binding_matches_records"] is True
    assert verification.checks["files_match"] is True
    assert verification.checks["all_artifacts_supplied"] is True
    # always unsigned in CI: there is no key in the repository, by design
    assert verification.status == "UNSIGNED"
    assert payload["evidence"]["verification"]["status"] == "UNSIGNED"
    assert payload["evidence"]["signing"]["status"] == "NOT_CONFIGURED"


def test_evidence_does_not_bind_its_own_results_document(results):
    """The bundle cannot contain the digest of the file that contains its digest."""
    payload, work = results
    run_dir = _run_dir(payload, work)
    bundle = EvidenceBundle.from_dict(
        json.loads((run_dir / "evidence_bundle.json").read_text(encoding="utf-8"))
    )
    names = {record.name for record in bundle.records}
    assert "benchmark_results.json" not in names
    assertions = payload["evidence"]["c2pa_manifest_shape"]["assertions"]
    scopes = [item["binding_scope"] for item in assertions if "binding_scope" in item]
    assert len(scopes) == 1
    scope = scopes[0]
    assert scope["excluded"]
    assert "benchmark_results.json" in scope["excluded"]
    assert bundle.assertions["media"]["frames"] > 0


def test_a_run_publishes_atomically_under_its_own_id(tmp_path):
    """A second run with the same id must not overwrite or merge with the first."""
    first = run_benchmark(BenchmarkConfig(work_dir=tmp_path, run_id="run-fixed-id"))
    run_dir = tmp_path / "results" / "run-fixed-id"
    assert run_dir.is_dir()
    assert not (tmp_path / "results" / ".run-fixed-id.staging").exists()
    assert first["run"]["atomic_publish"] is True
    assert first["run"]["staging_dir"].endswith(".run-fixed-id.staging")
    with pytest.raises(FileExistsError):
        run_benchmark(BenchmarkConfig(work_dir=tmp_path, run_id="run-fixed-id"))
    # the first run's bytes are untouched by the refused second run
    assert (run_dir / "benchmark_results.json").is_file()


def test_reproducible_run_has_the_same_corpus_and_results_digest(tmp_path):
    """With a pinned run id the two artefacts are byte-identical apart from timing."""
    a = run_benchmark(BenchmarkConfig(work_dir=tmp_path / "a", run_id="pinned"))
    b = run_benchmark(BenchmarkConfig(work_dir=tmp_path / "b", run_id="pinned"))
    assert a["corpus"]["digest"] == b["corpus"]["digest"]
    assert a["results_digest"] == b["results_digest"]
    # the exclusions are published, not hidden
    assert a["results_digest_scope"]["excluded"]
    assert a["results_digest"] != b["corpus"]["digest"]


def test_results_document_is_self_describing(results):
    payload, work = results
    assert payload["schema"] == "character-capsule-benchmark/1"
    run_dir = _run_dir(payload, work)
    for name in (
        "BENCHMARK.md",
        "calibration.json",
        "continuity_reports.json",
        "corpus_manifest.json",
        "evidence_bundle.json",
        "benchmark_results.json",
    ):
        assert (run_dir / name).is_file(), name
    # exactly one run directory exists: a rerun inside the same work dir would
    # otherwise overwrite a previous run's evidence
    runs = [path for path in (work / "results").iterdir() if not path.name.startswith(".")]
    assert len(runs) == 1
