"""Permanent gate: the repository must be able to prove its own truth (law R16).

The doctor in :mod:`nexus_ai_agent.diagnostics.truth` is only worth anything if
something *forces* it to run and if the doctor itself cannot silently degrade.
This gate provides both:

* it runs the doctor against the real tree and fails on any finding, so a version
  drift, an unindexed document, a boundary law pointing at a deleted test, or a
  stale ``truth:`` marker turns CI red;
* it enforces the doctor's own design contract — pure-stdlib imports (so it runs
  in the pre-install fast rail), a non-empty check registry, and a
  non-degenerate law check (removing the law table must produce a finding, not
  silence);
* it asserts the doctor is reachable as a runnable CLI with a documented exit
  contract.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

from nexus_ai_agent.diagnostics import truth

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCTOR = REPO_ROOT / "src" / "nexus_ai_agent" / "diagnostics" / "truth.py"

#: Third-party import roots the doctor must never use: it has to run before
#: ``pip install`` in the fast rail (see ``scripts/check_version_lockstep.py`` for
#: the same constraint on the version check).
_FORBIDDEN_IMPORT_ROOTS = {
    "typer",
    "pydantic",
    "httpx",
    "langgraph",
    "sqlmodel",
    "telegram",
    "pytest",
    "numpy",
}


def _import_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def test_doctor_is_pure_stdlib() -> None:
    """The doctor must not import anything that needs installation."""
    roots = _import_roots(DOCTOR)
    offenders = sorted(roots & _FORBIDDEN_IMPORT_ROOTS)
    assert not offenders, f"truth doctor must stay stdlib-only, found {offenders}"


def test_doctor_registry_is_non_empty_and_callable() -> None:
    assert truth.CHECKS, "the truth doctor registers no checks"
    names = [name for name, _ in truth.CHECKS]
    assert len(names) == len(set(names)), f"duplicate check names: {names}"
    for name, check in truth.CHECKS:
        assert callable(check), f"check {name!r} is not callable"


def test_real_repository_has_no_truth_findings() -> None:
    """The repository must pass its own doctor: no drift, no stale claim, no lie."""
    report = truth.run_doctor(REPO_ROOT)
    assert report.findings == (), report.format_text()


def test_law_check_is_not_a_no_op(tmp_path: Path) -> None:
    """Removing the MODULE_MAP law table must produce TRUTH020, not silence.

    Guards against the classic vacuity failure: a regex that quietly matches
    nothing would let every law reference rot unnoticed.
    """
    (tmp_path / "docs" / "architecture").mkdir(parents=True)
    report = truth.run_doctor(tmp_path, only=["law-test-resolution"])
    assert "TRUTH020" in {f.code for f in report.findings}


def test_doctor_reports_a_planted_version_drift(tmp_path: Path) -> None:
    """The gate's own evidence that the version check is live, not decorative."""
    (tmp_path / "VERSION").write_text("1.0.0\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "2.0.0"\n', encoding="utf-8")
    (tmp_path / "CHANGELOG.md").write_text("## [2.0.0] - x\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("> **Version: v2.0.0**\n", encoding="utf-8")
    report = truth.run_doctor(tmp_path, only=["version-lockstep"])
    assert "TRUTH003" in {f.code for f in report.findings}


@pytest.mark.parametrize("fail_on,expected", [("error", 0), ("warning", 0)])
def test_cli_exit_contract_on_clean_tree(fail_on: str, expected: int) -> None:
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "nexus_ai_agent.diagnostics.truth",
            "--root",
            str(REPO_ROOT),
            "--format",
            "json",
            "--fail-on",
            fail_on,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == expected, proc.stdout + proc.stderr


def test_law_r16_names_this_gate() -> None:
    """R16 (repo truth) must name its enforcing test, and this file must be it."""
    section = truth._module_map_section(REPO_ROOT)
    assert "R16" in section, "MODULE_MAP §3 is missing law R16 (repo truth)"
    row = next((ln for ln in section.splitlines() if ln.startswith("| R16 ")), "")
    assert "test_repo_truth_consistency.py" in row, (
        "law R16 must name tests/architecture/test_repo_truth_consistency.py"
    )
