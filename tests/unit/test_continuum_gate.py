"""Contract of the operational Continuum gate (``nexus_ai_agent.continuum.gate``).

The gate itself runs end to end in CI (job ``continuum-evidence``); these tests
pin the parts that decide its verdict so the gate cannot be weakened quietly:
the judging rule, the completeness of the attack catalogue, and fail-closed
setup on a checkout that cannot certify a commit.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from nexus_ai_agent.continuum import gate
from nexus_ai_agent.continuum.gate import CONTROL, SCENARIOS, Scenario, judge, run_gate


def _scenario(expected_exit: int = 1, fragment: str = "state loss detected") -> Scenario:
    return Scenario("SX", "attack", fragment, lambda _ws: None, expected_exit=expected_exit)


@pytest.mark.parametrize(
    ("observed_exit", "lines", "error", "passed"),
    [
        (1, ("✗ state loss detected: x",), None, True),
        (1, ("✗ test count mismatch",), None, False),  # right exit, wrong diagnosis
        (0, ("✗ state loss detected: x",), None, False),  # right text, green exit
        (2, ("✗ state loss detected: x",), None, False),  # usage error is not a rejection
        (None, (), "GateSetupError: boom", False),  # setup failure never passes
        (1, ("✗ state loss detected: x",), "late error", False),
    ],
)
def test_a_scenario_passes_only_on_exact_exit_and_diagnosis(
    observed_exit: int | None, lines: tuple[str, ...], error: str | None, passed: bool
) -> None:
    assert judge(_scenario(), observed_exit, lines, error).passed is passed


def test_the_control_must_exit_zero_with_the_success_line() -> None:
    assert CONTROL.expected_exit == 0
    assert judge(CONTROL, 0, ("✓ continuum snapshot matches checkout",), None).passed
    assert not judge(CONTROL, 1, ("✓ continuum snapshot matches checkout",), None).passed


def test_every_attack_expects_a_red_exit_and_ids_are_unique() -> None:
    ids = [scenario.id for scenario in SCENARIOS]
    assert len(ids) == len(set(ids)) and CONTROL.id not in ids
    assert all(scenario.expected_exit == 1 for scenario in SCENARIOS)
    assert all(scenario.expected_fragment for scenario in SCENARIOS)


REQUIRED_ATTACKS = {
    "stale": "unreachable",
    "dirty": "uncommitted edit",
    "untracked": "untracked importable",
    "later drift": "later committed source drift",
    "no git": "git executable unavailable",
    "shallow": "shallow clone",
    "unreachable commit": "does not exist",
    "test-count drift": "forged test count",
    "env drift": "forged environment",
    "malformed": "malformed JSON",
    "extra keys": "extra key",
    "missing keys": "missing key",
    "wrong types": "wrong type",
    "partial publish": "interrupted publication",
    "copied snapshot": "copied onto another history",
}


@pytest.mark.parametrize(("threat", "attack"), sorted(REQUIRED_ATTACKS.items()))
def test_the_catalogue_covers_every_required_threat(threat: str, attack: str) -> None:
    assert any(attack in scenario.attack for scenario in SCENARIOS), threat


def _git(root: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=a@b.invalid", "-c", "user.name=a", *arguments],
        cwd=root,
        check=True,
        capture_output=True,
    )


def test_gate_refuses_a_checkout_with_uncommitted_evidence(tmp_path: Path) -> None:
    root = tmp_path / "source"
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    (root / "src" / "a.py").write_text("x = 2\n", encoding="utf-8")

    report = run_gate(root, include_committed_status=False)
    assert report["passed"] is False
    assert report["scenarios"] == [] and report["control"] is None
    (problem,) = report["problems"]  # type: ignore[misc]
    assert "uncommitted evidence changes" in problem


def test_gate_refuses_a_directory_without_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    report = run_gate(tmp_path, include_committed_status=False)
    assert report["passed"] is False
    assert "cannot identify the commit under test" in report["problems"][0]  # type: ignore[index]


def test_gate_refuses_a_shallow_checkout(tmp_path: Path) -> None:
    origin = tmp_path / "origin"
    (origin / "src").mkdir(parents=True)
    for index in range(2):
        (origin / "src" / "a.py").write_text(f"x = {index}\n", encoding="utf-8")
        if index == 0:
            _git(origin, "init", "-q")
        _git(origin, "add", "-A")
        _git(origin, "commit", "-qm", f"c{index}")
    shallow = tmp_path / "shallow"
    subprocess.run(
        ["git", "clone", "-q", "--depth", "1", f"file://{origin}", str(shallow)], check=True
    )
    report = run_gate(shallow, include_committed_status=False)
    assert report["passed"] is False
    assert any("shallow" in problem for problem in report["problems"])  # type: ignore[union-attr]


def test_reports_are_canonical_json() -> None:
    report = {"b": 1, "a": [1, 2]}
    text = gate.canonical_report(report)
    assert text == json.dumps(report, indent=2, sort_keys=True) + "\n"


def test_summary_marks_every_row_and_the_committed_status() -> None:
    report = {
        "control": {"id": "S00", "passed": True, "observed_exit": 0, "attack": "control"},
        "scenarios": [{"id": "S01", "passed": False, "observed_exit": 0, "attack": "dirty"}],
        "committed_snapshot": {"status": "STALE"},
        "problems": ["attacks not rejected as specified: S01"],
    }
    assert gate.summary_lines(report) == [
        "PASS S00 exit=0 control",
        "FAIL S01 exit=0 dirty",
        "committed snapshot: STALE (blocking: false)",
    ]
    assert gate.report_problems(report) == ["attacks not rejected as specified: S01"]


def _clean_source(tmp_path: Path) -> Path:
    root = tmp_path / "source"
    (root / "src" / "nexus_ai_agent").mkdir(parents=True)
    (root / "src" / "nexus_ai_agent" / "__init__.py").write_text("", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    return root


GREEN = (0, ("✓ continuum snapshot matches checkout",))
REJECTED = (1, ("✗ state loss detected: recorded good commit x",))


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    outcomes: dict[str, tuple[int, tuple[str, ...]]],
    publish_exit: int = 0,
) -> None:
    """Replace only the two subprocess boundaries: publishing and the CLI run."""

    def publish(clone: Path) -> tuple[int, tuple[str, ...]]:
        (clone / ".nexus").mkdir(exist_ok=True)
        (clone / ".nexus" / "continuum.json").write_text("{}\n", encoding="utf-8")
        return publish_exit, ("✗ publish refused",) if publish_exit else ("✓ published",)

    def run_cli(
        root: Path, mode: str, *, extra_environment: object = None
    ) -> tuple[int, tuple[str, ...]]:
        return outcomes[mode]

    monkeypatch.setattr(gate, "_publish", publish)
    monkeypatch.setattr(
        gate, "_imports_from", lambda clone: (clone / "src" / "nexus_ai_agent").resolve()
    )
    monkeypatch.setattr(gate, "run_cli", run_cli)


def _attack(identifier: str) -> Scenario:
    return Scenario(
        identifier,
        f"attack {identifier}",
        "state loss detected",
        lambda _ws: gate.Invocation(mode=identifier),
    )


def test_run_gate_passes_only_when_the_control_and_every_attack_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _clean_source(tmp_path)
    _wire(monkeypatch, {"verify": GREEN, "SA": REJECTED, "SB": REJECTED})
    report = run_gate(
        source, scenarios=(_attack("SA"), _attack("SB")), include_committed_status=False
    )
    assert report["problems"] == []
    assert report["passed"] is True
    assert isinstance(report["published_commit"], str) and len(report["published_commit"]) == 40
    assert [row["passed"] for row in report["scenarios"]] == [True, True]  # type: ignore[union-attr,index]


@pytest.mark.parametrize(
    ("outcomes", "problem"),
    [
        (
            {"verify": GREEN, "SA": REJECTED, "SB": (1, ("✗ other",))},
            "not rejected as specified: SB",
        ),
        ({"verify": GREEN, "SA": REJECTED, "SB": GREEN}, "not rejected as specified: SB"),
        ({"verify": REJECTED, "SA": REJECTED, "SB": REJECTED}, "control failed"),
    ],
)
def test_run_gate_fails_when_any_row_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcomes: dict[str, tuple[int, tuple[str, ...]]],
    problem: str,
) -> None:
    source = _clean_source(tmp_path)
    _wire(monkeypatch, outcomes)
    report = run_gate(
        source, scenarios=(_attack("SA"), _attack("SB")), include_committed_status=False
    )
    assert report["passed"] is False
    assert any(problem in entry for entry in report["problems"])  # type: ignore[union-attr]


def test_run_gate_without_attacks_or_publication_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _clean_source(tmp_path)
    _wire(monkeypatch, {"verify": GREEN})
    empty = run_gate(source, scenarios=(), include_committed_status=False)
    assert empty["passed"] is False
    assert "no attack scenarios ran" in empty["problems"]  # type: ignore[operator]

    _wire(monkeypatch, {"verify": GREEN}, publish_exit=1)
    unpublished = run_gate(source, scenarios=(_attack("SA"),), include_committed_status=False)
    assert unpublished["passed"] is False
    assert "publishing the snapshot failed" in unpublished["problems"][0]  # type: ignore[index]


def test_run_gate_refuses_a_clone_that_imports_another_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _clean_source(tmp_path)
    _wire(monkeypatch, {"verify": GREEN, "SA": REJECTED})
    monkeypatch.setattr(gate, "_imports_from", lambda _clone: Path("/elsewhere/nexus_ai_agent"))
    report = run_gate(source, scenarios=(_attack("SA"),), include_committed_status=False)
    assert report["passed"] is False
    assert "would verify the wrong checkout" in report["problems"][0]  # type: ignore[index]
