"""Contract tests for the cognition quality evaluation harness (task-269).

The parameterised boundary suites already prove individual properties of
``parse_proposal`` / ``LocalCognition`` / ``NullCognition``.  These tests cover
what is *new* and would otherwise be unverified:

* the harness has a complete fixture set (scenarios A..K) whose expectations
  match the live registry contract; a dropped fixture turns this file red;
* every reported counter is **recomputed here from the case results**, so a
  hard-coded number cannot survive;
* acceptance is separated from execution and verification, and the separating
  invariant is proven **non-tautological** by flipping it;
* determinism is behavioural (fingerprint) while latency is reported apart;
* the default run is network-free and key-free (sockets are blocked at runtime
  and the module's import allowlist is checked statically);
* the report is **non-disclosing** — prompt/intent text, model prose and
  provider exception text never reach stdout, stderr or the JSON payload;
* invalid CLI input is refused with exit 2 and no traceback;
* the committed **versioned baseline** is a real regression contract: the
  checker independently recomputes verdicts and aggregates instead of trusting
  the report's own ``passed``/counter fields, ignores latency and timestamps,
  fails closed on a missing, malformed, duplicated, version-incompatible or
  stale baseline, and never rewrites the committed baseline;
* the fixture set itself is validated like data (unique ids, known scenarios,
  documented outcomes, no silently dropped scenario).

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
BASELINE = ROOT / "tests" / "fixtures" / "cognition_quality_eval" / "v2_baseline.json"

_spec = importlib.util.spec_from_file_location("cognition_quality_eval", SCRIPT)
assert _spec and _spec.loader
harness = importlib.util.module_from_spec(_spec)
sys.modules["cognition_quality_eval"] = harness
_spec.loader.exec_module(harness)

REQUIRED_SCENARIOS = tuple("ABCDEFGHIJK")

#: The report contract.  A new field is fine, but it must be added here too —
#: that is what stops "proposal accepted" from quietly becoming "verified".
REPORT_KEYS = {
    "evaluation_version",
    "execution_mode",
    "registry",
    "execution_modules_loaded",
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
    assert harness.DISALLOWED_OPERATION not in report["registry"]["offered_operations"]


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
    # J and K are suite invariants, not producer outcomes.
    invariant = sum(1 for case in cases if case["scenario"] in {"J", "K"})
    assert invariant == 2
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


def test_the_script_imports_only_stdlib_cognition_and_runtime_factory() -> None:
    allowed_stdlib = {
        "__future__",
        "argparse",
        "asyncio",
        "collections",
        "collections.abc",
        "contextvars",
        "dataclasses",
        "hashlib",
        "inspect",
        "json",
        "pathlib",
        "sys",
        "time",
        "typing",
    }
    for module in _imported_modules():
        if module in allowed_stdlib:
            continue
        assert module.startswith("nexus_ai_agent.nagar.cognition") or module == (
            "nexus_ai_agent.creative.packs.runtime"
        ), module
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


# -- versioned regression baseline ----------------------------------------


def _baseline_copy(tmp_path: Path, mutate: Any) -> Path:
    """Copy the committed baseline through ``mutate`` into ``tmp_path``."""
    data = json.loads(BASELINE.read_text(encoding="utf-8"))
    mutate(data)
    target = tmp_path / "baseline.json"
    target.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return target


def _check_cli(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        cwd=cwd or ROOT,
        check=False,
    )


def test_committed_baseline_matches_the_current_evaluation(report: dict[str, Any]) -> None:
    baseline = harness.load_baseline(BASELINE)
    assert harness.check_baseline(report, baseline) == []
    assert baseline["baseline_schema_version"] == harness.BASELINE_SCHEMA_VERSION
    assert baseline["evaluation_version"] == report["evaluation_version"]
    assert baseline["fingerprint"] == report["repeatability"]["fingerprints"][0]
    assert [case["case_id"] for case in baseline["cases"]] == sorted(
        case["case_id"] for case in baseline["cases"]
    )


def test_the_committed_baseline_pins_no_timestamps_paths_or_latency() -> None:
    text = BASELINE.read_text(encoding="utf-8")
    for forbidden in ("latency", "suite_ms", "timestamp", "created_at", "/home/", "T00:00"):
        assert forbidden not in text, forbidden
    data = json.loads(text)
    assert set(data) == {
        "baseline_schema_version",
        "evaluation_version",
        "registry",
        "contract",
        "counts",
        "refusal_reasons",
        "fingerprint",
        "cases",
    }
    for marker in ("Ignore the safety policy", "deny_by_contract", "clip_eval_1"):
        assert marker not in text


def test_the_committed_baseline_is_not_generated_from_the_run_it_verifies(
    report: dict[str, Any], tmp_path: Path
) -> None:
    # A candidate is derived from a report; the *committed* baseline is a
    # separate reviewed artifact.  If it were rewritten from the current run it
    # would be an expected-output cache, so the check must be sensitive to any
    # real drift between the two.
    assert harness.build_baseline(report) == harness.load_baseline(BASELINE)


def _restate(data: dict[str, Any]) -> None:
    """Recompute a baseline's own aggregates after an intentional case edit.

    Without this, a drifted case would still be caught by the aggregate
    counters — which would hide a checker that stopped comparing the cases
    themselves.  Restating the aggregates makes the case-level comparison the
    only thing left that can notice the edit.
    """
    cases = data["cases"]
    data["counts"]["cases_total"] = len(cases)
    data["counts"]["cases_passed"] = sum(1 for case in cases if case["passed"])
    data["counts"]["cases_failed"] = sum(1 for case in cases if not case["passed"])
    data["counts"]["valid_proposals_accepted"] = sum(
        1 for case in cases if case["observed"] == "proposal"
    )
    data["counts"]["synthetic_boundary_probe_cases"] = sum(
        1 for case in cases if case["probe"] == "synthetic_boundary_probe"
    )
    reasons: dict[str, int] = {}
    for case in cases:
        if case["observed"] in harness._REFUSAL_OUTCOMES:
            reasons[case["observed"]] = reasons.get(case["observed"], 0) + 1
    data["refusal_reasons"] = dict(sorted(reasons.items()))
    data["counts"]["refusals_observed"] = sum(reasons.values())


def _edit_case(data: dict[str, Any], case_id: str, **changes: Any) -> None:
    for case in data["cases"]:
        if case["case_id"] == case_id:
            case.update(changes)
            return
    raise AssertionError(f"baseline has no case {case_id!r}")


def test_a_changed_outcome_is_detected_even_when_the_baseline_is_self_consistent(
    tmp_path: Path,
) -> None:
    case_id = "A-valid-json-caller-provenance"

    def drift(data: dict[str, Any]) -> None:
        _edit_case(data, case_id, observed="malformed", passed=False)
        _restate(data)

    path = _baseline_copy(tmp_path, drift)
    report = harness.evaluate(repeats=1)
    problems = harness.check_baseline(report, harness.load_baseline(path))
    assert any(case_id in problem and "observed changed" in problem for problem in problems), (
        problems
    )


def test_a_changed_expectation_is_detected_even_when_counts_agree(tmp_path: Path) -> None:
    case_id = "B-empty-string"

    def drift(data: dict[str, Any]) -> None:
        _edit_case(data, case_id, expected="proposal", passed=False)
        _restate(data)

    path = _baseline_copy(tmp_path, drift)
    report = harness.evaluate(repeats=1)
    problems = harness.check_baseline(report, harness.load_baseline(path))
    assert any(case_id in problem and "expected changed" in problem for problem in problems), (
        problems
    )


def test_a_tampered_report_verdict_is_detected(report: dict[str, Any]) -> None:
    # A refusal fixture relabelled as an accepted proposal, with ``passed`` still
    # True: the pair (expected=malformed, observed=proposal) contradicts itself,
    # so a checker that trusted ``passed`` would accept it.
    tampered = json.loads(json.dumps(report))
    for case in tampered["cases"]:
        if case["case_id"] == "B-empty-string":
            case["observed"] = "proposal"
            case["passed"] = True
    problems = harness.check_baseline(tampered, harness.load_baseline(BASELINE))
    assert any("B-empty-string" in problem for problem in problems), problems
    assert any("verdict does not match its own outcomes" in problem for problem in problems)


def test_a_missing_fixture_is_detected(report: dict[str, Any], tmp_path: Path) -> None:
    def drop(data: dict[str, Any]) -> None:
        removed = data["cases"].pop(10)
        data["counts"]["cases_total"] -= 1
        data["counts"]["cases_passed"] -= 1 if removed["passed"] else 0
        data["counts"]["refusals_observed"] -= 1
        data["refusal_reasons"][removed["observed"]] -= 1

    path = _baseline_copy(tmp_path, drop)
    problems = harness.check_baseline(report, harness.load_baseline(path))
    assert any("unexpected case" in problem for problem in problems)


def test_an_unexpected_case_is_detected(report: dict[str, Any], tmp_path: Path) -> None:
    def add(data: dict[str, Any]) -> None:
        extra = dict(data["cases"][0])
        extra["case_id"] = "Z-injected-case"
        data["cases"].append(extra)
        data["counts"]["cases_total"] += 1
        data["counts"]["cases_passed"] += 1

    path = _baseline_copy(tmp_path, add)
    problems = harness.check_baseline(report, harness.load_baseline(path))
    assert any("missing from the report" in problem for problem in problems)


def test_duplicate_fixture_ids_fail_closed(report: dict[str, Any], tmp_path: Path) -> None:
    def duplicate(data: dict[str, Any]) -> None:
        data["cases"].append(dict(data["cases"][0]))

    path = _baseline_copy(tmp_path, duplicate)
    baseline_problems = harness.check_baseline(report, harness.load_baseline(path))
    assert any("duplicate case_id" in problem for problem in baseline_problems)

    duplicated_report = json.loads(json.dumps(report))
    duplicated_report["cases"].append(dict(duplicated_report["cases"][0]))
    report_problems = harness.check_baseline(duplicated_report, harness.load_baseline(BASELINE))
    assert any("report has duplicate case_id" in problem for problem in report_problems)


def test_an_incompatible_baseline_version_fails_closed(
    report: dict[str, Any], tmp_path: Path
) -> None:
    def bump(data: dict[str, Any]) -> None:
        data["baseline_schema_version"] = harness.BASELINE_SCHEMA_VERSION + 1

    path = _baseline_copy(tmp_path, bump)
    problems = harness.check_baseline(report, harness.load_baseline(path))
    assert any("baseline_schema_version" in problem for problem in problems)

    def retag(data: dict[str, Any]) -> None:
        data["evaluation_version"] = "nexus-cognition-quality-eval/v0"

    path = _baseline_copy(tmp_path, retag)
    problems = harness.check_baseline(report, harness.load_baseline(path))
    assert any("evaluation_version drifted" in problem for problem in problems)


def test_a_mutated_expected_outcome_cannot_be_silently_accepted(
    report: dict[str, Any], tmp_path: Path
) -> None:
    case_id = report["cases"][0]["case_id"]
    original = report["cases"][0]["observed"]

    def mutate(data: dict[str, Any]) -> None:
        for case in data["cases"]:
            if case["case_id"] == case_id:
                case["expected"] = "deliberately_wrong"
                # ``passed`` is left alone on purpose: a checker that trusted it
                # would now accept a baseline that contradicts itself.
                case["passed"] = True

    path = _baseline_copy(tmp_path, mutate)
    problems = harness.check_baseline(report, harness.load_baseline(path))
    assert any(case_id in problem and "self-inconsistent" in problem for problem in problems)
    assert any(case_id in problem and "expected" in problem for problem in problems), original


def test_latency_and_timestamp_changes_alone_do_not_break_the_baseline(
    report: dict[str, Any],
) -> None:
    drifted = json.loads(json.dumps(report))
    drifted["latency_summary"]["suite_ms_p50"] = 999.999
    drifted["latency_summary"]["suite_ms_max"] = 12345.0
    drifted["repeats"] = 7
    drifted["repeatability"]["fingerprints"] = drifted["repeatability"]["fingerprints"][:1]
    drifted["observed_at"] = "2030-01-01T00:00:00Z"  # a field the baseline must ignore
    assert harness.check_baseline(drifted, harness.load_baseline(BASELINE)) == []


def test_a_fingerprint_drift_is_reported(report: dict[str, Any], tmp_path: Path) -> None:
    def drift(data: dict[str, Any]) -> None:
        data["fingerprint"] = "deadbeefdeadbeef"

    path = _baseline_copy(tmp_path, drift)
    problems = harness.check_baseline(report, harness.load_baseline(path))
    assert any("fingerprint drifted" in problem for problem in problems)


def test_a_tampered_report_counter_is_recomputed_and_rejected(report: dict[str, Any]) -> None:
    tampered = json.loads(json.dumps(report))
    tampered["valid_proposals_accepted"] += 1
    problems = harness.check_baseline(tampered, harness.load_baseline(BASELINE))
    assert any("valid_proposals_accepted" in problem for problem in problems)


def test_check_cli_passes_on_the_committed_baseline_and_never_writes_it() -> None:
    before = BASELINE.read_bytes()
    completed = _check_cli("--check", "--repeats", "1")
    assert completed.returncode == 0, completed.stderr
    assert "baseline check PASS" in completed.stdout
    assert BASELINE.read_bytes() == before


def test_check_cli_reports_a_mismatch_with_exit_code_1(tmp_path: Path) -> None:
    def drift(data: dict[str, Any]) -> None:
        data["cases"][0]["observed"] = "malformed"
        data["cases"][0]["passed"] = False
        data["fingerprint"] = "0000000000000000"

    path = _baseline_copy(tmp_path, drift)
    before = path.read_bytes()
    completed = _check_cli("--check", "--baseline", str(path), "--repeats", "1")
    assert completed.returncode == 1, completed.stderr
    assert "baseline check FAIL" in completed.stdout
    assert "Traceback" not in completed.stderr
    assert "was NOT modified" in completed.stdout
    assert path.read_bytes() == before  # --check never rewrites its own input


def test_check_cli_json_mode_is_machine_readable(tmp_path: Path) -> None:
    completed = _check_cli("--check", "--json", "--repeats", "1")
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["status"] == "PASS"
    assert payload["problems"] == []
    assert payload["report"]["verification_status"] == "NOT_RUN"
    assert set(payload["report"]) == REPORT_KEYS


@pytest.mark.parametrize(
    "content",
    ['{"baseline_schema_version": 1,', "[]", '{"cases": "not-a-list", "counts": 1}'],
)
def test_check_cli_refuses_a_malformed_baseline_without_traceback(
    tmp_path: Path, content: str
) -> None:
    path = tmp_path / "broken.json"
    path.write_text(content, encoding="utf-8")
    completed = _check_cli("--check", "--baseline", str(path), "--repeats", "1")
    assert completed.returncode == 2, completed.stdout
    assert "error:" in completed.stderr
    assert "Traceback" not in completed.stderr
    assert completed.stdout == ""


def test_check_cli_refuses_a_missing_baseline_without_traceback(tmp_path: Path) -> None:
    completed = _check_cli("--check", "--baseline", str(tmp_path / "nope.json"), "--repeats", "1")
    assert completed.returncode == 2
    assert "cannot be read" in completed.stderr
    assert "Traceback" not in completed.stderr


def test_check_output_discloses_no_fixture_text(tmp_path: Path) -> None:
    def drift(data: dict[str, Any]) -> None:
        data["fingerprint"] = "1111111111111111"

    path = _baseline_copy(tmp_path, drift)
    for args in (("--check", "--repeats", "1"), ("--check", "--json", "--repeats", "1")):
        completed = _check_cli(*args)
        combined = completed.stdout + completed.stderr
        for marker in SECRET_MARKERS:
            assert marker not in combined, marker
    failed = _check_cli("--check", "--baseline", str(path), "--repeats", "1")
    for marker in SECRET_MARKERS:
        assert marker not in failed.stdout + failed.stderr


def test_write_candidate_refuses_to_overwrite_the_committed_baseline() -> None:
    completed = _check_cli("--write-candidate", str(BASELINE), "--repeats", "1")
    assert completed.returncode == 2
    assert "refusing to overwrite the committed baseline" in completed.stderr
    assert json.loads(BASELINE.read_text(encoding="utf-8"))["fingerprint"] == "268adff247b9060a"


def test_write_candidate_rejects_a_path_outside_the_repository(tmp_path: Path) -> None:
    report = harness.evaluate(repeats=1)
    with pytest.raises(harness.BaselineError):
        harness.write_candidate_baseline(report, tmp_path / "candidate.json", allowed_root=ROOT)


def test_write_candidate_produces_a_reviewable_equivalent_baseline(tmp_path: Path) -> None:
    report = harness.evaluate(repeats=1)
    target = tmp_path / "candidate.json"
    harness.write_candidate_baseline(report, target, allowed_root=tmp_path)
    candidate = json.loads(target.read_text(encoding="utf-8"))
    assert harness.check_baseline(report, candidate) == []
    assert candidate == json.loads(BASELINE.read_text(encoding="utf-8"))


def test_check_and_write_candidate_cannot_be_combined() -> None:
    completed = _check_cli("--check", "--write-candidate", "x.json")
    assert completed.returncode == 2
    assert "cannot be combined" in completed.stderr
    assert "Traceback" not in completed.stderr


# -- fixture-set validation -----------------------------------------------


def test_the_real_fixture_set_is_valid() -> None:
    assert harness.validate_fixture_set(harness._build_cases()) == []


def _case(**overrides: Any) -> Any:
    base = dict(
        case_id="X-valid",
        scenario="A",
        expected="proposal",
        probe="fixture",
        run=lambda: "proposal",
    )
    base.update(overrides)
    return harness._Case(**base)


@pytest.mark.parametrize(
    ("cases", "needle"),
    [
        ([], "fixture set is empty"),
        ([_case(), _case()], "duplicate case_id"),
        ([_case(case_id="")], "non-blank string"),
        ([_case(scenario="Q")], "unknown scenario 'Q'"),
        ([_case(expected="looks_fine")], "undocumented expected outcome"),
        ([_case(probe="wishful")], "unknown probe label"),
        ([_case(scenario="J")], "suite-level and must not be a fixture"),
    ],
)
def test_invalid_fixture_sets_are_rejected(cases: list[Any], needle: str) -> None:
    problems = harness.validate_fixture_set(cases)
    assert any(needle in problem for problem in problems), problems


def test_a_dropped_scenario_is_an_invalid_fixture_set() -> None:
    cases = [case for case in harness._build_cases() if case.scenario != "E"]
    problems = harness.validate_fixture_set(cases)
    assert any("scenario E has no fixture" in problem for problem in problems)


def test_evaluate_fails_closed_on_an_invalid_fixture_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(harness, "_build_cases", lambda: (_case(), _case()))
    with pytest.raises(ValueError, match="invalid fixture set"):
        harness.evaluate(repeats=1)


def test_the_cli_reports_an_invalid_fixture_set_with_exit_code_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(harness, "_build_cases", lambda: ())
    assert harness.main(["--repeats", "1"]) == 2
    captured = capsys.readouterr()
    assert "invalid fixture set" in captured.err


# -- cross-environment determinism ----------------------------------------


@pytest.mark.parametrize("seed", ["0", "1", "12345"])
def test_the_fingerprint_is_stable_across_hash_seeds(seed: str) -> None:
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--json", "--repeats", "2"],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
        env={"PYTHONHASHSEED": seed, "PATH": "/usr/bin:/bin"},
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["repeatability"]["identical"] is True
    assert payload["repeatability"]["fingerprints"][0] == "268adff247b9060a"


def test_the_report_is_identical_from_a_foreign_working_directory(tmp_path: Path) -> None:
    from_cwd = _check_cli("--json", "--repeats", "1", cwd=tmp_path)
    from_root = _check_cli("--json", "--repeats", "1")
    assert from_cwd.returncode == 0, from_cwd.stderr
    first = json.loads(from_root.stdout)
    second = json.loads(from_cwd.stdout)
    assert first["cases"] == second["cases"]
    assert first["repeatability"]["fingerprints"] == second["repeatability"]["fingerprints"]
    assert first["refusal_reasons"] == second["refusal_reasons"]
    # The default baseline path is resolved against the repository root, not the CWD.
    checked = _check_cli("--check", "--repeats", "1", cwd=tmp_path)
    assert checked.returncode == 0, checked.stderr


def test_case_order_is_stable_across_runs(report: dict[str, Any]) -> None:
    # Ordering is part of the report contract: the baseline is compared by id,
    # but a human diff of two ``--json`` runs must not churn.
    again = harness.evaluate(repeats=1)
    assert [case["case_id"] for case in report["cases"]] == [
        case["case_id"] for case in again["cases"]
    ]


def test_a_failing_fixture_is_visible_and_fails_the_cli(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cases = list(harness._build_cases())
    target = cases[0]
    cases[0] = harness._Case(
        case_id=target.case_id,
        scenario=target.scenario,
        expected=target.expected,
        probe=target.probe,
        run=lambda: "malformed",  # a wrong outcome: must not be hidden
    )
    monkeypatch.setattr(harness, "_build_cases", lambda: tuple(cases))
    assert harness.main(["--repeats", "1"]) == harness.EXIT_MISMATCH
    printed = capsys.readouterr().out
    assert f"FAIL {target.case_id}" in printed
    assert "failed=1" in printed


def test_a_failing_fixture_also_fails_the_baseline_check(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cases = list(harness._build_cases())
    target = cases[0]
    cases[0] = harness._Case(
        case_id=target.case_id,
        scenario=target.scenario,
        expected=target.expected,
        probe=target.probe,
        run=lambda: "malformed",
    )
    monkeypatch.setattr(harness, "_build_cases", lambda: tuple(cases))
    # A mismatch must never degrade to a warning: the check stays non-zero.
    assert harness.main(["--check", "--repeats", "1"]) == harness.EXIT_MISMATCH
    captured = capsys.readouterr()
    assert "baseline check FAIL" in captured.out


def test_a_structurally_broken_report_is_reported_not_crashed(report: dict[str, Any]) -> None:
    baseline = harness.load_baseline(BASELINE)
    missing_key = json.loads(json.dumps(report))
    del missing_key["cases"][3]["observed"]
    problems = harness.check_baseline(missing_key, baseline)
    assert any("is missing observed" in problem for problem in problems)

    not_an_object = json.loads(json.dumps(report))
    not_an_object["cases"][2] = "not-an-object"
    problems = harness.check_baseline(not_an_object, baseline)
    assert any("is not an object" in problem for problem in problems)

    # A clean report is still clean: the guard must not turn into noise.
    assert harness.check_baseline(report, baseline) == []


# -- V2: production registry binding and fail-closed drift -----------------


class _Registry:
    def __init__(self, operations: Any) -> None:
        self.operations = operations
        self.calls = 0

    def list_operations(self) -> Any:
        self.calls += 1
        return self.operations


def test_v2_default_offer_is_the_production_runtime_catalog(report: dict[str, Any]) -> None:
    from nexus_ai_agent.creative.packs.runtime import build_runtime_registry
    from nexus_ai_agent.nagar.cognition.capabilities import offered_operations

    operations = offered_operations(build_runtime_registry())
    assert report["registry"] == {"status": "ok", "offered_operations": sorted(operations)}
    assert "creative.caption" not in operations
    assert _by_id(report)["K-registry-drift"]["passed"] is True
    assert report["evaluation_version"] == "nexus-cognition-quality-eval/v2"
    assert "nexus_ai_agent.creative.studio.bus" in report["execution_modules_loaded"]


def test_registry_additions_removals_and_order_change_the_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _Registry(["probe.one", "probe.two"])
    monkeypatch.setattr(harness, "_runtime_registry", lambda: registry)
    first = harness.evaluate(repeats=1)
    registry.operations = ["probe.two", "probe.one"]
    reordered = harness.evaluate(repeats=1)
    assert first["registry"] == reordered["registry"]
    assert first["repeatability"]["fingerprints"] == reordered["repeatability"]["fingerprints"]

    registry.operations = ["probe.one", "probe.two", "probe.three", "probe.op-with-dash"]
    added = harness.evaluate(repeats=1)
    assert "probe.three" in added["registry"]["offered_operations"]
    assert "probe.op-with-dash" in added["registry"]["offered_operations"]
    assert added["repeatability"]["fingerprints"] != first["repeatability"]["fingerprints"]
    registry.operations = ["probe.one"]
    removed = harness.evaluate(repeats=1)
    assert "probe.two" not in removed["registry"]["offered_operations"]
    assert removed["repeatability"]["fingerprints"] != first["repeatability"]["fingerprints"]
    for changed in (added, removed):
        assert any(
            "registry drift" in p
            for p in harness.check_baseline(changed, harness.build_baseline(first))
        )


def test_registry_is_snapshotted_once_for_all_repeats(monkeypatch: pytest.MonkeyPatch) -> None:
    class ChangingRegistry(_Registry):
        def list_operations(self) -> Any:
            self.calls += 1
            return ["probe.first"] if self.calls == 1 else ["probe.second"]

    registry = ChangingRegistry([])
    monkeypatch.setattr(harness, "_runtime_registry", lambda: registry)
    report = harness.evaluate(repeats=3)
    assert registry.calls == 1
    assert report["registry"]["offered_operations"] == ["probe.first"]
    assert report["repeatability"]["identical"] is True


def test_fixture_cannot_widen_the_snapshot() -> None:
    token = harness._CURRENT_SNAPSHOT.set(harness._RegistrySnapshot("ok", frozenset({"probe.one"})))
    try:
        schema = harness._schema(allowed=frozenset({"probe.one", "probe.injected"}))
        assert schema.allowed_operations == frozenset({"probe.one"})
        assert harness._parse(harness._payload(operation="probe.injected"), schema=schema) == (
            "disallowed_operation"
        )
        assert harness._parse(harness._payload(operation="probe.one", permissions=["*"])) == (
            "authority_field"
        )
    finally:
        harness._CURRENT_SNAPSHOT.reset(token)


def test_empty_registry_is_diagnostic_but_never_green(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(harness, "_runtime_registry", lambda: _Registry([]))
    report = harness.evaluate(repeats=1)
    assert report["registry"] == {"status": "empty", "offered_operations": []}
    assert report["valid_proposals_accepted"] == 0
    assert _by_id(report)["K-registry-drift"]["observed"] == "registry_empty"
    assert _by_id(report)["K-registry-drift"]["passed"] is False
    assert harness.main(["--check", "--json", "--repeats", "1"]) == 1
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "FAIL"
    assert output["report"]["registry"]["status"] == "empty"


@pytest.mark.parametrize(
    "operations",
    [
        None,
        "probe.one",
        {"probe.one": 1},
        [None],
        [""],
        ["bad id"],
        ["no_dot"],
        ["probe.ok", "BAD"],
    ],
)
def test_unusable_registry_is_reported_without_sensitive_content(
    operations: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(harness, "_runtime_registry", lambda: _Registry(operations))
    assert harness.main(["--json", "--repeats", "1"]) == 2
    output = json.loads(capsys.readouterr().out)
    assert output["registry"] == {"status": "unusable", "offered_operations": []}
    assert output["valid_proposals_accepted"] == 0
    assert _by_id(output)["K-registry-drift"]["passed"] is False


def test_registry_exception_is_not_disclosed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def broken() -> Any:
        raise RuntimeError("must-never-be-logged")

    monkeypatch.setattr(harness, "_runtime_registry", broken)
    assert harness.main(["--check", "--json", "--repeats", "1"]) == 2
    output = capsys.readouterr()
    assert "must-never-be-logged" not in output.out + output.err
    assert json.loads(output.out)["report"]["registry"]["status"] == "unusable"


def test_registry_method_exception_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    class Broken(_Registry):
        def list_operations(self) -> Any:
            raise RuntimeError("private-token")

    monkeypatch.setattr(harness, "_runtime_registry", lambda: Broken([]))
    report = harness.evaluate(repeats=1)
    assert report["registry"]["status"] == "unusable"
    assert report["cases_failed"] > 0
    assert "private-token" not in json.dumps(report)


def test_v1_baseline_is_historical_and_never_silently_accepted() -> None:
    historical = ROOT / "tests" / "fixtures" / "cognition_quality_eval" / "v1_baseline.json"
    before = historical.read_bytes()
    result = _check_cli("--check", "--baseline", str(historical), "--repeats", "1")
    assert result.returncode != 0
    assert historical.read_bytes() == before
    candidate = _check_cli("--write-candidate", str(historical), "--repeats", "1")
    assert candidate.returncode == 2
    assert historical.read_bytes() == before


def test_report_fingerprint_is_recomputed_from_registry_and_cases(report: dict[str, Any]) -> None:
    tampered = json.loads(json.dumps(report))
    tampered["registry"]["offered_operations"].append("zz.injected")
    baseline = harness.build_baseline(tampered)
    assert any("report fingerprint" in p for p in harness.check_baseline(tampered, baseline))


def test_evaluation_never_constructs_or_dispatches_bus(monkeypatch: pytest.MonkeyPatch) -> None:
    from nexus_ai_agent.creative.studio.bus import CommandBus

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("evaluation attempted execution")

    monkeypatch.setattr(CommandBus, "__init__", forbidden)
    monkeypatch.setattr(CommandBus, "dispatch", forbidden)
    report = harness.evaluate(repeats=1)
    assert report["cases_failed"] == 0
    assert report["execution_dispatched"] is False
    assert report["verification_status"] == "NOT_RUN"


def test_removed_operation_is_refused_even_if_fixture_requests_it() -> None:
    first = harness._RegistrySnapshot("ok", frozenset({"probe.one", "probe.two"}))
    after = harness._RegistrySnapshot("ok", frozenset({"probe.one"}))
    token = harness._CURRENT_SNAPSHOT.set(first)
    try:
        assert harness._parse(harness._payload(operation="probe.two")) == "proposal"
        harness._CURRENT_SNAPSHOT.set(after)
        schema = harness._schema(allowed=frozenset({"probe.one", "probe.two"}))
        assert harness._parse(harness._payload(operation="probe.two"), schema=schema) == (
            "disallowed_operation"
        )
    finally:
        harness._CURRENT_SNAPSHOT.reset(token)


def test_fixture_outcome_text_never_reaches_report() -> None:
    case = harness._Case(
        case_id="A-untrusted-output",
        scenario="A",
        expected="proposal",
        probe="fixture",
        run=lambda: "must-never-be-logged",
    )
    result = _await(harness._execute_case(case))
    assert result["observed"] == "unexpected_outcome"
    assert result["passed"] is False
    assert "must-never-be-logged" not in json.dumps(result)


def test_candidate_rejects_failed_fixture_results(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(harness, "_runtime_registry", lambda: _Registry([]))
    report = harness.evaluate(repeats=1)
    target = tmp_path / "candidate.json"
    with pytest.raises(harness.BaselineError):
        harness.write_candidate_baseline(report, target, allowed_root=tmp_path)
    assert not target.exists()


def test_report_shape_failures_are_not_crashes(report: dict[str, Any]) -> None:
    baseline = harness.load_baseline(BASELINE)
    for field in ("cases", "repeatability", "evaluation_version", "verification_status"):
        damaged = json.loads(json.dumps(report))
        del damaged[field]
        assert harness.check_baseline(damaged, baseline), field


def test_raw_fixture_schema_cannot_widen_registry_even_through_adapter() -> None:
    token = harness._CURRENT_SNAPSHOT.set(harness._RegistrySnapshot("ok", frozenset({"probe.one"})))
    try:
        raw_schema = harness.ProposalSchema(
            schema_id=harness.EVAL_SCHEMA_ID,
            schema_version=1,
            allowed_operations=frozenset({"probe.one", "probe.injected"}),
        )
        payload = harness._payload_json(operation="probe.injected")
        assert harness._parse(payload, schema=raw_schema) == "disallowed_operation"
        assert _await(
            harness._adapter_observed(harness._StaticProvider(payload), schema=raw_schema)
        ) == ("disallowed_operation")
    finally:
        harness._CURRENT_SNAPSHOT.reset(token)


def test_tampered_baseline_aggregate_is_independently_recomputed(
    report: dict[str, Any], tmp_path: Path
) -> None:
    def tamper(data: dict[str, Any]) -> None:
        data["counts"]["valid_proposals_accepted"] += 1

    path = _baseline_copy(tmp_path, tamper)
    problems = harness.check_baseline(report, harness.load_baseline(path))
    assert any("count 'valid_proposals_accepted' changed" in problem for problem in problems)
