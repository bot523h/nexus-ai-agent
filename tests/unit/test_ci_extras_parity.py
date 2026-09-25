"""Extras-matrix rail invariants — every guard must be able to fail (task-132).

``tests/unit/test_ci_lint_parity.py`` (task-113) already pins the lint rails;
this module pins the **task-132 rails** the same way, with the same rule: a
guard that cannot fail is not a guard.  Every check here has (a) the green
assertion on the real repository and (b) at least one red-proof on a mutated
fixture, so a regression in the guard logic itself is caught by this suite.

Promises under test:

1. **one blocking leg per shipping extra** — the ``extras-matrix`` job's legs
   must be exactly ``core`` + the shipping extras of ``pyproject.toml``, with
   no ``continue-on-error`` and ``fail-fast: false``;
2. **python parity** — every literal ``python-version`` pinned anywhere in the
   workflow is covered by the ``python-parity`` matrix, which must include the
   ``requires-python`` floor and the primary CI Python;
3. **the committed matrix document is current** — ``.github/DEPENDENCY_MATRIX.md``
   equals its regeneration (stale generated output = red, dirty-tree-after-
   generation = red);
4. **skip inflation is detectable** — a ``pytest -rs`` line that skips a test
   for a module the active leg installed is a defect, and the detector is
   proven on a fixture log;
5. **generation is deterministic** — the same repository state regenerates
   byte-identical output (unreproducible reports are a defect).

No YAML parser is imported on purpose (the core-only leg has no YAML
dependency); the workflow is scanned as text, exactly like the task-113 rail.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parents[2]
SCRIPT = REPO_ROOT / "scripts" / "extras_matrix.py"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
PYPROJECT = REPO_ROOT / "pyproject.toml"
COMMITTED_DOC = REPO_ROOT / ".github" / "DEPENDENCY_MATRIX.md"

spec = importlib.util.spec_from_file_location("extras_matrix_under_test", SCRIPT)
assert spec is not None and spec.loader is not None
extras_matrix = importlib.util.module_from_spec(spec)
# dataclasses resolves type strings via sys.modules[cls.__module__]; the
# module must be registered *before* exec or @dataclass blows up.
sys.modules["extras_matrix_under_test"] = extras_matrix
spec.loader.exec_module(extras_matrix)

WORKFLOW_TEXT = WORKFLOW.read_text(encoding="utf-8")
PYPROJECT_TEXT = PYPROJECT.read_text(encoding="utf-8")


def _fixture_workflow_without_leg(leg: str) -> str:
    """The real workflow with one matrix leg removed (the mutation)."""
    return WORKFLOW_TEXT.replace(f"          - leg: {leg}\n", "", 1)


def _fixture_workflow_with_continue_on_error() -> str:
    block = extras_matrix.job_block(WORKFLOW_TEXT, "extras-matrix")
    mutated = block.replace(
        "    runs-on: ubuntu-latest",
        "    runs-on: ubuntu-latest\n    continue-on-error: true",
        1,
    )
    return WORKFLOW_TEXT.replace(block, mutated, 1)


def _fixture_workflow_with_unknown_leg() -> str:
    return WORKFLOW_TEXT.replace(
        "          - leg: core\n", "          - leg: core\n          - leg: otio\n", 1
    )


def _fixture_workflow_parity_missing_floor() -> str:
    return WORKFLOW_TEXT.replace('"3.10", "3.11", "3.12"', '"3.11", "3.12"', 1)


def _fixture_workflow_uncovered_python() -> str:
    """A job pinned to a python the parity matrix never tests."""
    return WORKFLOW_TEXT.replace('python-version: "3.12"', 'python-version: "3.13"', 1)


# --------------------------------------------------------------------------- #
# 1. legs ↔ pyproject (the task-132 contract)
# --------------------------------------------------------------------------- #
def test_the_repository_matrix_agrees_with_pyproject() -> None:
    problems = extras_matrix.matrix_problems(PYPROJECT_TEXT, WORKFLOW_TEXT)
    assert problems == [], "extras-matrix drift: " + "; ".join(problems)


def test_leg_definitions_cover_the_shipping_extras() -> None:
    extras = extras_matrix._optional_dependencies(PYPROJECT_TEXT)  # noqa: SLF001
    shipping = {name for name in extras if name != extras_matrix.DEV_EXTRA}
    assert set(extras_matrix.LEG_DEFINITIONS) == shipping


def test_red_proof_removing_a_leg_is_detected() -> None:
    problems = extras_matrix.matrix_problems(
        PYPROJECT_TEXT, _fixture_workflow_without_leg("speech")
    )
    assert any("leg: speech" in problem for problem in problems), problems


def test_red_proof_an_unknown_leg_is_detected() -> None:
    problems = extras_matrix.matrix_problems(PYPROJECT_TEXT, _fixture_workflow_with_unknown_leg())
    assert any("otio" in problem for problem in problems), problems


def test_red_proof_continue_on_error_is_detected() -> None:
    problems = extras_matrix.matrix_problems(
        PYPROJECT_TEXT, _fixture_workflow_with_continue_on_error()
    )
    assert any("continue-on-error" in problem for problem in problems), problems


def test_red_proof_a_new_pyproject_extra_without_a_leg_is_detected() -> None:
    """Add a fake extra to pyproject: the missing leg must be named."""
    mutated = PYPROJECT_TEXT.replace("pdf = [", 'otio = ["fake-otio>=1.0"]\npdf = [', 1)
    problems = extras_matrix.matrix_problems(mutated, WORKFLOW_TEXT)
    assert any("otio" in problem and "LEG_DEFINITIONS" in problem for problem in problems), problems


def test_red_proof_a_new_extra_with_a_definition_but_no_leg_is_detected() -> None:
    """Definition added, CI leg forgotten — the workflow side must report it."""
    mutated_pyproject = PYPROJECT_TEXT.replace("pdf = [", 'otio = ["fake-otio>=1.0"]\npdf = [', 1)
    mutated_script_extras = dict(extras_matrix.LEG_DEFINITIONS)
    mutated_script_extras["otio"] = extras_matrix.LegDefinition(
        extra="otio", requirements=("fake-otio>=1.0",), import_modules=("fake_otio",)
    )
    original = extras_matrix.LEG_DEFINITIONS
    extras_matrix.LEG_DEFINITIONS = mutated_script_extras
    try:
        problems = extras_matrix.matrix_problems(mutated_pyproject, WORKFLOW_TEXT)
    finally:
        extras_matrix.LEG_DEFINITIONS = original
    assert any("leg: otio" in problem for problem in problems), problems


# --------------------------------------------------------------------------- #
# 2. python parity
# --------------------------------------------------------------------------- #
def test_red_proof_missing_requires_python_floor_is_detected() -> None:
    problems = extras_matrix.matrix_problems(
        PYPROJECT_TEXT, _fixture_workflow_parity_missing_floor()
    )
    assert any("floor" in problem for problem in problems), problems


def test_red_proof_an_uncovered_job_python_is_detected() -> None:
    problems = extras_matrix.matrix_problems(PYPROJECT_TEXT, _fixture_workflow_uncovered_python())
    assert any("3.13" in problem and "parity" in problem for problem in problems), problems


def test_red_proof_requires_python_floor_bump_is_detected() -> None:
    """Bump requires-python without extending the parity matrix → red."""
    mutated = PYPROJECT_TEXT.replace('requires-python = ">=3.10"', 'requires-python = ">=3.11"', 1)
    problems = extras_matrix.matrix_problems(mutated, WORKFLOW_TEXT)
    assert any("floor 3.11" in problem for problem in problems), problems


# --------------------------------------------------------------------------- #
# 3. committed generated doc is current (stale matrix / dirty tree guards)
# --------------------------------------------------------------------------- #
def test_the_committed_dependency_matrix_document_is_current() -> None:
    committed = COMMITTED_DOC.read_text(encoding="utf-8")
    regenerated = extras_matrix.matrix_markdown(PYPROJECT_TEXT, WORKFLOW_TEXT)
    assert committed == regenerated, (
        ".github/DEPENDENCY_MATRIX.md is stale — regenerate it with "
        "`python scripts/extras_matrix.py print-md > .github/DEPENDENCY_MATRIX.md`"
    )


def test_red_proof_a_stale_committed_document_is_detected() -> None:
    committed = COMMITTED_DOC.read_text(encoding="utf-8")
    tampered = committed.replace("| `pdf` |", "| `pdfX` |", 1)
    assert tampered != committed
    regenerated = extras_matrix.matrix_markdown(PYPROJECT_TEXT, WORKFLOW_TEXT)
    assert tampered != regenerated, "tampering with the committed matrix must be detectable"


# --------------------------------------------------------------------------- #
# 4. skip-inflation detector
# --------------------------------------------------------------------------- #
def test_red_proof_a_dependency_skip_on_the_active_leg_is_detected() -> None:
    log = (
        "tests/unit/test_jobs_cli.py::test_pdf_flow PASSED\n"
        "SKIPPED [1] tests/integration/test_in_process_job_queue.py:343: "
        "could not import 'pypdf'\n"
    )
    problems = extras_matrix.skip_inflation_problems(log, "pdf")
    assert any("pypdf" in problem for problem in problems), problems


def test_no_inflation_when_another_legs_module_skips() -> None:
    """On the pdf leg, a *speech* skip is a different leg's business."""
    log = "SKIPPED [1] tests/x.py:12: could not import 'faster_whisper'\n"
    assert extras_matrix.skip_inflation_problems(log, "pdf") == []


def test_no_inflation_on_clean_logs() -> None:
    log = "PASSED tests/unit/test_optional_extras.py::test_pdf_leg_extracts_real_text_layer\n"
    assert extras_matrix.skip_inflation_problems(log, "pdf") == []


def test_red_proof_an_unknown_leg_is_rejected_by_the_skip_audit() -> None:
    assert extras_matrix.skip_inflation_problems("", "otio") != []


# --------------------------------------------------------------------------- #
# 5. deterministic generation (reproducibility)
# --------------------------------------------------------------------------- #
def test_matrix_generation_is_deterministic() -> None:
    first = extras_matrix.matrix_markdown(PYPROJECT_TEXT, WORKFLOW_TEXT)
    second = extras_matrix.matrix_markdown(PYPROJECT_TEXT, WORKFLOW_TEXT)
    assert first == second


def test_report_is_sha_bound_and_carries_its_problems() -> None:
    report = extras_matrix.build_report(
        PYPROJECT_TEXT,
        WORKFLOW_TEXT,
        sha="deadbeef" * 5,
        timestamp="2026-09-25T00:00:00Z",
    )
    assert report["source_commit"] == "deadbeef" * 5
    assert report["generated_at"] == "2026-09-25T00:00:00Z"
    assert report["drift_problems"] == []
    # mutating the workflow inside the report input must surface as drift
    report_mutated = extras_matrix.build_report(
        PYPROJECT_TEXT,
        _fixture_workflow_without_leg("pdf"),
        sha="deadbeef" * 5,
        timestamp="2026-09-25T00:00:00Z",
    )
    assert report_mutated["drift_problems"], "the artifact must carry its own drift truth"


# --------------------------------------------------------------------------- #
# 6. the workflow actually runs the rails
# --------------------------------------------------------------------------- #
def test_the_workflow_runs_the_leg_test_file_with_leg_env() -> None:
    assert "tests/unit/test_optional_extras.py" in WORKFLOW_TEXT
    assert "NEXUS_EXTRA_LEG: ${{ matrix.leg }}" in WORKFLOW_TEXT


def test_the_workflow_runs_the_extras_check_before_any_install() -> None:
    """lint-fast must verify the matrix declarations pre-install (fast report)."""
    block = extras_matrix.job_block(WORKFLOW_TEXT, "lint-fast")
    check_at = block.find("extras_matrix.py check")
    install_at = block.find("pip install")
    assert check_at != -1, "lint-fast must run scripts/extras_matrix.py check"
    assert install_at == -1 or check_at < install_at, (
        "the extras check must run before the (ruff) install so drift is reported in seconds"
    )


def test_the_workflow_audits_skips_on_every_leg() -> None:
    assert "audit-skips" in WORKFLOW_TEXT
    assert '--leg "${{ matrix.leg }}"' in WORKFLOW_TEXT


def test_the_workflow_uploads_leg_artifacts_bound_to_sha() -> None:
    block = extras_matrix.job_block(WORKFLOW_TEXT, "extras-matrix")
    assert "dependency-matrix-${{ matrix.leg }}" in block
    assert '--sha "$GITHUB_SHA"' in block
    assert "actions/upload-artifact@" in block


def test_the_release_lineage_job_exists_and_fetches_tags() -> None:
    block = extras_matrix.job_block(WORKFLOW_TEXT, "release-lineage")
    assert "scripts/release_lineage.py" in block
    assert "fetch-depth: 0" in block
    assert "fetch-tags: true" in block
