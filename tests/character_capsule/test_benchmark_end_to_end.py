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


def test_evidence_bundle_is_written_and_verifies(results):
    payload, work = results
    bundle_path = work / "results" / "evidence_bundle.json"
    assert bundle_path.is_file()
    bundle = EvidenceBundle.from_dict(json.loads(bundle_path.read_text(encoding="utf-8")))
    assert bundle.lineage
    files = {
        record.name: work / "results" / record.name
        for record in bundle.records
        if (work / "results" / record.name).is_file()
    }
    verification = verify_bundle(bundle, files=files)
    assert verification.checks["hard_binding_matches_records"] is True
    assert verification.checks["files_match"] is True
    # always unsigned in CI: there is no key in the repository, by design
    assert verification.status in {"UNSIGNED", "VALID"}


def test_results_document_is_self_describing(results):
    payload, work = results
    assert payload["schema"] == "character-capsule-benchmark/1"
    assert (work / "results" / "BENCHMARK.md").is_file()
    assert (work / "results" / "calibration.json").is_file()
    assert (work / "results" / "continuity_reports.json").is_file()
