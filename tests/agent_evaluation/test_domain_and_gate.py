"""Deterministic contracts, strict gate, and report-shape tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from nexus_ai_agent.evaluation.corpus import (
    ADVERSARIAL_CASES,
    AGENT_FOUNDATION_SUITE,
    FAILURE_CASES,
    GOLDEN_CASES,
)
from nexus_ai_agent.evaluation.domain import (
    EvaluationDimension,
    EvaluationInput,
    EvaluationReport,
    ExpectedBehavior,
    ExpectedDecision,
    GateVerdict,
    IdentityRelation,
    ObservedOutcome,
    Verdict,
    is_subject_sha,
)
from nexus_ai_agent.evaluation.runner import run_suite

SUBJECT_SHA = "e5b326b2eaf691a638d030ad57acf1ce60016ef0"
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def test_corpus_versions_and_case_identities_are_stable() -> None:
    assert AGENT_FOUNDATION_SUITE.version == "1.0.0"
    assert (len(GOLDEN_CASES), len(ADVERSARIAL_CASES), len(FAILURE_CASES)) == (19, 17, 10)
    case_ids = tuple(case.case_id for case in AGENT_FOUNDATION_SUITE.cases)
    assert len(case_ids) == len(set(case_ids)) == 46
    assert all(case_id.startswith("case-") for case_id in case_ids)
    assert len(AGENT_FOUNDATION_SUITE.suite_digest) == 64
    assert len({case.case_key for case in AGENT_FOUNDATION_SUITE.cases}) == 46


def test_case_and_report_identity_exclude_timestamps_and_process_state() -> None:
    case = GOLDEN_CASES[0]
    run_a = run_suite(AGENT_FOUNDATION_SUITE, subject_sha=SUBJECT_SHA)
    run_b = run_suite(AGENT_FOUNDATION_SUITE, subject_sha=SUBJECT_SHA)
    assert case.case_id == GOLDEN_CASES[0].case_id
    assert run_a.run_id == run_b.run_id
    assert run_a.report().to_json() == run_b.report().to_json()
    assert "timestamp" not in run_a.report().to_json().lower()


def test_fresh_process_case_ids_report_bytes_and_gate_verdict_match() -> None:
    code = """
import json
from nexus_ai_agent.evaluation.corpus import AGENT_FOUNDATION_SUITE
from nexus_ai_agent.evaluation.runner import run_suite
run = run_suite(AGENT_FOUNDATION_SUITE, subject_sha='e5b326b2eaf691a638d030ad57acf1ce60016ef0')
print(json.dumps({
    'case_ids': [case.case_id for case in AGENT_FOUNDATION_SUITE.cases],
    'suite_digest': AGENT_FOUNDATION_SUITE.suite_digest,
    'run_id': run.run_id,
    'report': run.report().to_json(),
    'gate': run.gate_verdict.value,
}, sort_keys=True, separators=(',', ':')))
"""
    environment = dict(os.environ)
    existing = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = str(REPOSITORY_ROOT / "src") + (
        os.pathsep + existing if existing else ""
    )
    first = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPOSITORY_ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    second = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPOSITORY_ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    assert first.stdout == second.stdout
    payload = json.loads(first.stdout)
    assert payload["gate"] == "FAIL"
    assert payload["case_ids"] == [case.case_id for case in AGENT_FOUNDATION_SUITE.cases]
    assert payload["report"]


def test_gate_without_explicit_adapter_is_blocked_and_fails_closed() -> None:
    run = run_suite(AGENT_FOUNDATION_SUITE, subject_sha=SUBJECT_SHA, adapter=None)
    report = run.report()
    payload = json.loads(report.to_json())

    assert run.gate_verdict is GateVerdict.FAIL
    assert report.verdict is GateVerdict.FAIL
    assert run.adapter_id == "unregistered"
    assert all(case.status is Verdict.BLOCKED for case in run.results)
    assert len(report.cases) == 46
    assert payload["subject"]["sha"] == SUBJECT_SHA
    assert payload["suite"] == {"id": "agent-foundation-v1", "version": "1.0.0"}
    assert payload["verdict"] == "FAIL"
    assert payload["dimensions"][EvaluationDimension.E2E_READINESS.value] == "BLOCKED"
    assert len(payload["e2e_readiness"]["stages"]) == 10
    assert all(stage["status"] == "BLOCKED" for stage in payload["e2e_readiness"]["stages"])
    assert payload["blocked_conditions"][0]["code"] == "E2E_ADAPTER_UNAVAILABLE"
    assert all(
        payload["dimensions"][dimension.value] == "BLOCKED"
        for dimension in EvaluationDimension
        if dimension is not EvaluationDimension.E2E_READINESS
    )
    assert payload["hard_failures"] == []
    assert payload["evidence_refs"] == []
    assert all(case["observed"] is None for case in payload["cases"])


def test_report_has_stable_machine_contract_and_oracle_versions() -> None:
    report: EvaluationReport = run_suite(
        AGENT_FOUNDATION_SUITE,
        subject_sha=SUBJECT_SHA,
    ).report()
    data = report.to_data()
    assert data["schema_version"] == "1.0"
    assert len(data["oracle_versions"]) == 15
    assert set(data["oracle_versions"].values()) == {"1.0.0"}
    assert data["case_ids"] == [case.case_id for case in AGENT_FOUNDATION_SUITE.cases]
    assert set(data["dimensions"]) == {dimension.value for dimension in EvaluationDimension}
    assert data["blocked_conditions"]
    assert data["failed_invariants"] == []
    assert report.to_json() == report.to_json()
    assert json.loads(report.to_json()) == data


def test_gate_rejects_invalid_subject_sha() -> None:
    with pytest.raises(ValueError, match="subject_sha"):
        run_suite(AGENT_FOUNDATION_SUITE, subject_sha="../main")
    assert not is_subject_sha("../main")
    assert is_subject_sha(SUBJECT_SHA)


def test_expected_contracts_preserve_distinct_failure_outcomes() -> None:
    by_key = {case.case_key: case for case in AGENT_FOUNDATION_SUITE.cases}
    cases = (
        "g13-known-tool-unavailable-is-distinct",
        "g14-policy-blocked-is-distinct",
        "f01-command-failure-is-not-verification-failure",
        "f02-timeout-is-causally-distinct",
    )
    observed = {
        key: (
            by_key[key].expected.expected_outcome,
            by_key[key].expected.failure_code,
            by_key[key].expected.expected_tool_state,
        )
        for key in cases
    }
    assert observed[cases[0]][0] is ObservedOutcome.FAILED
    assert observed[cases[0]][1].value == "TOOL_UNAVAILABLE"
    assert observed[cases[1]][0] is ObservedOutcome.REFUSED
    assert observed[cases[1]][1].value == "POLICY_DENIED"
    assert observed[cases[2]][1].value == "EXECUTION_FAILED"
    assert observed[cases[3]][1].value == "TIMEOUT"
    assert observed[cases[2]][2] is not observed[cases[0]][2]


def test_domain_rejects_invalid_identity_contract() -> None:
    with pytest.raises(ValueError, match="localized semantic edits"):
        ExpectedBehavior(
            decision=ExpectedDecision.ACCEPT,
            intent_key="edit",
            identity_relation=IdentityRelation.LOCALIZED_SEMANTIC_EDIT,
        )


def test_evaluation_input_does_not_turn_payload_actor_claim_into_trusted_identity() -> None:
    item = EvaluationInput(
        request_text="Add a marker.",
        request_id="request-1",
        project_id="project-1",
        revision_id="revision-1",
        trusted_actor_id="trusted-user",
        untrusted_actor_claim="admin",
    )
    assert item.trusted_actor_id == "trusted-user"
    assert item.untrusted_actor_claim == "admin"
