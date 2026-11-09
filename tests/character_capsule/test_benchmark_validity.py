"""Phase-8 regression suite: the benchmark measures what it claims (F23-F27).

A benchmark that fits its thresholds on the data it then reports is not a
benchmark, and a scenario that compares an array with itself is not a measurement.
These tests run the real benchmark once and then interrogate the *published*
artefacts - the same files a reviewer would open.
"""

# ruff: noqa: E402 - _support.ensure_path() must run before these imports
from __future__ import annotations

import _support

_support.ensure_path()

import json

import numpy as np
import pytest

from experiments.character_capsule.benchmark import (
    BenchmarkConfig,
    build_calibration_from_corpus,
    run_benchmark,
)
from experiments.character_capsule.continuity import (
    EVIDENCE_DIGEST_EXCLUSIONS,
    report_digest,
    verify_report_evidence,
)
from experiments.character_capsule.dataset import (
    VOICE_SESSIONS,
    build_corpus,
    voice_samples,
    voice_signature,
)


@pytest.fixture(scope="module")
def published(tmp_path_factory: pytest.TempPathFactory):
    work = tmp_path_factory.mktemp("validity-bench")
    payload = run_benchmark(BenchmarkConfig(work_dir=work))
    run_dir = work / "results" / payload["run"]["run_id"]
    return payload, run_dir


def _load(run_dir, name):
    return json.loads((run_dir / name).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# F23 - calibration and holdout do not overlap
# ---------------------------------------------------------------------------


def test_f23_the_holdout_character_is_not_in_the_fitted_split(published):
    payload, run_dir = published
    calibration = _load(run_dir, "calibration.json")
    fitted = calibration["split"]
    assert fitted["calibration"]
    assert fitted["holdout"]
    assert set(fitted["calibration"]) & set(fitted["holdout"]) == set()
    assert fitted["overlap"] == []
    # the corpus-wide split report agrees with the artefact's own record
    assert set(payload["split"]["holdout"]) == set(fitted["holdout"])


def test_f23b_the_fitted_statistics_do_not_see_the_holdout_character(published):
    """A gallery z-scored with the holdout's own statistics leaks it into the fit."""
    _, run_dir = published
    calibration = _load(run_dir, "calibration.json")
    payload = _load(run_dir, "benchmark_results.json")
    holdout_ids = set(payload["split"]["holdout"])
    fitted_ids = set(calibration["metrics"]["face"]["calibration_split"].get("character_ids", []))
    if fitted_ids:  # only asserted when the artefact records them
        assert fitted_ids & holdout_ids == set()


def test_f23c_the_holdout_metrics_are_reported_even_when_they_are_bad(published):
    """Face FAR is 0.6 on this corpus. Hiding it would be the failure, not the number."""
    payload, _ = published
    metrics = payload["holdout_metrics"]
    assert "face" in metrics
    point = metrics["face"]["at_accept_boundary"]
    assert 0.0 <= point["far"] <= 1.0
    assert 0.0 <= point["frr"] <= 1.0
    assert point["far"] + point["frr"] > 0.0 or point["tar"] == 1.0


def test_f23d_a_dimension_that_cannot_separate_is_not_measured(published):
    """The look-alike pair sits 3 Hz apart: voice must not be given a fake band."""
    _, run_dir = published
    calibration = _load(run_dir, "calibration.json")
    payload = _load(run_dir, "benchmark_results.json")
    voice = calibration["metrics"].get("voice", {})
    if voice.get("status") == "NOT_MEASURED":
        assert voice["reason"] == "CALIBRATION_INSEPARABLE"
        assert "voice" not in calibration["thresholds"]
        # no threshold means no holdout operating point to publish: the honest
        # report is the absence plus the reason, not a made-up FAR/FRR
        assert "voice" not in payload["holdout_metrics"]
        assert voice["eer"] > 0.25
    else:  # if a future corpus separates them, the band must be justified
        assert voice["band_fit"]["mode"] in {"QUANTILE", "EER_FALLBACK"}


def test_f23e_a_band_fit_is_always_labelled(published):
    _, run_dir = published
    calibration = _load(run_dir, "calibration.json")
    for dimension, entry in calibration["metrics"].items():
        if entry.get("status") == "NOT_MEASURED":
            assert entry["reason"]
            continue
        assert entry["band_fit"]["mode"] in {"QUANTILE", "EER_FALLBACK"}
        threshold = calibration["thresholds"][dimension]
        assert threshold["accept"] < threshold["reject"]


def test_f23f_the_eer_fallback_is_only_used_for_a_defensible_split(published):
    """The fallback is a declared policy with a stated ceiling, not a tuned number."""
    from experiments.character_capsule.continuity import MAX_FALLBACK_EER

    _, run_dir = published
    calibration = _load(run_dir, "calibration.json")
    for entry in calibration["metrics"].values():
        fit = entry.get("band_fit")
        if fit and fit["mode"] == "EER_FALLBACK":
            assert fit["eer"] <= MAX_FALLBACK_EER
            assert fit["eer_threshold"] > 0


# ---------------------------------------------------------------------------
# F24 - a genuine pair is not the same array twice
# ---------------------------------------------------------------------------


def test_f24_voice_sessions_differ_for_the_same_speaker():
    for session in VOICE_SESSIONS[1:]:
        first = voice_samples("NAGAR-CHAR-000001", session="a")
        other = voice_samples("NAGAR-CHAR-000001", session=session)
        assert first.shape == other.shape
        assert not np.array_equal(first, other)
    assert voice_signature("NAGAR-CHAR-000001", session="b").f0_hz != (
        voice_signature("NAGAR-CHAR-000001", session="a").f0_hz
    )


def test_f24b_the_voice_scenario_compares_two_recordings(published):
    """The scenario used to pass the reference array to both sides (distance 0.0)."""
    _, run_dir = published
    reports = _load(run_dir, "continuity_reports.json")
    payload = _load(run_dir, "benchmark_results.json")
    voice_reports = [
        report
        for report, scenario in zip(reports, payload["scenarios"], strict=True)
        if scenario["scenario"] == "extra_voice_identity"
    ]
    assert len(voice_reports) == 1
    voice = next(
        (dim for dim in voice_reports[0]["dimensions"] if dim["dimension"] == "voice"), None
    )
    assert voice is not None
    if voice["measured"]:
        assert voice["distance"] > 0.0
    else:
        assert voice["band"] == "NOT_MEASURED"


# ---------------------------------------------------------------------------
# F25 - published reports satisfy their own digests
# ---------------------------------------------------------------------------


def test_f25_every_published_report_digest_recomputes(published):
    _, run_dir = published
    reports = _load(run_dir, "continuity_reports.json")
    assert reports
    for report in reports:
        assert report["evidence_digest"]
        assert verify_report_evidence(report) is True
        assert set(EVIDENCE_DIGEST_EXCLUSIONS) <= set(report)


def test_f25b_editing_a_published_report_breaks_its_digest(published):
    _, run_dir = published
    report = _load(run_dir, "continuity_reports.json")[0]
    edited = json.loads(json.dumps(report))
    edited["verdict"] = "VERIFIED"
    if report["verdict"] == "VERIFIED":
        edited["verdict"] = "IDENTITY_DRIFT"
    assert verify_report_evidence(edited) is False
    assert (
        report_digest(edited, calibration_digest=edited["calibration_digest"])
        != (report["evidence_digest"])
    )


# ---------------------------------------------------------------------------
# F27 - one run, one identity, one write
# ---------------------------------------------------------------------------


def test_f27_each_run_gets_its_own_bundle_id(published):
    payload, _ = published
    assert payload["run"]["run_id"] in payload["evidence"]["bundle_id"]
    assert payload["run"]["run_id"] in payload["evidence"]["path"]
    first = run_benchmark(BenchmarkConfig(work_dir=published[1].parent.parent / "other"))
    assert first["evidence"]["bundle_id"] != payload["evidence"]["bundle_id"]
    assert first["run"]["run_id"] != payload["run"]["run_id"]


def test_f27b_the_results_document_is_written_once_and_is_self_describing(published):
    payload, run_dir = published
    on_disk = _load(run_dir, "benchmark_results.json")
    assert on_disk["results_digest"] == payload["results_digest"]
    assert on_disk["results_digest_scope"]["excluded"]
    assert on_disk["run"]["atomic_publish"] is True
    excluded = on_disk["results_digest_scope"]["excluded"]
    assert "results_digest" in excluded and "evidence" in excluded
    assert "scenarios[].processing_ms" in excluded


def test_f27c_the_environment_reports_measured_rss_or_says_it_cannot(published):
    payload, _ = published
    rss = payload["environment"]["peak_rss_mb"]
    assert rss == "NOT_MEASURED: resource.getrusage unavailable on this platform" or isinstance(
        rss, float | int
    )
    assert payload["environment"]["gpu"].startswith("NOT_MEASURED")


# ---------------------------------------------------------------------------
# calibration helpers used above must exist and be callable
# ---------------------------------------------------------------------------


def test_build_calibration_from_corpus_is_deterministic(tmp_path):
    corpus = build_corpus(tmp_path / "corpus", write=True)
    first = build_calibration_from_corpus(corpus, calibration_ids=("NAGAR-CHAR-000001",))
    second = build_calibration_from_corpus(corpus, calibration_ids=("NAGAR-CHAR-000001",))
    assert first.digest == second.digest
    assert first.thresholds.keys() == second.thresholds.keys()
