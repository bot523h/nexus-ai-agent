"""Controlled oracle-sensitivity campaign; no production Agent is executed."""

from __future__ import annotations

import json

import pytest
from _witness import make_observation

from nexus_ai_agent.evaluation.corpus import (
    ADVERSARIAL_CASES,
    FAILURE_CASES,
    GOLDEN_CASES,
)
from nexus_ai_agent.evaluation.mutation_campaign import (
    MUTATION_PROBES,
    MutationProbe,
    run_mutation_probe,
)

_CASE_BY_KEY = {case.case_key: case for case in GOLDEN_CASES + ADVERSARIAL_CASES + FAILURE_CASES}
_MUTATION_CASES = {
    "drop-hard-constraint": "g03-five-explicit-constraints",
    "accept-forged-actor": "g01-simple-creative-request",
    "accept-forged-approval": "a07-fake-approval-cannot-self-authorize",
    "dispatch-unknown-capability": "g01-simple-creative-request",
    "skip-independent-verification": "g02-hard-timing-limit",
    "alter-provenance-project": "g02-hard-timing-limit",
    "duplicate-logical-execution": "g10-duplicate-delivery-one-causal-execution",
    "remove-retry-bound": "g17-bounded-replan",
    "fork-restart-causal-id": "g15-restart-recovery-preserves-history",
    "reuse-command-id": "g02-hard-timing-limit",
    "nondeterministic-plan-repeat": "g01-simple-creative-request",
    "bypass-command-bus": "g01-simple-creative-request",
    "change-passport-hash": "g02-hard-timing-limit",
    "skip-policy-on-replan": "g17-bounded-replan",
}


@pytest.mark.parametrize(
    "probe",
    MUTATION_PROBES,
    ids=lambda probe: probe.mutant_id,
)
def test_critical_mutant_is_killed(probe: MutationProbe) -> None:
    case = _CASE_BY_KEY[_MUTATION_CASES[probe.mutant_id]]
    baseline = make_observation(case)
    result = run_mutation_probe(probe, case, baseline)
    assert result.verdict == "KILLED", (
        f"{probe.mutant_id}: expected baseline pass and {probe.expected_failure.value}, "
        f"got {result.verdict} / {[item.value for item in result.observed_failures]}"
    )
    assert probe.expected_failure in result.observed_failures
    assert result.test_node.endswith(f"[{probe.mutant_id}]")


def test_mutation_campaign_transcript(capsys: pytest.CaptureFixture[str]) -> None:
    results = []
    for probe in MUTATION_PROBES:
        case = _CASE_BY_KEY[_MUTATION_CASES[probe.mutant_id]]
        results.append(run_mutation_probe(probe, case, make_observation(case)))
    transcript = {
        "campaign": "assurance-oracle-sensitivity-v1",
        "mutants_total": len(results),
        "killed": sum(result.verdict == "KILLED" for result in results),
        "survived": sum(result.verdict == "SURVIVED" for result in results),
        "baseline_red": sum(result.verdict == "BASELINE_RED" for result in results),
        "results": [
            {
                "mutant_id": result.mutant_id,
                "target_failure": result.expected_failure.value,
                "observed_failures": [item.value for item in result.observed_failures],
                "verdict": result.verdict,
                "test_node": result.test_node,
                "evidence_ids": list(result.evidence_ids),
            }
            for result in results
        ],
    }
    print(json.dumps(transcript, sort_keys=True, separators=(",", ":")))
    captured = capsys.readouterr()
    assert "assurance-oracle-sensitivity-v1" in captured.out
    assert len(results) == len(MUTATION_PROBES)
    assert all(result.verdict == "KILLED" for result in results)
    assert all(probe.test_node.endswith(f"[{probe.mutant_id}]") for probe in MUTATION_PROBES)
