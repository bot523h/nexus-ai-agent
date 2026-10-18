"""Contract tests for the durable A-06 API-gate mutation campaign.

The runner's synthetic controls replace only subprocess execution and source
writes; the real campaign run separately proves that all fourteen edits apply
and are killed by their live API-inventory test oracles.
"""

from __future__ import annotations

import json
import runpy
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
SCRIPT_DIR = REPO / "scripts"
CAMPAIGN_SCRIPT = SCRIPT_DIR / "api_gate_mutations.py"
WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import security_mutation_support as mutation_support  # noqa: E402


def _load_campaign() -> dict[str, Any]:
    argv = sys.argv
    sys.argv = [str(CAMPAIGN_SCRIPT)]
    try:
        return runpy.run_path(str(CAMPAIGN_SCRIPT), run_name="api_gate_mutations_under_test")
    finally:
        sys.argv = argv


def _catalog() -> dict[str, Any]:
    return _load_campaign()


def _synthetic_mutations(
    support: Any, ids: tuple[str, ...] = tuple("ABCDEFGHIJKLMN")
) -> tuple[Any, ...]:
    return tuple(
        support.Mutation(
            name=f"synthetic-{mutation_id}",
            edits=(
                support.SourceEdit(
                    "tests/unit/test_api_gate_mutations.py",
                    f"unique-anchor-{mutation_id}",
                    f"weakened-anchor-{mutation_id}",
                ),
            ),
            tests=(f"tests/synthetic/test_{mutation_id.lower()}.py::test_{mutation_id.lower()}",),
            expected_failures=(f"oracle-{mutation_id}",),
            rationale=f"contract fixture {mutation_id}",
            mutation_id=mutation_id,
        )
        for mutation_id in ids
    )


def _install_fake_successful_environment(
    monkeypatch: pytest.MonkeyPatch,
    support: Any,
    mutations: tuple[Any, ...],
    baseline_tests: tuple[str, ...],
) -> None:
    monkeypatch.delenv("GITHUB_SHA", raising=False)
    monkeypatch.setattr(support, "_worktree_clean", lambda: (True, []))
    monkeypatch.setattr(support, "_apply_exact_mutation", lambda _mutation: ({}, None, True))
    monkeypatch.setattr(support, "_restore_exact_bytes", lambda _originals: (True, []))
    oracle_by_test = {mutation.tests[0]: mutation.expected_failures[0] for mutation in mutations}

    def fake_run(tests: tuple[str, ...], _timeout: int) -> subprocess.CompletedProcess[str]:
        if tests == baseline_tests:
            return subprocess.CompletedProcess(
                args=["pytest"], returncode=0, stdout="20 passed in 0.01s\n", stderr=""
            )
        test_node = tests[0]
        oracle = oracle_by_test[test_node]
        return subprocess.CompletedProcess(
            args=["pytest"],
            returncode=1,
            stdout=f"FAILED {test_node} - AssertionError: {oracle}\n1 failed in 0.01s\n",
            stderr="",
        )

    monkeypatch.setattr(support, "_run_audited_tests", fake_run)


def test_campaign_catalog_is_auditable_and_all_fourteen_live_anchors_are_unique() -> None:
    campaign = _catalog()
    mutations = campaign["MUTATIONS"]
    expected_ids = campaign["EXPECTED_IDS"]

    assert expected_ids == tuple("ABCDEFGHIJKLMN")
    assert tuple(mutation.mutation_id for mutation in mutations) == expected_ids
    assert len({mutation.name for mutation in mutations}) == 14
    for mutation in mutations:
        assert mutation.edits, mutation.mutation_id
        assert len(mutation.tests) == 1, mutation.mutation_id
        assert "::test_" in mutation.tests[0], mutation.mutation_id
        assert mutation.expected_failures, mutation.mutation_id
        assert mutation.rationale.strip(), mutation.mutation_id
        for edit in mutation.edits:
            target = (REPO / edit.path).resolve()
            assert target.is_relative_to(REPO), f"{mutation.mutation_id}: target escapes repo"
            assert edit.old != edit.new, f"{mutation.mutation_id}: no-op edit"
            assert target.read_bytes().count(edit.old.encode("utf-8")) == 1, (
                f"{mutation.mutation_id}: anchor in {edit.path} must occur exactly once"
            )


def test_reported_runner_accepts_only_fourteen_oracle_confirmed_mutations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    support = mutation_support
    mutations = _synthetic_mutations(support)
    expected_ids = tuple("ABCDEFGHIJKLMN")
    baseline_tests = ("tests/synthetic/test_baseline.py::test_baseline",)
    _install_fake_successful_environment(monkeypatch, support, mutations, baseline_tests)

    first = tmp_path / "first.json"
    exit_code = support.run_reported_mutation_suite(
        "api-gate-mutations",
        baseline_tests,
        mutations,
        expected_ids,
        first,
    )
    second = tmp_path / "second.json"
    second_exit = support.run_reported_mutation_suite(
        "api-gate-mutations",
        baseline_tests,
        mutations,
        expected_ids,
        second,
    )

    report = json.loads(first.read_text(encoding="utf-8"))
    assert exit_code == second_exit == 0
    assert report["accepted"] is True
    assert report["summary"] == {
        "planned": 14,
        "executed": 14,
        "detected": 14,
        "survived": 0,
        "invalid": 0,
        "not_run": 0,
    }
    assert all(row["status"] == "detected" for row in report["mutations"])
    assert report["provenance"]["git_commit"]
    assert report["provenance"]["commit_binding_valid"] is True
    assert first.read_bytes() == second.read_bytes(), "report must be byte-stable on rerun"


def test_one_surviving_mutant_makes_the_runner_nonzero_and_is_not_a_kill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    support = mutation_support
    mutation = _synthetic_mutations(support, ("A",))[0]
    baseline_tests = ("tests/synthetic/test_baseline.py::test_baseline",)
    _install_fake_successful_environment(monkeypatch, support, (mutation,), baseline_tests)
    original_run = support._run_audited_tests

    def one_survivor(tests: tuple[str, ...], timeout: int) -> subprocess.CompletedProcess[str]:
        if tests == mutation.tests:
            return subprocess.CompletedProcess(
                args=["pytest"], returncode=0, stdout="1 passed in 0.01s\n", stderr=""
            )
        return original_run(tests, timeout)

    monkeypatch.setattr(support, "_run_audited_tests", one_survivor)
    report_path = tmp_path / "survivor.json"
    exit_code = support.run_reported_mutation_suite(
        "api-gate-mutations", baseline_tests, (mutation,), ("A",), report_path
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert exit_code != 0
    assert report["mutations"][0]["status"] == "survived"
    assert report["summary"] == {
        "planned": 1,
        "executed": 1,
        "detected": 0,
        "survived": 1,
        "invalid": 0,
        "not_run": 0,
    }
    assert report["accepted"] is False


@pytest.mark.parametrize(
    ("error_message", "expected_kind"),
    [
        ("ImportError: oracle-A", "import_error"),
        ("OperationalError: database is locked oracle-A", "database_error"),
        ("fixture 'db' setup failed oracle-A", "fixture_error"),
    ],
)
def test_infrastructure_failures_are_invalid_not_successfully_detected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_message: str,
    expected_kind: str,
) -> None:
    support = mutation_support
    mutation = _synthetic_mutations(support, ("A",))[0]
    baseline_tests = ("tests/synthetic/test_baseline.py::test_baseline",)
    _install_fake_successful_environment(monkeypatch, support, (mutation,), baseline_tests)
    original_run = support._run_audited_tests

    def infrastructure_failure(
        tests: tuple[str, ...], timeout: int
    ) -> subprocess.CompletedProcess[str]:
        if tests == mutation.tests:
            # Even including the declared marker in an infrastructure error
            # cannot convert it into a mutation kill.
            return subprocess.CompletedProcess(
                args=["pytest"],
                returncode=1,
                stdout=f"ERROR {tests[0]} - {error_message}\n1 error in 0.01s\n",
                stderr="",
            )
        return original_run(tests, timeout)

    monkeypatch.setattr(support, "_run_audited_tests", infrastructure_failure)
    report_path = tmp_path / "infrastructure.json"
    exit_code = support.run_reported_mutation_suite(
        "api-gate-mutations", baseline_tests, (mutation,), ("A",), report_path
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert exit_code != 0
    assert report["mutations"][0]["status"] == "invalid"
    assert report["mutations"][0]["observed_errors"] == [mutation.tests[0]]
    assert report["mutations"][0]["failure_kind"] == expected_kind
    assert report["mutations"][0]["infrastructure_failure_kind"] == expected_kind
    assert report["summary"]["detected"] == 0
    assert report["summary"]["invalid"] == 1
    assert report["accepted"] is False


def test_timeout_is_invalid_not_successfully_detected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    support = mutation_support
    mutation = _synthetic_mutations(support, ("A",))[0]
    baseline_tests = ("tests/synthetic/test_baseline.py::test_baseline",)
    _install_fake_successful_environment(monkeypatch, support, (mutation,), baseline_tests)
    original_run = support._run_audited_tests

    def timed_out(tests: tuple[str, ...], timeout: int) -> subprocess.CompletedProcess[str]:
        if tests == mutation.tests:
            raise subprocess.TimeoutExpired(cmd=["pytest", *tests], timeout=timeout)
        return original_run(tests, timeout)

    monkeypatch.setattr(support, "_run_audited_tests", timed_out)
    report_path = tmp_path / "timeout.json"
    exit_code = support.run_reported_mutation_suite(
        "api-gate-mutations", baseline_tests, (mutation,), ("A",), report_path
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    row = report["mutations"][0]
    assert exit_code != 0
    assert row["status"] == "invalid"
    assert row["test_executed"] is True
    assert row["failure_kind"] == "timeout"
    assert report["summary"]["detected"] == 0


def test_failed_baseline_marks_every_mutation_not_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    support = mutation_support
    mutations = _synthetic_mutations(support, ("A",))
    baseline_tests = ("tests/synthetic/test_baseline.py::test_baseline",)
    monkeypatch.delenv("GITHUB_SHA", raising=False)
    monkeypatch.setattr(support, "_worktree_clean", lambda: (True, []))
    apply_calls: list[str] = []
    monkeypatch.setattr(
        support,
        "_apply_exact_mutation",
        lambda mutation: apply_calls.append(mutation.name) or ({}, None, True),
    )
    monkeypatch.setattr(support, "_restore_exact_bytes", lambda _originals: (True, []))
    monkeypatch.setattr(
        support,
        "_run_audited_tests",
        lambda _tests, _timeout: subprocess.CompletedProcess(
            args=["pytest"],
            returncode=1,
            stdout="ERROR tests/synthetic/test_baseline.py::test_baseline - ImportError\n",
            stderr="",
        ),
    )
    report_path = tmp_path / "baseline-failure.json"
    exit_code = support.run_reported_mutation_suite(
        "api-gate-mutations", baseline_tests, mutations, ("A",), report_path
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert exit_code != 0
    assert apply_calls == []
    assert report["baseline"]["status"] == "invalid_import_error"
    assert report["baseline"]["failure_kind"] == "import_error"
    assert report["summary"]["executed"] == 0
    assert report["summary"]["not_run"] == 1
    assert report["mutations"][0]["status"] == "not_run"


def test_anchor_drift_is_invalid_and_not_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    support = mutation_support
    mutation = support.Mutation(
        name="anchor_drift",
        edits=(support.SourceEdit("src/missing_probe.py", "old", "new"),),
        tests=("tests/synthetic/test_guard.py::test_guard",),
        expected_failures=("oracle-A",),
        rationale="anchor drift must fail closed",
        mutation_id="A",
    )
    baseline_tests = ("tests/synthetic/test_baseline.py::test_baseline",)
    monkeypatch.delenv("GITHUB_SHA", raising=False)
    monkeypatch.setattr(support, "_worktree_clean", lambda: (True, []))
    calls: list[tuple[str, ...]] = []

    def baseline_only(tests: tuple[str, ...], _timeout: int) -> subprocess.CompletedProcess[str]:
        calls.append(tests)
        return subprocess.CompletedProcess(
            args=["pytest"], returncode=0, stdout="1 passed in 0.01s\n", stderr=""
        )

    monkeypatch.setattr(support, "_run_audited_tests", baseline_only)
    report_path = tmp_path / "anchor-drift.json"
    exit_code = support.run_reported_mutation_suite(
        "api-gate-mutations", baseline_tests, (mutation,), ("A",), report_path
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    row = report["mutations"][0]
    assert exit_code != 0
    assert calls == [baseline_tests, baseline_tests]
    assert row["status"] == "invalid"
    assert row["test_executed"] is False
    assert row["failure_kind"] == "invalid_source_edit"
    assert report["summary"]["invalid"] == 1
    assert report["summary"]["not_run"] == 1


def test_restore_uncertainty_stops_the_campaign_and_is_never_a_kill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    support = mutation_support
    mutations = _synthetic_mutations(support, ("A", "B"))
    baseline_tests = ("tests/synthetic/test_baseline.py::test_baseline",)
    _install_fake_successful_environment(monkeypatch, support, mutations, baseline_tests)
    monkeypatch.setattr(
        support,
        "_restore_exact_bytes",
        lambda _originals: (False, ["simulated restore verification failure"]),
    )
    report_path = tmp_path / "restore-failure.json"
    exit_code = support.run_reported_mutation_suite(
        "api-gate-mutations", baseline_tests, mutations, ("A", "B"), report_path
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    first, second = report["mutations"]
    assert exit_code == 3
    assert first["status"] == "invalid"
    assert first["failure_kind"] == "source_restoration_error"
    assert first["source_bytes_restored"] is False
    assert first["restore_errors"] == ["simulated restore verification failure"]
    assert second["status"] == "not_run"
    assert report["restored_baseline"]["status"] == "not_run"
    assert report["summary"]["detected"] == 0
    assert report["accepted"] is False


def test_dirty_worktree_cannot_produce_commit_bound_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    support = mutation_support
    mutation = _synthetic_mutations(support, ("A",))[0]
    baseline_tests = ("tests/synthetic/test_baseline.py::test_baseline",)
    monkeypatch.delenv("GITHUB_SHA", raising=False)
    monkeypatch.setattr(support, "_worktree_clean", lambda: (False, [" M src/changed.py"]))
    calls: list[bool] = []
    monkeypatch.setattr(
        support,
        "_run_audited_tests",
        lambda *_args: calls.append(True),
    )
    report_path = tmp_path / "dirty-worktree.json"
    exit_code = support.run_reported_mutation_suite(
        "api-gate-mutations", baseline_tests, (mutation,), ("A",), report_path
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert exit_code != 0
    assert calls == []
    assert report["provenance"]["worktree_clean"] is False
    assert report["provenance"]["commit_binding_valid"] is False
    assert report["mutations"][0]["status"] == "not_run"


def test_missing_definition_is_invalid_and_explicitly_not_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    support = mutation_support
    baseline_tests = ("tests/synthetic/test_baseline.py::test_baseline",)
    monkeypatch.delenv("GITHUB_SHA", raising=False)
    monkeypatch.setattr(support, "_worktree_clean", lambda: (True, []))
    monkeypatch.setattr(
        support,
        "_run_audited_tests",
        lambda *_args: subprocess.CompletedProcess(
            args=["pytest"], returncode=0, stdout="1 passed in 0.01s\n", stderr=""
        ),
    )
    report_path = tmp_path / "missing-definition.json"
    exit_code = support.run_reported_mutation_suite(
        "api-gate-mutations", baseline_tests, (), ("A",), report_path
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert exit_code != 0
    assert report["catalog"]["missing_ids"] == ["A"]
    assert report["mutations"][0]["status"] == "invalid"
    assert report["mutations"][0]["not_run"] is True
    assert report["summary"]["invalid"] == 1
    assert report["summary"]["not_run"] == 1


def test_report_path_inside_product_source_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    support = mutation_support
    monkeypatch.delenv("GITHUB_SHA", raising=False)
    called: list[bool] = []
    monkeypatch.setattr(support, "_run_audited_tests", lambda *_args: called.append(True))
    target = REPO / "src" / "api-gate-evidence.json"
    exit_code = support.run_reported_mutation_suite("api-gate-mutations", (), (), (), target)
    assert exit_code != 0
    assert called == []
    assert not target.exists()


def test_ci_job_is_least_privilege_repeatable_and_uploads_evidence_on_failure() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    job = workflow["jobs"]["api-gate-mutations"]
    steps = job["steps"]
    commands = [step.get("run", "") for step in steps]
    serialized = yaml.safe_dump(job)

    assert workflow["permissions"] == {"contents": "read"}
    assert job["timeout-minutes"] <= 30
    assert any(step.get("with", {}).get("python-version") == "3.12" for step in steps)
    assert any('pip install -e ".[dev]"' in command for command in commands)
    assert any("scripts/api_gate_mutations.py" in command for command in commands)
    assert any("cmp ci-artifacts/api-gate-mutations.json" in command for command in commands)
    assert any('"worktree_clean"' in command for command in commands)
    assert "secrets." not in serialized
    upload = next(step for step in steps if "upload-artifact" in step.get("uses", ""))
    assert upload.get("if") == "always()"
    assert "github.sha" in upload["with"]["name"]
    assert "ci-artifacts/" in upload["with"]["path"]
    source_check = next(
        command for command in commands if "git diff --exit-code HEAD -- src/" in command
    )
    assert '"--porcelain=v1"' in source_check
