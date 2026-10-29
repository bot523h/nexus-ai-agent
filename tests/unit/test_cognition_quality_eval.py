"""Contract tests for the cognition quality evaluation harness (task-269).

The parameterised boundary suites already prove individual properties of
``parse_proposal`` / ``LocalCognition`` / ``NullCognition``.  These tests cover
what is *new* and would otherwise be unverified:

* the harness is one **complete, frozen fixture set** (scenarios A..J) whose
  expectations match the live contract — a silent contract change or a dropped
  fixture turns this file red;
* every reported counter is **recomputed here from the case results**, so a
  hard-coded number cannot survive;
* acceptance is separated from execution and verification, and the separating
  invariant is proven **non-tautological** by flipping it;
* determinism is behavioural (fingerprint) while latency is reported apart;
* the default run is network-free and key-free (sockets are blocked at runtime
  and the module's import allowlist is checked statically);
* the report is **non-disclosing** — prompt/intent text, model prose and
  provider exception text never reach stdout, stderr or the JSON payload;
* invalid CLI input is refused with exit 2 and no traceback.

The script is loaded by path (``scripts/`` is deliberately not a package — see
``tests/architecture/test_scripts_import_boundary.py``), the same pattern as
``tests/unit/test_pr_convergence.py``.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "cognition_quality_eval.py"

_spec = importlib.util.spec_from_file_location("cognition_quality_eval", SCRIPT)
assert _spec and _spec.loader
harness = importlib.util.module_from_spec(_spec)
sys.modules["cognition_quality_eval"] = harness
_spec.loader.exec_module(harness)

REQUIRED_SCENARIOS = tuple("ABCDEFGHIJ")

#: The report contract.  A new field is fine, but it must be added here too —
#: that is what stops "proposal accepted" from quietly becoming "verified".
REPORT_KEYS = {
    "evaluation_version",
    "execution_mode",
    "network_used",
    "real_model_used",
    "repeats",
    "counts_from_repeat",
    "cases_total",
    "cases_passed",
    "cases_failed",
    "valid_proposals_accepted",
    "refusals_observed",
    "refusal_reasons",
    "synthetic_boundary_probe_cases",
    "repeatability",
    "latency_summary",
    "verification_status",
    "execution_dispatched",
    "execution_modules_loaded_during_run",
    "cases",
}

CASE_KEYS = {"case_id", "scenario", "expected", "observed", "passed", "probe"}

#: Text that must never be disclosed by the harness.
SECRET_MARKERS = (
    harness.BOUNDARY_PROBE_INTENT,
    harness.BOUNDARY_PROBE_PROSE,
    "must-never-be-logged",
    "You are a bounded reasoning component",
    "deny_by_contract",
    "clip_eval_1",
)


@pytest.fixture(scope="module")
def report() -> dict[str, Any]:
    return harness.evaluate(repeats=1)


def _cases(report: dict[str, Any]) -> list[dict[str, Any]]:
    return list(report["cases"])


def _by_id(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {case["case_id"]: case for case in report["cases"]}


def _scenario_cases(report: dict[str, Any], scenario: str) -> list[dict[str, Any]]:
    return [case for case in report["cases"] if case["scenario"] == scenario]


# -- 1. completeness of the fixture set -----------------------------------


def test_every_mandatory_scenario_is_present_and_passes(report: dict[str, Any]) -> None:
    scenarios = {case["scenario"] for case in report["cases"]}
    assert set(REQUIRED_SCENARIOS) == scenarios
    for scenario in REQUIRED_SCENARIOS:
        assert _scenario_cases(report, scenario), f"scenario {scenario} has no fixture"
    assert report["cases_failed"] == 0
    assert report["cases_passed"] == report["cases_total"]


def test_authority_probe_cover_the_documented_field_classes(report: dict[str, Any]) -> None:
    probed = {
        case["case_id"].removeprefix("E-authority-field-")
        for case in report["cases"]
        if case["case_id"].startswith("E-authority-field-")
    }
    assert probed == set(harness.AUTHORITY_PROBE_FIELDS)
    for field in ("actor", "permissions", "authorizer", "confirmed", "shell", "command", "sudo"):
        assert field in probed


# -- 2..6. behavioural expectations ---------------------------------------


def test_valid_proposals_are_accepted_and_counted(report: dict[str, Any]) -> None:
    for case in _scenario_cases(report, "A"):
        assert case["observed"] == "proposal", case["case_id"]
        assert case["passed"] is True
    accepted = [case for case in report["cases"] if case["observed"] == "proposal"]
    assert report["valid_proposals_accepted"] == len(accepted)
    assert report["valid_proposals_accepted"] >= 4


def test_malformed_output_is_rejected_not_coerced(report: dict[str, Any]) -> None:
    malformed = [case for case in _scenario_cases(report, "B")]
    assert len(malformed) >= 5
    for case in malformed:
        assert case["observed"] == "malformed", case["case_id"]
        assert case["passed"] is True


def test_authority_fields_never_become_a_proposal(report: dict[str, Any]) -> None:
    probe_cases = [
        case for case in _scenario_cases(report, "E") if case["case_id"].startswith("E-authority")
    ]
    assert len(probe_cases) == len(harness.AUTHORITY_PROBE_FIELDS)
    for case in probe_cases:
        assert case["observed"] == "authority_field", case["case_id"]
    # A key nested under ``input`` is operation data, not top-level authority;
    # the fixture documents that truth and still reports no execution.
    nested = _by_id(report)["E-nested-input-key-is-not-authority"]
    assert nested["observed"] == "proposal"
    assert report["execution_dispatched"] is False


def test_operation_outside_the_offered_set_is_never_accepted(report: dict[str, Any]) -> None:
    for case in _scenario_cases(report, "D"):
        assert case["observed"] == "disallowed_operation", case["case_id"]
    assert "shell.exec" not in harness.EVAL_ALLOWED_OPERATIONS


def test_explicit_refusal_is_preserved_and_not_converted(report: dict[str, Any]) -> None:
    by_id = _by_id(report)
    assert by_id["G-explicit-refusal-preserved"]["observed"] == "producer_refused"
    assert by_id["G-adapter-refusal-not-converted"]["observed"] == "producer_refused"
    assert by_id["G-empty-model-response"]["observed"] == "malformed"


def test_provider_failure_and_no_model_are_explicit_and_distinct(report: dict[str, Any]) -> None:
    by_id = _by_id(report)
    failures = _scenario_cases(report, "H")
    no_model = _scenario_cases(report, "I")
    assert len(failures) >= 4 and len(no_model) >= 2
    for case in failures:
        assert case["observed"] in {"producer_failed", "malformed", "schema_violation"}, case
        assert case["observed"] != "proposal"
    for case in no_model:
        assert case["observed"] == "producer_refused"
    assert by_id["H-provider-exception"]["observed"] == "producer_failed"
    assert by_id["H-provider-timeout-bounded"]["observed"] == "producer_failed"
    assert by_id["H-retry-is-bounded"]["observed"] == "producer_failed"
    assert by_id["I-null-cognition-refuses"]["observed"] == "producer_refused"


def test_injection_shaped_fixtures_are_labelled_and_rejected(report: dict[str, Any]) -> None:
    probes = [case for case in report["cases"] if case["probe"] == "synthetic_boundary_probe"]
    assert len(probes) >= 4
    assert report["synthetic_boundary_probe_cases"] == len(probes)
    outcomes = {case["case_id"]: case["observed"] for case in probes}
    assert outcomes["F-injection-response-authority-fields"] == "authority_field"
    assert outcomes["F-injection-response-prose"] == "malformed"
    assert outcomes["F-injection-response-disallowed-operation"] == "disallowed_operation"
    # Context text is data: a benign proposal produced under hostile intent
    # text is still only a proposal, never an authority or an execution.
    assert outcomes["F-injection-context-valid-proposal"] == "proposal"


# -- 7..9. report integrity, separation, determinism ----------------------


def test_report_counters_are_recomputed_from_the_case_results(report: dict[str, Any]) -> None:
    cases = _cases(report)
    assert report["cases_total"] == len(cases)
    assert report["cases_passed"] == sum(1 for case in cases if case["passed"])
    assert report["cases_failed"] == sum(1 for case in cases if not case["passed"])
    accepted = sum(1 for case in cases if case["observed"] == "proposal")
    assert report["valid_proposals_accepted"] == accepted
    reasons: dict[str, int] = {}
    for case in cases:
        if case["observed"] in harness._REFUSAL_OUTCOMES:
            reasons[case["observed"]] = reasons.get(case["observed"], 0) + 1
    assert report["refusal_reasons"] == dict(sorted(reasons.items()))
    assert report["refusals_observed"] == sum(reasons.values())
    # Scenario J is a suite-level invariant, not a producer outcome, so it is
    # counted separately: every other case is exactly one proposal or refusal.
    invariant = sum(1 for case in cases if case["scenario"] == "J")
    assert invariant == 1
    assert accepted + report["refusals_observed"] + invariant == report["cases_total"]
    for case in cases:
        assert set(case) == CASE_KEYS


def test_acceptance_is_not_reported_as_verification_or_execution(report: dict[str, Any]) -> None:
    assert set(report) == REPORT_KEYS
    assert report["verification_status"] == "NOT_RUN"
    assert report["execution_dispatched"] is False
    assert report["execution_modules_loaded_during_run"] == []
    assert report["network_used"] is False
    assert report["real_model_used"] is False
    assert report["valid_proposals_accepted"] > 0
    separation = _by_id(report)["J-acceptance-is-not-verification"]
    assert separation["observed"] == "separated"
    forbidden = {"verified", "verified_cases", "artifacts", "rendered", "executed"}
    assert forbidden.isdisjoint(report)


def test_the_separation_invariant_is_not_tautological(
    monkeypatch: pytest.MonkeyPatch, report: dict[str, Any]
) -> None:
    results = [case for case in report["cases"] if case["scenario"] != "J"]
    assert harness._verification_separation_result(results)["passed"] is True

    monkeypatch.setattr(harness, "VERIFICATION_STATUS", "VERIFIED")
    claimed = harness._verification_separation_result(results)
    assert claimed["passed"] is False
    assert claimed["observed"].startswith("not_separated:")
    assert "verification_claimed" in claimed["observed"]

    monkeypatch.setattr(harness, "VERIFICATION_STATUS", "NOT_RUN")
    monkeypatch.setattr(harness, "EXECUTION_DISPATCHED", True)
    dispatched = harness._verification_separation_result(results)
    assert dispatched["passed"] is False
    assert "execution_dispatched" in dispatched["observed"]


def test_an_exploding_fixture_is_a_failure_not_a_crash() -> None:
    broken = harness._Case(
        case_id="Z-exploding-fixture",
        scenario="A",
        expected="proposal",
        probe="fixture",
        run=_raise_runtime_error,
    )
    result = _await(harness._execute_case(broken))
    assert result["passed"] is False
    assert result["observed"] == "unexpected_error:RuntimeError"


def _raise_runtime_error() -> str:
    raise RuntimeError("synthetic harness bug")


def _await(awaitable: Any) -> Any:
    import asyncio

    return asyncio.run(awaitable)


def test_repeated_runs_are_behaviourally_identical(report: dict[str, Any]) -> None:
    repeated = harness.evaluate(repeats=3)
    assert repeated["repeatability"]["identical"] is True
    assert repeated["repeatability"]["mismatched_case_ids"] == []
    assert len(set(repeated["repeatability"]["fingerprints"])) == 1
    assert repeated["cases_failed"] == 0

    # A second process-level run must agree: determinism is not luck.
    again = harness.evaluate(repeats=1)
    assert again["repeatability"]["fingerprints"][0] == repeated["repeatability"]["fingerprints"][0]

    latency = repeated["latency_summary"]
    assert latency["unit"] == "ms"
    assert latency["suite_ms_min"] <= latency["suite_ms_p50"] <= latency["suite_ms_max"]
    assert latency["suite_ms_max"] > 0


# -- 10..12. isolation, non-disclosure, CLI contract ----------------------


def test_harness_runs_without_network_and_without_api_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _blocked(*args: object, **kwargs: object) -> None:
        raise AssertionError("the evaluation harness must not touch the network")

    # Patch the outbound paths, not the class: ``asyncio.run`` builds an
    # event loop that needs a local socketpair, which is not network access.
    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)
    monkeypatch.setattr(socket, "getaddrinfo", _blocked)
    for key in (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GEMINI_API_KEY",
        "GROQ_API_KEY",
        "OPENROUTER_API_KEY",
        "NEXUS_MODEL_PATH",
    ):
        monkeypatch.delenv(key, raising=False)

    offline = harness.evaluate(repeats=1)
    assert offline["cases_failed"] == 0
    assert offline["network_used"] is False
    assert offline["real_model_used"] is False


def _run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
    )


def test_cli_json_report_is_clean_and_consistent() -> None:
    completed = _run_cli("--json", "--repeats", "1")
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert set(payload) == REPORT_KEYS
    assert payload["cases_failed"] == 0
    assert payload["verification_status"] == "NOT_RUN"


def test_cli_text_report_does_not_disclose_prompt_or_response_text() -> None:
    for args in ((), ("--json",)):
        completed = _run_cli(*args)
        assert completed.returncode == 0, completed.stderr
        combined = completed.stdout + completed.stderr
        for marker in SECRET_MARKERS:
            assert marker not in combined, f"report disclosed fixture text: {marker!r}"


@pytest.mark.parametrize("value", ["0", "11", "-3", "abc", "1.5"])
def test_invalid_cli_repeat_counts_are_rejected(value: str) -> None:
    completed = _run_cli("--repeats", value)
    assert completed.returncode == 2
    assert "Traceback" not in completed.stderr
    assert completed.stdout == ""


def test_programmatic_invalid_repeat_counts_are_rejected() -> None:
    for value in (0, 11, -1, True, "3"):
        with pytest.raises(ValueError):
            harness.evaluate(repeats=value)  # type: ignore[arg-type]


# -- static proof: no network, no execution lane --------------------------


def _imported_modules() -> set[str]:
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_the_script_imports_only_stdlib_and_the_cognition_contract() -> None:
    allowed_stdlib = {
        "__future__",
        "argparse",
        "asyncio",
        "collections",
        "collections.abc",
        "dataclasses",
        "hashlib",
        "inspect",
        "json",
        "sys",
        "time",
        "typing",
    }
    for module in _imported_modules():
        if module in allowed_stdlib:
            continue
        assert module.startswith("nexus_ai_agent.nagar.cognition"), module
    forbidden = ("socket", "urllib", "http", "httpx", "requests", "subprocess")
    for module in _imported_modules():
        assert not module.startswith(forbidden), module


def test_the_script_never_reaches_the_execution_lane() -> None:
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    attributes = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    assert "CommandBus" not in names
    assert "dispatch" not in attributes
    for module in _imported_modules():
        assert not module.startswith("nexus_ai_agent.creative.studio"), module
