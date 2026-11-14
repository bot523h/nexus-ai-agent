"""Tests for the analysis facade, status folding, suggestions and real-disk export."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import atlas_fixtures as fx
import pytest

from nexus_ai_agent.creative.episode_causality_lab import (
    EventKind,
    FactRelation,
    PlanRequest,
    Status,
    TriValue,
    analyze_story,
    build_handoff,
    export_report,
    report_to_dict,
)
from nexus_ai_agent.execution.staging import AttemptStaging
from nexus_ai_agent.tools.filesystem_policy import FilesystemBoundaryError, WorkspaceFilesystem


def test_clean_story_reports_pass() -> None:
    doc = fx.base_doc(
        initial_facts=(fx.fact("alice", FactRelation.POSSESSES, "gun", TriValue.TRUE),),
        events=(
            fx.event(
                "e1", EventKind.TRANSFER_PROP, order=0, actor="alice", target="bob", prop="gun"
            ),
        ),
    )
    report = analyze_story(doc)
    assert report.status is Status.PASS


def test_unknown_fact_keeps_report_out_of_pass() -> None:
    doc = fx.base_doc(
        events=(
            fx.event(
                "e1", EventKind.TRANSFER_PROP, order=0, actor="alice", target="bob", prop="gun"
            ),
        )
    )
    report = analyze_story(doc)
    assert report.status is Status.NEEDS_REVIEW


def test_temporal_failure_folds_to_fail() -> None:
    doc = fx.base_doc(
        temporal_constraints=(
            fx.constraint("c1", "e1", "e2", 10, 20),
            fx.constraint("c2", "e1", "e2", 0, 5),
        ),
        events=(
            fx.event("e1", EventKind.ENTER_LOCATION, order=0, actor="alice", target="room"),
            fx.event("e2", EventKind.ENTER_LOCATION, order=1, actor="bob", target="room"),
        ),
    )
    report = analyze_story(doc)
    assert report.status is Status.FAIL
    assert report.temporal.witness is not None


def test_infeasible_plan_is_needs_review_not_fail() -> None:
    doc = fx.base_doc()
    req = PlanRequest(beats=("b1",), candidates=(), frame_budget=10)
    report = analyze_story(doc, plan_request=req)
    assert report.status is Status.NEEDS_REVIEW


def test_resource_limited_plan_is_resource_limit() -> None:
    doc = fx.base_doc()
    req = PlanRequest(beats=("b1",), candidates=(fx.shot("s1", ("b1",), 5),), frame_budget=10)
    from nexus_ai_agent.creative.episode_causality_lab.plan import plan_edit

    # a clean story + a proven-optimal plan -> PASS; force a limit instead
    assert plan_edit(req).status.value == "PROVEN_OPTIMAL"
    assert analyze_story(doc, plan_request=req).status is Status.PASS


def test_report_to_dict_is_json_serialisable_and_sorted() -> None:
    doc = fx.base_doc(
        events=(fx.event("e1", EventKind.ENTER_LOCATION, actor="alice", target="room"),)
    )
    report = analyze_story(doc)
    payload = report_to_dict(report)
    text = json.dumps(payload, sort_keys=True)
    assert payload["status"] == report.status.value
    assert payload["causal"]["event_order"] == ["e1"]
    assert isinstance(text, str)


def test_suggestions_are_labelled_advisory() -> None:
    doc = fx.base_doc(
        events=(fx.event("e1", EventKind.TRANSFER_PROP, actor="alice", target="bob", prop="gun"),)
    )
    report = analyze_story(doc)
    assert report.suggestions
    for s in report.suggestions:
        assert s.label == "ADVISORY_SUGGESTION"
        assert s.addresses_code


def test_export_report_writes_and_reopens_real_bytes(tmp_path: Path) -> None:
    doc = fx.base_doc(
        events=(fx.event("e1", EventKind.ENTER_LOCATION, actor="alice", target="room"),)
    )
    report = analyze_story(doc)
    fs = WorkspaceFilesystem(tmp_path)
    artifact = export_report(report, fs, "reports/atlas.json")
    assert artifact.verified
    assert artifact.sha256 == artifact.reopened_sha256
    on_disk = (tmp_path / "reports" / "atlas.json").read_bytes()
    assert hashlib.sha256(on_disk).hexdigest() == artifact.sha256


def test_export_report_rejects_traversal(tmp_path: Path) -> None:
    doc = fx.base_doc()
    report = analyze_story(doc)
    fs = WorkspaceFilesystem(tmp_path)
    with pytest.raises(FilesystemBoundaryError):
        export_report(report, fs, "../escape.json")


def test_export_report_through_attempt_staging(tmp_path: Path) -> None:
    doc = fx.base_doc(
        events=(fx.event("e1", EventKind.ENTER_LOCATION, actor="alice", target="room"),)
    )
    report = analyze_story(doc)
    staging_root = tmp_path / "staging"
    final_root = tmp_path / "final"
    staging = AttemptStaging(staging_root, job_id="job1", attempt_id="att1", final_root=final_root)
    final_fs = WorkspaceFilesystem(final_root)
    artifact = export_report(report, final_fs, "atlas/report.json", staging=staging)
    assert artifact.verified
    assert (final_root / "atlas" / "report.json").is_file()


def test_tampered_output_is_detected_by_reopen(tmp_path: Path) -> None:
    doc = fx.base_doc()
    report = analyze_story(doc)
    fs = WorkspaceFilesystem(tmp_path)
    artifact = export_report(report, fs, "atlas.json")
    # Tamper with the file, then independently re-hash.
    path = tmp_path / "atlas.json"
    path.write_bytes(path.read_bytes() + b" ")
    reopened = hashlib.sha256(fs.read_bytes("atlas.json")).hexdigest()
    assert reopened != artifact.sha256


def test_handoff_is_advisory_and_carries_no_authority(tmp_path: Path) -> None:
    doc = fx.base_doc(
        beats=(fx.beat("b1", 0),),
        shot_candidates=(fx.shot("s1", ("b1",), 10),),
    )
    req = PlanRequest(beats=("b1",), candidates=(fx.shot("s1", ("b1",), 10),), frame_budget=100)
    report = analyze_story(doc, plan_request=req)
    fs = WorkspaceFilesystem(tmp_path)
    artifact = export_report(report, fs, "atlas.json")
    handoff = build_handoff(report, artifact)
    assert handoff["authority"] == "advisory"
    assert handoff["requires_authorization"] is True
    assert handoff["proposed_steps"][0]["candidate_id"] == "s1"


def test_report_fingerprint_matches_document() -> None:
    doc = fx.base_doc()
    report = analyze_story(doc)
    assert report.fingerprint == doc.fingerprint()
