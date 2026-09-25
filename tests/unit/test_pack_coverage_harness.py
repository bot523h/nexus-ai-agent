"""Wave 5 — tests for the dependency-free pack coverage harness.

The harness answers a question the repository could not answer before without a
third-party dependency: *which lines of the shipped capability packs has the
suite actually executed?*  These tests pin the parts a reviewer must be able to
trust:

* the denominator — :func:`executable_lines` must count what the compiler counts
  (statements, nested functions, comprehensions; never comments or docstrings);
* the verdict — :func:`coverage_failures` must fail a unit below the bar and name
  the worst modules, and must stay silent at or above it;
* the report — ``as_dict``/``format_table`` are the machine- and human-readable
  surfaces, so their shape is part of the contract;
* the CLI — ``--json``/``--threshold`` must translate a measurement into the
  documented exit codes, and the script must not import the unpackaged
  ``scripts`` namespace (the PR#40 lesson).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from nexus_ai_agent.continuum.pack_coverage import (
    CORE_GROUP,
    DEFAULT_PACK_ROOT,
    DEFAULT_TEST_TARGETS,
    DEFAULT_THRESHOLD,
    CoverageReport,
    ModuleCoverage,
    PackCoverage,
    coverage_failures,
    executable_lines,
    format_table,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "pack_coverage.py"


def _module(path: str, executed: int, executable: int) -> ModuleCoverage:
    return ModuleCoverage(path=path, executed=executed, executable=executable)


def _report(*packs: PackCoverage, threshold: float = DEFAULT_THRESHOLD) -> CoverageReport:
    return CoverageReport(packs=tuple(packs), threshold=threshold)


# ---------------------------------------------------------------------------
# the denominator
# ---------------------------------------------------------------------------


def test_executable_lines_counts_statements_not_comments_or_docstrings(tmp_path: Path) -> None:
    source = tmp_path / "sample.py"
    source.write_text(
        '"""Module docstring: not executable."""\n'
        "\n"
        "# a comment\n"
        "value = 1\n"
        "if value:\n"
        "    value += 1\n"
        "\n"
        "def helper(x):\n"
        '    """Docstring."""\n'
        "    return x + 1\n",
        encoding="utf-8",
    )
    lines = executable_lines(source)
    # CPython's line table for a module is 1-based for statements, but on 3.11+
    # a module prologue (RESUME) reports line 0 and a *module* docstring really
    # does compile to a store of ``__doc__`` — both are what a coverage tool
    # counts, and the harness must agree with the compiler rather than with
    # intuition.  On 3.10 there is no RESUME prologue, so line 0 never appears.
    expected = {0, 1, 4, 5, 6, 8, 10} if sys.version_info >= (3, 11) else {1, 4, 5, 6, 8, 10}
    assert lines == frozenset(expected), sorted(lines)
    assert 3 not in lines  # a comment never executes
    assert 7 not in lines  # a blank line never executes
    assert 9 not in lines  # a *function* docstring is constant-folded away


def test_executable_lines_descends_into_comprehensions(tmp_path: Path) -> None:
    source = tmp_path / "nested.py"
    source.write_text(
        "def double(values):\n    return [value * 2 for value in values if value]\n",
        encoding="utf-8",
    )
    lines = executable_lines(source)
    assert 2 in lines  # the comprehension's own line start


def test_executable_lines_on_the_real_packs_is_non_empty_and_bounded() -> None:
    for module in ("registry.py", "runtime.py", "verify.py"):
        lines = executable_lines(DEFAULT_PACK_ROOT / module)
        assert len(lines) > 50, module
        assert max(lines) <= len((DEFAULT_PACK_ROOT / module).read_text().splitlines())


def test_default_target_set_covers_every_pack_test_module() -> None:
    """A pack without a test target would silently report 0% coverage."""
    assert len(DEFAULT_TEST_TARGETS) >= 20
    joined = " ".join(DEFAULT_TEST_TARGETS)
    for pack in ("slideshow", "caption", "edit", "motion", "audio", "delivery"):
        assert pack in joined, f"no default test target mentions {pack}"
    assert all((REPO_ROOT / target).is_file() for target in DEFAULT_TEST_TARGETS)


# ---------------------------------------------------------------------------
# the verdict
# ---------------------------------------------------------------------------


def test_coverage_failures_is_silent_at_or_above_the_threshold() -> None:
    report = _report(
        PackCoverage(pack="motion", modules=(_module("motion/operations.py", 95, 100),)),
        PackCoverage(pack="audio", modules=(_module("audio/operations.py", 100, 100),)),
        threshold=95.0,
    )
    assert coverage_failures(report) == ()
    assert report.below_threshold is False


def test_coverage_failures_names_the_pack_and_its_worst_modules() -> None:
    report = _report(
        PackCoverage(
            pack="delivery",
            modules=(
                _module("delivery/signing.py", 60, 100),
                _module("delivery/operations.py", 90, 100),
                _module("delivery/__init__.py", 95, 100),
            ),
        ),
        threshold=95.0,
    )
    failures = coverage_failures(report)
    assert len(failures) == 1
    assert "'delivery': 81.67% < 95.00%" in failures[0]
    assert "delivery/signing.py 60.00%" in failures[0]


def test_report_aggregates_and_percentages() -> None:
    pack = PackCoverage(
        pack="core",
        modules=(_module("a.py", 50, 100), _module("b.py", 100, 100)),
    )
    report = _report(pack)
    assert (pack.executed, pack.executable, pack.percent) == (150, 200, 75.0)
    assert (report.executed, report.executable, report.percent) == (150, 200, 75.0)
    assert [m.path for m in pack.worst(2)] == ["a.py", "b.py"]


def test_zero_executable_module_is_full_coverage() -> None:
    """A module with nothing to run cannot drag a pack down."""
    assert _module("empty.py", 0, 0).percent == 100.0
    assert PackCoverage(pack="empty", modules=()).percent == 100.0


# ---------------------------------------------------------------------------
# the report surfaces
# ---------------------------------------------------------------------------


def test_as_dict_is_json_serialisable_and_keyed_for_ci() -> None:
    report = _report(
        PackCoverage(pack="edit", modules=(_module("edit/models.py", 9, 10),)),
        threshold=85.0,
    )
    payload = json.loads(json.dumps(report.as_dict()))
    assert set(payload) == {"threshold", "total", "pytest_exit_code", "tests", "packs"}
    assert payload["total"] == {"executed": 9, "executable": 10, "percent": 90.0}
    assert payload["packs"][0]["pack"] == "edit"
    assert payload["packs"][0]["modules"][0]["missing_lines"] == []  # not computed here


def test_format_table_marks_below_threshold_rows() -> None:
    report = _report(
        PackCoverage(pack="delivery", modules=(_module("delivery/signing.py", 6, 10),)),
        PackCoverage(pack="core", modules=(_module("core/registry.py", 10, 10),)),
        threshold=85.0,
    )
    table = format_table(report)
    lines = table.splitlines()
    assert lines[0].startswith("pack")
    assert any("delivery" in line and "BELOW" in line for line in lines)
    assert any("core" in line and "OK" in line for line in lines)
    assert any(line.startswith("TOTAL") for line in lines)
    assert f"threshold: {report.threshold:.2f}%" in table


def test_format_table_handles_an_empty_measurement() -> None:
    table = format_table(_report())
    assert "TOTAL" in table
    assert "100.00%" in table


# ---------------------------------------------------------------------------
# the CLI
# ---------------------------------------------------------------------------


def test_cli_list_tests_exits_zero_without_running_pytest() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--list-tests"],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO_ROOT,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == list(DEFAULT_TEST_TARGETS)


def test_cli_reports_below_threshold_with_exit_code_one() -> None:
    """One real measurement, one core module, an impossible bar: exit 1 + JSON."""
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--tests",
            "tests/unit/test_pack_runtime_composition.py",
            "--pack",
            CORE_GROUP,
            "--threshold",
            "100",
            "--json",
            "--quiet",
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO_ROOT,
    )
    assert result.returncode == 1, result.stderr
    payload = json.loads(result.stdout)
    assert payload["threshold"] == 100.0
    assert payload["pytest_exit_code"] == 0
    assert [pack["pack"] for pack in payload["packs"]] == [CORE_GROUP]
    assert 0 < payload["total"]["percent"] < 100
    assert "✗" in result.stderr
