"""Law R19 — the GitHub enforcement plane must match the declared policy.

R18 (``scripts/merge_base_guard.py``) makes ``base == main`` a fail-closed
decision.  R19 makes the *enforcement of that decision* observable, because on
``main`` @6122c9b it was not:

* the three required status checks existed only in GitHub's settings, so
  un-requiring one left this whole suite green;
* ``test_merge_base_guard.py`` asserted only that the *string*
  ``scripts/merge_base_guard.py`` appears in ``ci.yml`` — and the workflow's own
  comment contains that string.  Deleting the ``run:`` step therefore left all
  12 R18 tests passing while the required check kept reporting green without
  deciding anything (reproduced: 12 passed with the invocation removed);
* a PR's base can be retargeted after it went green.  That is a
  ``pull_request``/``edited`` event, which GitHub's default activity types
  (``opened``/``synchronize``/``reopened``) do not include — so the old green
  check is reused against the new base and R18 is bypassed.

``scripts/governance_guard.py`` binds the three planes together.  These tests
drive it as a black box (subprocess, exactly as an operator would) and then
attack it: every mutation below must turn it red, and an unreadable source must
be BLOCKED (exit 2), never a pass.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).parents[2]
SCRIPT = REPO_ROOT / "scripts" / "governance_guard.py"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

EXIT_VERIFIED = 0
EXIT_VIOLATION = 1
EXIT_BLOCKED = 2


def _load_guard() -> ModuleType:
    """Load the guard by path (``scripts/`` is not an installed package).

    The module must be registered in ``sys.modules`` *before* it executes:
    ``@dataclass`` resolves each annotation through
    ``sys.modules[cls.__module__]``, so a path-loaded module that is not
    registered fails at class-creation time with an ``AttributeError`` on
    ``NoneType.__dict__``.
    """
    spec = importlib.util.spec_from_file_location("governance_guard_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)  # type: ignore[union-attr]
    except BaseException:
        sys.modules.pop(spec.name, None)
        raise
    return module


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )


@pytest.fixture()
def guard() -> Iterator[ModuleType]:
    module = _load_guard()
    yield module
    sys.modules.pop("governance_guard_under_test", None)


@pytest.fixture()
def workflow_root(tmp_path: Path) -> Path:
    """A temp repository root carrying a mutable copy of the real CI workflow."""
    target = tmp_path / ".github" / "workflows"
    target.mkdir(parents=True)
    shutil.copy(CI_WORKFLOW, target / "ci.yml")
    return tmp_path


def _mutate(root: Path, old: str, new: str) -> None:
    path = root / ".github" / "workflows" / "ci.yml"
    text = path.read_text(encoding="utf-8")
    assert old in text, f"mutation anchor not found: {old[:60]!r}"
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


# --------------------------------------------------------------------------- #
# the live repository is consistent
# --------------------------------------------------------------------------- #
def test_the_repository_plane_is_verified() -> None:
    proc = _run("check-offline")
    assert proc.returncode == EXIT_VERIFIED, proc.stdout + proc.stderr
    assert "verdict=VERIFIED" in proc.stdout


def test_the_declared_policy_is_the_governance_of_this_repository() -> None:
    """The declared contexts are the ones GitHub actually requires (live-verified
    2026-10-07, witness: .agents/evidence/GOVERNANCE_GITHUB_2026-10-07.json)."""
    proc = _run("plan", "--json")
    assert proc.returncode == EXIT_VERIFIED, proc.stderr
    plan = json.loads(proc.stdout)
    assert plan["required_contexts"] == [
        "lint (ruff + mypy + version lockstep)",
        'test (pytest -m "not slow")',
        "merge-base-guard (base == main)",
    ]
    assert "edited" in plan["base_changing_pr_types"]


def test_the_workflow_re_runs_when_a_pr_base_is_retargeted() -> None:
    """The retarget bypass, pinned at the source level too."""
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    assert "types: [opened, synchronize, reopened, edited, ready_for_review]" in workflow, (
        "pull_request without `edited` reuses the previous green merge-base check "
        "when a PR's base is retargeted (#143 shape)"
    )


def test_the_guard_is_pure_stdlib() -> None:
    """It runs before any install, so a third-party import is a defect."""
    import ast

    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    forbidden = {"typer", "pydantic", "httpx", "yaml", "requests", "click", "nexus_ai_agent"}
    offenders = sorted(roots & forbidden)
    assert not offenders, f"governance_guard.py must stay stdlib-only: {offenders}"


# --------------------------------------------------------------------------- #
# mutation attacks — each one must turn the guard red
# --------------------------------------------------------------------------- #
def test_deleting_the_guard_invocation_is_a_violation(workflow_root: Path) -> None:
    """The exact mutation that survived on main @6122c9b."""
    _mutate(
        workflow_root,
        """      - name: A main-bound merge requires base == main
        run: |
          python scripts/merge_base_guard.py check-event \\
            --event "${{ github.event_name }}" \\
            --base "${{ github.base_ref }}"
""",
        "",
    )
    proc = _run("check-offline", "--root", str(workflow_root))
    assert proc.returncode == EXIT_VIOLATION, proc.stdout
    assert "GOV020" in proc.stdout, "a job that never runs the guard is not a gate"


def test_dropping_the_base_argument_is_a_violation(workflow_root: Path) -> None:
    _mutate(workflow_root, '            --base "${{ github.base_ref }}"\n', "")
    proc = _run("check-offline", "--root", str(workflow_root))
    assert proc.returncode == EXIT_VIOLATION, proc.stdout
    assert "GOV021" in proc.stdout


def test_dropping_the_event_argument_is_a_violation(workflow_root: Path) -> None:
    _mutate(workflow_root, '            --event "${{ github.event_name }}" \\\n', "")
    proc = _run("check-offline", "--root", str(workflow_root))
    assert proc.returncode == EXIT_VIOLATION, proc.stdout
    assert "GOV021" in proc.stdout


def test_running_a_different_subcommand_is_a_violation(workflow_root: Path) -> None:
    """`check` alone decides on nothing CI knows; the event entry point must run."""
    _mutate(workflow_root, "merge_base_guard.py check-event", "merge_base_guard.py plan")
    proc = _run("check-offline", "--root", str(workflow_root))
    assert proc.returncode == EXIT_VIOLATION, proc.stdout
    assert "GOV020" in proc.stdout


def test_soft_failing_the_guard_job_is_a_violation(workflow_root: Path) -> None:
    _mutate(
        workflow_root,
        "  merge-base-guard:\n    name: merge-base-guard (base == main)\n",
        "  merge-base-guard:\n    name: merge-base-guard (base == main)\n"
        "    continue-on-error: true\n",
    )
    proc = _run("check-offline", "--root", str(workflow_root))
    assert proc.returncode == EXIT_VIOLATION, proc.stdout
    assert "GOV022" in proc.stdout


def test_renaming_the_guard_job_breaks_the_required_context(workflow_root: Path) -> None:
    _mutate(
        workflow_root,
        "    name: merge-base-guard (base == main)",
        "    name: merge-base-guard (base is main)",
    )
    proc = _run("check-offline", "--root", str(workflow_root))
    assert proc.returncode == EXIT_VIOLATION, proc.stdout
    assert "GOV010" in proc.stdout or "GOV011" in proc.stdout


def test_deleting_the_guard_job_entirely_is_a_violation(workflow_root: Path) -> None:
    path = workflow_root / ".github" / "workflows" / "ci.yml"
    text = path.read_text(encoding="utf-8")
    start = text.index("  merge-base-guard:\n")
    end = text.index("  # ── Release lineage")
    path.write_text(text[:start] + text[end:], encoding="utf-8")
    proc = _run("check-offline", "--root", str(workflow_root))
    assert proc.returncode == EXIT_VIOLATION, proc.stdout
    assert "GOV010" in proc.stdout


def test_dropping_the_retarget_event_type_is_a_violation(workflow_root: Path) -> None:
    _mutate(
        workflow_root,
        "types: [opened, synchronize, reopened, edited, ready_for_review]",
        "types: [opened, synchronize, reopened]",
    )
    proc = _run("check-offline", "--root", str(workflow_root))
    assert proc.returncode == EXIT_VIOLATION, proc.stdout
    assert "GOV030" in proc.stdout
    assert "edited" in proc.stdout


def test_reverting_to_the_flow_form_trigger_is_a_violation(workflow_root: Path) -> None:
    """`on: [push, pull_request]` silently means GitHub's default activity types."""
    _mutate(
        workflow_root,
        """on:
  push:
  pull_request:
    types: [opened, synchronize, reopened, edited, ready_for_review]
""",
        "on: [push, pull_request]\n",
    )
    proc = _run("check-offline", "--root", str(workflow_root))
    assert proc.returncode == EXIT_VIOLATION, proc.stdout
    assert "GOV030" in proc.stdout
    assert "GitHub defaults" in proc.stdout


def test_a_workflow_that_parses_to_no_jobs_is_a_violation(workflow_root: Path) -> None:
    """The reader must never report green on a file it did not understand."""
    (workflow_root / ".github" / "workflows" / "ci.yml").write_text(
        "name: CI\njobs: {}\n", encoding="utf-8"
    )
    proc = _run("check-offline", "--root", str(workflow_root))
    assert proc.returncode == EXIT_VIOLATION, proc.stdout
    assert "GOV002" in proc.stdout


def test_an_unreadable_workflow_is_blocked_not_green(tmp_path: Path) -> None:
    proc = _run("check-offline", "--root", str(tmp_path))
    assert proc.returncode == EXIT_BLOCKED, proc.stdout
    assert "GOV001" in proc.stdout


# --------------------------------------------------------------------------- #
# the live plane — an unreadable governance source is never a pass
# --------------------------------------------------------------------------- #
def _stub_api(monkeypatch: pytest.MonkeyPatch, guard: ModuleType, responses: dict[str, object]):
    def fake(url: str, token: str | None):
        for needle, value in responses.items():
            if url.endswith(needle):
                if isinstance(value, tuple):
                    return value
                return value, None, 200
        return None, "HTTP 404: Not Found", 404

    monkeypatch.setattr(guard, "_api_get", fake)


def test_an_unprotected_branch_is_a_violation(
    guard: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_api(
        monkeypatch,
        guard,
        {"branches/main": {"name": "main", "protected": False, "protection": {}}},
    )
    report = guard.check_live("o/r", "main", "token")
    assert report.verdict() == "VIOLATION"
    assert report.exit_code() == EXIT_VIOLATION
    assert any(f.code == "GOV040" for f in report.violations)


def test_a_missing_required_check_is_a_violation(
    guard: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact regression: someone un-requires the merge-base gate."""
    _stub_api(
        monkeypatch,
        guard,
        {
            "branches/main": {
                "name": "main",
                "protected": True,
                "protection": {
                    "required_status_checks": {
                        "contexts": [
                            "lint (ruff + mypy + version lockstep)",
                            'test (pytest -m "not slow")',
                        ],
                        "enforcement_level": "everyone",
                    }
                },
            }
        },
    )
    report = guard.check_live("o/r", "main", "token")
    assert report.verdict() == "VIOLATION"
    assert any("merge-base-guard" in f.message for f in report.violations)


def test_an_undeclared_live_context_is_drift_not_silence(
    guard: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_api(
        monkeypatch,
        guard,
        {
            "branches/main": {
                "name": "main",
                "protected": True,
                "protection": {
                    "required_status_checks": {
                        "contexts": [*guard.REQUIRED_CONTEXTS, "some-renamed-job"],
                        "enforcement_level": "everyone",
                    }
                },
            }
        },
    )
    report = guard.check_live("o/r", "main", "token")
    assert report.verdict() == "VIOLATION"
    assert any("undeclared" in f.message for f in report.violations)


def test_an_unreadable_branch_source_is_blocked(
    guard: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_api(monkeypatch, guard, {"branches/main": (None, "HTTP 403: Forbidden", 403)})
    report = guard.check_live("o/r", "main", "token")
    assert report.verdict() == "BLOCKED"
    assert report.exit_code() == EXIT_BLOCKED
    assert not report.violations, "an unreadable source must not be reported as a violation"


def test_an_unreadable_sub_setting_never_yields_verified(
    guard: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 2026-10-07 live shape: protection readable, sub-settings 403/404."""
    _stub_api(
        monkeypatch,
        guard,
        {
            "branches/main": {
                "name": "main",
                "protected": True,
                "protection": {
                    "required_status_checks": {
                        "contexts": list(guard.REQUIRED_CONTEXTS),
                        "enforcement_level": "everyone",
                    }
                },
            }
        },
    )
    report = guard.check_live("o/r", "main", "token")
    assert report.verdict() == "BLOCKED", "partial visibility is BLOCKED, never VERIFIED"
    assert {f.code for f in report.unknowns} == {
        "GOV050",
        "GOV051",
        "GOV052",
        "GOV053",
        "GOV054",
    }


def test_a_fully_readable_and_consistent_plane_is_verified(
    guard: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    responses: dict[str, object] = {
        "branches/main": {
            "name": "main",
            "protected": True,
            "protection": {
                "required_status_checks": {
                    "contexts": list(guard.REQUIRED_CONTEXTS),
                    "enforcement_level": "everyone",
                }
            },
        }
    }
    for endpoint, _code, _label in guard._DETAIL_ENDPOINTS:
        responses[f"protection/{endpoint}"] = {"enabled": True}
    _stub_api(monkeypatch, guard, responses)
    report = guard.check_live("o/r", "main", "token")
    assert report.verdict() == "VERIFIED", [f.to_dict() for f in report.findings]
    assert report.exit_code() == EXIT_VERIFIED


def test_partial_enforcement_is_a_violation(
    guard: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_api(
        monkeypatch,
        guard,
        {
            "branches/main": {
                "name": "main",
                "protected": True,
                "protection": {
                    "required_status_checks": {
                        "contexts": list(guard.REQUIRED_CONTEXTS),
                        "enforcement_level": "non_admins",
                    }
                },
            }
        },
    )
    report = guard.check_live("o/r", "main", "token")
    assert report.verdict() == "VIOLATION"
    assert any(f.code == "GOV042" for f in report.violations)


# --------------------------------------------------------------------------- #
# the workflow reader itself (it must not invent data)
# --------------------------------------------------------------------------- #
def test_the_reader_understands_both_trigger_forms(guard: ModuleType) -> None:
    flow = guard.parse_workflow("on: [push, pull_request]\njobs:\n  a:\n    runs-on: x\n")
    assert flow.triggers == {"push": [], "pull_request": []}
    assert list(flow.jobs) == ["a"]

    block = guard.parse_workflow(
        "on:\n  push:\n  pull_request:\n    types: [opened, edited]\njobs:\n  a:\n    runs-on: x\n"
    )
    assert block.triggers["pull_request"] == ["opened", "edited"]


def test_the_reader_captures_name_condition_and_soft_fail(guard: ModuleType) -> None:
    workflow = guard.parse_workflow(
        "jobs:\n"
        "  g:\n"
        "    name: gate (x)\n"
        "    if: github.event_name == 'pull_request'\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - run: |\n"
        "          python scripts/x.py check-event \\\n"
        '            --base "${{ github.base_ref }}"\n'
        "      - run: echo hi\n"
        "        continue-on-error: true\n"
    )
    job = workflow.jobs["g"]
    assert job.context == "gate (x)"
    assert job.condition == "github.event_name == 'pull_request'"
    assert job.soft_fail is True
    assert len(job.runs) == 2
    assert "scripts/x.py check-event" in job.runs[0]
    assert "--base" in job.runs[0]


def test_a_comment_cannot_satisfy_the_invocation_check(guard: ModuleType) -> None:
    """The vacuity defect, at the parser level: a comment is not a run step."""
    workflow = guard.parse_workflow(
        "jobs:\n"
        "  g:\n"
        "    name: gate\n"
        "    steps:\n"
        "      # python scripts/merge_base_guard.py check-event\n"
        "      - run: echo nothing\n"
    )
    body = "\n".join(workflow.jobs["g"].runs)
    assert "scripts/merge_base_guard.py" not in body


# --------------------------------------------------------------------------- #
# the durable live witness must agree with the declared policy
# --------------------------------------------------------------------------- #
def test_the_committed_live_witness_matches_the_declared_policy() -> None:
    witness = REPO_ROOT / ".agents" / "evidence" / "GOVERNANCE_GITHUB_2026-10-07.json"
    assert witness.is_file(), "the live GitHub snapshot must be committed as a dated witness"
    data = json.loads(witness.read_text(encoding="utf-8"))
    assert data["verdict"] in {"VERIFIED", "BLOCKED"}, data["verdict"]
    assert data["declared_required_contexts"] == [
        "lint (ruff + mypy + version lockstep)",
        'test (pytest -m "not slow")',
        "merge-base-guard (base == main)",
    ]
    assert data["live_required_contexts"] == data["declared_required_contexts"]
    assert data["protected"] is True
