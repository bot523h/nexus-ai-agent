"""Unit tests for the living-truth doctor.

Every check is exercised in both directions on a synthetic tree:

* a clean tree produces **no** finding (no false positives), and
* an injected defect produces the exact finding code with a witness.

The final test runs the real repository, so a genuine drift in *this* tree (a
version disagreement, an unindexed document, a law pointing at a deleted test, a
stale ``truth:`` marker) fails here as well as in the architecture gate.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from nexus_ai_agent.diagnostics import truth

REPO_ROOT = Path(__file__).resolve().parents[2]


def _clean_tree(root: Path) -> None:
    """A minimal repository that satisfies every check."""
    (root / "src" / "pkg").mkdir(parents=True)
    (root / "tests" / "unit").mkdir(parents=True)
    (root / "docs" / "architecture").mkdir(parents=True)

    (root / "VERSION").write_text("1.2.3\n", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        '[project]\nname = "x"\nversion = "1.2.3"\n\n[tool.other]\nversion = "9.9.9"\n',
        encoding="utf-8",
    )
    (root / "CHANGELOG.md").write_text(
        "## [Unreleased]\n\n## [1.2.3] - 2026-01-01\n", encoding="utf-8"
    )
    (root / "README.md").write_text("> **Version: v1.2.3**\n", encoding="utf-8")

    (root / "docs" / "README.md").write_text("[a](architecture/MODULE_MAP.md)\n", encoding="utf-8")
    (root / "docs" / "architecture" / "MODULE_MAP.md").write_text(
        "## 3 — laws\n\n| Law | Rule | Test |\n|---|---|---|\n"
        "| R1 | something | `test_guard.py` |\n\n**Legacy baseline.**\n",
        encoding="utf-8",
    )
    (root / "tests" / "unit" / "test_guard.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8"
    )
    (root / "src" / "pkg" / "mod.py").write_text("def present():\n    return 1\n", encoding="utf-8")


def _codes(findings: tuple[truth.Finding, ...]) -> set[str]:
    return {f.code for f in findings}


# --------------------------------------------------------------------------- #
# version lock-step
# --------------------------------------------------------------------------- #
def test_clean_tree_has_no_findings(tmp_path: Path) -> None:
    _clean_tree(tmp_path)
    report = truth.run_doctor(tmp_path)
    assert report.findings == (), report.format_text()
    assert report.exit_code() == 0


def test_version_drift_is_reported_with_values(tmp_path: Path) -> None:
    _clean_tree(tmp_path)
    (tmp_path / "README.md").write_text("> **Version: v9.9.9**\n", encoding="utf-8")
    report = truth.run_doctor(tmp_path, only=["version-lockstep"])
    assert _codes(report.findings) == {"TRUTH003"}
    assert "README.md=9.9.9" in report.findings[0].evidence


def test_missing_version_file_is_reported(tmp_path: Path) -> None:
    _clean_tree(tmp_path)
    (tmp_path / "VERSION").unlink()
    report = truth.run_doctor(tmp_path, only=["version-lockstep"])
    assert "TRUTH001" in _codes(report.findings)


def test_non_semver_declaration_is_reported(tmp_path: Path) -> None:
    _clean_tree(tmp_path)
    (tmp_path / "VERSION").write_text("1.2\n", encoding="utf-8")
    report = truth.run_doctor(tmp_path, only=["version-lockstep"])
    assert "TRUTH002" in _codes(report.findings)


# --------------------------------------------------------------------------- #
# docs index
# --------------------------------------------------------------------------- #
def test_unindexed_document_is_reported(tmp_path: Path) -> None:
    _clean_tree(tmp_path)
    (tmp_path / "docs" / "orphan.md").write_text("hi\n", encoding="utf-8")
    report = truth.run_doctor(tmp_path, only=["docs-index"])
    assert "TRUTH010" in _codes(report.findings)


def test_dangling_index_link_is_reported(tmp_path: Path) -> None:
    _clean_tree(tmp_path)
    (tmp_path / "docs" / "README.md").write_text("[x](gone.md)\n", encoding="utf-8")
    report = truth.run_doctor(tmp_path, only=["docs-index"])
    assert "TRUTH011" in _codes(report.findings)


# --------------------------------------------------------------------------- #
# law -> test resolution
# --------------------------------------------------------------------------- #
def test_law_without_a_test_is_reported(tmp_path: Path) -> None:
    _clean_tree(tmp_path)
    law = tmp_path / "docs" / "architecture" / "MODULE_MAP.md"
    law.write_text(
        "## 3 — laws\n\n| Law | Rule | Test |\n|---|---|---|\n"
        "| R1 | something | prose only |\n\n**Legacy baseline.**\n",
        encoding="utf-8",
    )
    report = truth.run_doctor(tmp_path, only=["law-test-resolution"])
    assert "TRUTH021" in _codes(report.findings)


def test_law_naming_a_missing_test_file_is_reported(tmp_path: Path) -> None:
    _clean_tree(tmp_path)
    law = tmp_path / "docs" / "architecture" / "MODULE_MAP.md"
    law.write_text(
        "## 3 — laws\n\n| Law | Rule | Test |\n|---|---|---|\n"
        "| R1 | something | `test_deleted.py` |\n\n**Legacy baseline.**\n",
        encoding="utf-8",
    )
    report = truth.run_doctor(tmp_path, only=["law-test-resolution"])
    assert "TRUTH020" in _codes(report.findings)


def test_law_naming_a_missing_test_symbol_is_reported(tmp_path: Path) -> None:
    _clean_tree(tmp_path)
    law = tmp_path / "docs" / "architecture" / "MODULE_MAP.md"
    law.write_text(
        "## 3 — laws\n\n| Law | Rule | Test |\n|---|---|---|\n"
        "| R1 | something | `test_guard.py::test_renamed` |\n\n**Legacy baseline.**\n",
        encoding="utf-8",
    )
    report = truth.run_doctor(tmp_path, only=["law-test-resolution"])
    assert "TRUTH020" in _codes(report.findings)


def test_law_section_with_no_rows_is_reported(tmp_path: Path) -> None:
    _clean_tree(tmp_path)
    law = tmp_path / "docs" / "architecture" / "MODULE_MAP.md"
    law.write_text("## 3 — laws\n\nnothing here\n\n**Legacy baseline.**\n", encoding="utf-8")
    report = truth.run_doctor(tmp_path, only=["law-test-resolution"])
    assert "TRUTH022" in _codes(report.findings)


def test_valid_law_reference_resolves(tmp_path: Path) -> None:
    _clean_tree(tmp_path)
    report = truth.run_doctor(tmp_path, only=["law-test-resolution"])
    assert report.findings == ()


# --------------------------------------------------------------------------- #
# machine-readable claim witnesses
# --------------------------------------------------------------------------- #
def test_truth_claim_resolves(tmp_path: Path) -> None:
    _clean_tree(tmp_path)
    (tmp_path / "README.md").write_text(
        "> **Version: v1.2.3**\n<!-- truth:file=src/pkg/mod.py symbol=present -->\n",
        encoding="utf-8",
    )
    report = truth.run_doctor(tmp_path, only=["claim-witnesses"])
    assert report.findings == ()


def test_truth_claim_on_missing_symbol_is_reported(tmp_path: Path) -> None:
    _clean_tree(tmp_path)
    (tmp_path / "README.md").write_text(
        "> **Version: v1.2.3**\n<!-- truth:file=src/pkg/mod.py symbol=absent -->\n",
        encoding="utf-8",
    )
    report = truth.run_doctor(tmp_path, only=["claim-witnesses"])
    assert _codes(report.findings) == {"TRUTH023"}


def test_truth_absent_claim_on_existing_symbol_is_reported(tmp_path: Path) -> None:
    _clean_tree(tmp_path)
    (tmp_path / "README.md").write_text(
        "> **Version: v1.2.3**\n<!-- truth-absent:file=src/pkg/mod.py symbol=present -->\n",
        encoding="utf-8",
    )
    report = truth.run_doctor(tmp_path, only=["claim-witnesses"])
    assert _codes(report.findings) == {"TRUTH024"}


def test_truth_claim_on_missing_file_is_reported(tmp_path: Path) -> None:
    _clean_tree(tmp_path)
    (tmp_path / "README.md").write_text(
        "> **Version: v1.2.3**\n<!-- truth:file=src/pkg/gone.py symbol=x -->\n",
        encoding="utf-8",
    )
    report = truth.run_doctor(tmp_path, only=["claim-witnesses"])
    assert _codes(report.findings) == {"TRUTH023"}


# --------------------------------------------------------------------------- #
# fail-open default scan
# --------------------------------------------------------------------------- #
def test_new_truthy_default_is_flagged(tmp_path: Path) -> None:
    _clean_tree(tmp_path)
    (tmp_path / "src" / "pkg" / "mod.py").write_text(
        "def f(result):\n    return not result.get('safe', True)\n", encoding="utf-8"
    )
    report = truth.run_doctor(tmp_path, only=["fail-open-defaults"])
    assert _codes(report.findings) == {"TRUTH030"}


# --------------------------------------------------------------------------- #
# model invariants
# --------------------------------------------------------------------------- #
def test_finding_requires_evidence() -> None:
    with pytest.raises(ValueError, match="no evidence"):
        truth.Finding("TRUTHX", "error", "summary", "")


def test_finding_rejects_illegal_severity() -> None:
    with pytest.raises(ValueError, match="illegal severity"):
        truth.Finding("TRUTHX", "fatal", "summary", "witness")


def test_exit_code_thresholds() -> None:
    report = truth.TruthReport(
        root=Path("/x"),
        checks_run=("c",),
        findings=(
            truth.Finding("W", "warning", "w", "ev"),
            truth.Finding("E", "error", "e", "ev"),
        ),
    )
    assert report.exit_code(fail_on="error") == 1
    assert report.exit_code(fail_on="warning") == 1
    assert report.exit_code(fail_on="info") == 1
    only_warn = truth.TruthReport(
        root=Path("/x"), findings=(truth.Finding("W", "warning", "w", "ev"),)
    )
    assert only_warn.exit_code(fail_on="error") == 0
    assert only_warn.exit_code(fail_on="warning") == 1


def test_registry_is_not_empty_and_names_are_unique() -> None:
    names = [name for name, _ in truth.CHECKS]
    assert names, "the doctor must register at least one check"
    assert len(names) == len(set(names))


def test_every_registered_check_fires_on_its_own_defect(tmp_path: Path) -> None:
    """Vacuity guard: each check must be able to produce a finding."""
    _clean_tree(tmp_path)
    (tmp_path / "README.md").write_text("> **Version: v9.9.9**\n", encoding="utf-8")
    (tmp_path / "docs" / "orphan.md").write_text("x\n", encoding="utf-8")
    (tmp_path / "docs" / "architecture" / "MODULE_MAP.md").write_text(
        "## 3 — laws\n\n| Law | Rule | Test |\n|---|---|---|\n"
        "| R1 | x | none |\n\n**Legacy baseline.**\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "pkg" / "mod.py").write_text(
        "def f(r):\n    return r.get('success', True)\n", encoding="utf-8"
    )
    report = truth.run_doctor(tmp_path)
    fired = _codes(report.findings)
    assert {"TRUTH003", "TRUTH010", "TRUTH021", "TRUTH030"} <= fired, report.format_text()


# --------------------------------------------------------------------------- #
# CLI surface
# --------------------------------------------------------------------------- #
def test_cli_json_matches_report_model(tmp_path: Path) -> None:
    _clean_tree(tmp_path)
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "nexus_ai_agent.diagnostics.truth",
            "--root",
            str(tmp_path),
            "--format",
            "json",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["errors"] == 0
    assert payload["checks_run"] == [name for name, _ in truth.CHECKS]


def test_cli_exit_code_is_one_on_error(tmp_path: Path) -> None:
    _clean_tree(tmp_path)
    (tmp_path / "VERSION").unlink()
    proc = subprocess.run(
        [sys.executable, "-m", "nexus_ai_agent.diagnostics.truth", "--root", str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 1
    assert "TRUTH001" in proc.stdout


# --------------------------------------------------------------------------- #
# the real tree
# --------------------------------------------------------------------------- #
def test_real_repository_is_truthful() -> None:
    """This repository itself must pass the doctor (no drift, no stale claim)."""
    report = truth.run_doctor(REPO_ROOT)
    assert report.findings == (), report.format_text()
