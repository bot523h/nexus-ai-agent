"""Law R18 — a main-bound merge requires ``base == main`` (incident #143).

The #143 incident: PR #143 was opened with base
``arena/01a0f986-nexus-ai-agent`` (a feature branch) and merged, producing
``d7c463e``.  GitHub reported the PR ``MERGEABLE``/``CLEAN`` because the merge
was valid *against its own wrong base*, so the mistake was invisible to review
and to every existing gate.  ``scripts/merge_base_guard.py`` makes the base a
mechanical, fail-closed precondition.

This file drives the guard as a black box through ``subprocess`` — exactly how
CI runs it — because ``scripts/`` is not an installed package and must stay
executable with the bare interpreter (``test_scripts_import_boundary.py``).
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parents[2]
SCRIPT = REPO_ROOT / "scripts" / "merge_base_guard.py"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

_FORBIDDEN_IMPORT_ROOTS = {"typer", "pydantic", "httpx", "yaml", "requests", "click"}


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )


# --------------------------------------------------------------------------- #
# the decision (no network)
# --------------------------------------------------------------------------- #
def test_main_base_passes() -> None:
    proc = _run("check", "--base", "main")
    assert proc.returncode == 0, proc.stderr
    assert "pass" in proc.stdout


def test_a_feature_branch_base_fails_closed() -> None:
    """The exact #143 shape: a feature branch as base must be a red gate."""
    proc = _run("check", "--base", "arena/01a0f986-nexus-ai-agent")
    assert proc.returncode == 1, proc.stdout
    assert "#143" in proc.stderr


def test_case_variant_is_not_main() -> None:
    """Matching is exact: 'MAIN' is not the main branch."""
    proc = _run("check", "--base", "MAIN")
    assert proc.returncode == 1, proc.stdout


def test_arbitrary_other_branch_fails() -> None:
    proc = _run("check", "--base", "develop")
    assert proc.returncode == 1, proc.stdout


# --------------------------------------------------------------------------- #
# the CI entry point (check-event)
# --------------------------------------------------------------------------- #
def test_non_pr_event_without_a_base_passes() -> None:
    proc = _run("check-event", "--event", "push")
    assert proc.returncode == 0, proc.stderr


def test_schedule_event_passes() -> None:
    proc = _run("check-event", "--event", "schedule")
    assert proc.returncode == 0, proc.stderr


def test_pull_request_event_on_main_passes() -> None:
    proc = _run("check-event", "--event", "pull_request", "--base", "main")
    assert proc.returncode == 0, proc.stderr


def test_pull_request_event_on_other_base_fails() -> None:
    proc = _run("check-event", "--event", "pull_request", "--base", "release/3.13")
    assert proc.returncode == 1, proc.stdout


def test_pull_request_event_without_a_base_is_unreadable_not_pass() -> None:
    """A pull_request event always carries a base; a missing one is exit 2, never 0."""
    proc = _run("check-event", "--event", "pull_request")
    assert proc.returncode == 2, proc.stdout


# --------------------------------------------------------------------------- #
# the guard cannot rot silently
# --------------------------------------------------------------------------- #
def test_guard_is_pure_stdlib() -> None:
    """It runs before any install, so a third-party import is a defect."""
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    offenders = sorted(roots & _FORBIDDEN_IMPORT_ROOTS)
    assert not offenders, f"merge_base_guard.py must stay stdlib-only, found {offenders}"


def test_guard_is_wired_into_ci() -> None:
    """A guard CI never runs is not a guard (this workflow ships with every PR)."""
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    assert "scripts/merge_base_guard.py" in workflow, (
        "ci.yml does not run the merge-base guard — the #143 wrong-base merge "
        "would again be invisible"
    )


def test_guard_names_the_incident_it_closes() -> None:
    """Evidence discipline: the guard states why it exists, with the incident id."""
    assert "#143" in SCRIPT.read_text(encoding="utf-8")
