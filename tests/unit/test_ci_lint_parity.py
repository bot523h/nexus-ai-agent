"""CI rail invariants — the fast rail must stay fast, pinned and independent (task-113).

Three promises the workflow makes to reviewers, each one turned into a mechanical check:

1. **one pinned ruff** — ``.pre-commit-config.yaml`` (``rev:``) and the workflow pin
   (``ruff==X.Y.Z``) must declare the same version, and ``pyproject.toml`` must still
   admit it.  Ruff's *format* output changes between minors, so a hook on 0.8 and a CI
   on 0.16 disagree about formatting: green locally, red in CI (or the reverse).  The
   parity helper is a pure function, and the mismatch path is proven on a fixture.
2. **the rail is fast** — ``lint-fast`` installs no package, so it can report lint and
   version drift in seconds instead of after ``pip install -e ".[dev]"``.
3. **lint cannot red the tests** — the ``test`` job declares no ``needs: lint``, and the
   fast rail depends on nothing.  This is the literal acceptance criterion of task-113.

No YAML parser is imported on purpose: the files are scanned as text, which keeps this
module running in the core-only install (the dev extra ships no YAML dependency).
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
PRECOMMIT = REPO_ROOT / ".pre-commit-config.yaml"
PYPROJECT = REPO_ROOT / "pyproject.toml"

FAST_RAIL_JOB = "lint-fast"
GATED_JOB = "test"

_JOB_HEADER = re.compile(r"^  (?P<job>[A-Za-z0-9_-]+):[ \t]*$", re.MULTILINE)
_RUFF_PIN = re.compile(r"ruff==([0-9][0-9.]*)")
_RUFF_LOWER_BOUND = re.compile(r"^\s*[\"']?ruff>=\s*([0-9][0-9.]*)", re.MULTILINE)
_RUFF_REPO = re.compile(r"astral-sh/ruff-pre-commit(?P<body>.*?)(?=\n  - repo:|\Z)", re.DOTALL)
_NEEDS_LINT = re.compile(r"^\s*needs:\s*(?:\[\s*)?.*\blint\b", re.MULTILINE)
_INSTALLS_THE_PACKAGE = re.compile(r"pip install[^\n]*\s-e\s|uv pip install[^\n]*\s-e\s")


# --------------------------------------------------------------------------- #
# text helpers (pure — the red-proofs below feed them fixtures)
# --------------------------------------------------------------------------- #
def job_block(workflow: str, job: str) -> str:
    """The body of one job: from its two-space ``job:`` header to the next header."""
    body: list[str] = []
    inside = False
    for line in workflow.splitlines():
        if _JOB_HEADER.match(line):
            if inside:
                break
            inside = line.strip() == f"{job}:"
            continue
        if inside:
            body.append(line)
    assert body, f"job {job!r} not found in the workflow"
    return "\n".join(body)


def needs_lint(workflow: str, job: str) -> bool:
    """True when *job* is gated on the ``lint`` job."""
    return bool(_NEEDS_LINT.search(job_block(workflow, job)))


def pinned_ruff_versions(workflow: str) -> set[str]:
    return set(_RUFF_PIN.findall(workflow))


def precommit_ruff_rev(config: str) -> str:
    """The ``rev:`` of the ruff-pre-commit repository, normalised without the ``v``."""
    repo = _RUFF_REPO.search(config)
    assert repo is not None, ".pre-commit-config.yaml no longer configures ruff-pre-commit"
    rev = re.search(r"^\s*rev:\s*v?([0-9][0-9.]*)", repo.group("body"), re.MULTILINE)
    assert rev is not None, "the ruff-pre-commit entry has no pinned rev"
    return rev.group(1)


def ruff_lower_bounds(pyproject: str) -> list[str]:
    """Every ``ruff>=X`` lower bound declared in pyproject.toml."""
    return _RUFF_LOWER_BOUND.findall(pyproject)


def _version_tuple(version: str) -> tuple[int, ...]:
    parts = tuple(int(part) for part in version.split("."))
    return parts + (0,) * (3 - len(parts))


def ruff_parity_problems(precommit: str, workflow: str, pyproject: str) -> list[str]:
    """Every disagreement between the hook revision, the CI pin and pyproject."""
    problems: list[str] = []
    rev = precommit_ruff_rev(precommit)
    pins = pinned_ruff_versions(workflow)
    if len(pins) != 1:
        problems.append(
            f"the workflow must pin exactly one ruff version, found {sorted(pins) or 'none'}"
        )
    for pin in sorted(pins):
        if pin != rev:
            problems.append(
                f"the workflow pins ruff=={pin} while .pre-commit-config.yaml pins v{rev}"
            )
        for bound in ruff_lower_bounds(pyproject):
            if _version_tuple(pin) < _version_tuple(bound):
                problems.append(
                    f"pyproject.toml requires ruff>={bound}, which the pin {pin} violates"
                )
    return problems


# --------------------------------------------------------------------------- #
# the repository itself
# --------------------------------------------------------------------------- #
def test_hook_and_ci_pin_the_same_ruff_version() -> None:
    problems = ruff_parity_problems(
        PRECOMMIT.read_text(encoding="utf-8"),
        WORKFLOW.read_text(encoding="utf-8"),
        PYPROJECT.read_text(encoding="utf-8"),
    )
    assert not problems, "ruff pin drift: " + "; ".join(problems)


def test_the_fast_rail_installs_no_package() -> None:
    """No ``pip install -e`` / ``uv pip install -e``: that is what makes it fast."""
    block = job_block(WORKFLOW.read_text(encoding="utf-8"), FAST_RAIL_JOB)
    assert not _INSTALLS_THE_PACKAGE.search(block), (
        f"{FAST_RAIL_JOB} must not install the project — it exists to report lint and version "
        "drift in seconds, before the expensive installs"
    )
    assert "ruff==" in block, f"{FAST_RAIL_JOB} must install the pinned ruff explicitly"


def test_the_fast_rail_runs_the_lockstep_script() -> None:
    block = job_block(WORKFLOW.read_text(encoding="utf-8"), FAST_RAIL_JOB)
    assert "scripts/check_version_lockstep.py" in block


def test_the_fast_rail_depends_on_nothing() -> None:
    block = job_block(WORKFLOW.read_text(encoding="utf-8"), FAST_RAIL_JOB)
    assert "needs:" not in block, f"{FAST_RAIL_JOB} must stay independent of the other jobs"


def test_the_test_job_is_not_gated_by_lint() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert not needs_lint(workflow, GATED_JOB), (
        f"the {GATED_JOB} job must not declare `needs: lint` (task-113): a broken-lint push "
        "has to report lint without turning the whole test suite red"
    )


# --------------------------------------------------------------------------- #
# red-proofs: the checks above must be able to fail
# --------------------------------------------------------------------------- #
def test_the_gate_check_is_red_on_a_gated_fixture() -> None:
    fixture = "jobs:\n  lint:\n    runs-on: ubuntu-latest\n  test:\n    needs: lint\n"
    assert needs_lint(fixture, "test")
    assert not needs_lint(fixture, "lint")


def test_the_gate_check_ignores_a_needs_of_another_job() -> None:
    fixture = "jobs:\n  lint:\n    runs-on: ubuntu-latest\n  test:\n    needs: migrate\n"
    assert not needs_lint(fixture, "test")


def test_the_parity_check_is_red_on_a_mismatched_fixture() -> None:
    precommit = "repos:\n  - repo: https://github.com/astral-sh/ruff-pre-commit\n    rev: v0.8.6\n"
    workflow = "jobs:\n  lint-fast:\n    steps:\n      - run: pip install ruff==0.16.8\n"
    problems = ruff_parity_problems(precommit, workflow, '  "ruff>=0.4",\n')
    assert problems and "v0.8.6" in problems[0]


def test_the_parity_check_is_red_when_the_workflow_pins_nothing() -> None:
    precommit = "repos:\n  - repo: https://github.com/astral-sh/ruff-pre-commit\n    rev: v0.16.8\n"
    workflow = "jobs:\n  lint-fast:\n    steps:\n      - run: ruff check .\n"
    problems = ruff_parity_problems(precommit, workflow, '  "ruff>=0.4",\n')
    assert problems and "exactly one ruff version" in problems[0]


def test_the_parity_check_is_red_on_a_floor_above_the_pin() -> None:
    precommit = "repos:\n  - repo: https://github.com/astral-sh/ruff-pre-commit\n    rev: v0.8.6\n"
    workflow = "jobs:\n  lint-fast:\n    steps:\n      - run: pip install ruff==0.8.6\n"
    problems = ruff_parity_problems(precommit, workflow, '  "ruff>=0.9",\n')
    assert problems and "requires ruff>=0.9" in problems[0]
