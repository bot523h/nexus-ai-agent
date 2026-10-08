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
    """Stub ``_api_get`` with suffix matching.

    Needles are matched LONGEST FIRST: ``rules/branches/main`` also ends with
    ``branches/main``, so in dict order the branch fixture would otherwise swallow
    the ruleset request.  When a test does not stub the ruleset plane at all it
    defaults to ``[]`` — the live repository's state — so pre-GOV043 tests keep
    their verdicts; tests that care about rulesets stub it explicitly.
    """

    def fake(url: str, token: str | None):
        is_rules = url.endswith("rules/branches/main")
        for needle in sorted(responses, key=len, reverse=True):
            if needle == "branches/main" and is_rules:
                continue  # the ruleset URL is not the branch object
            if url.endswith(needle):
                value = responses[needle]
                if isinstance(value, tuple):
                    return value
                return value, None, 200
        if is_rules:
            return [], None, 200
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


#: The payload GitHub returns for a plane that genuinely enforces the policy.
_SECURE_DETAIL_PAYLOADS: dict[str, object] = {
    "protection/required_pull_request_reviews": {
        "required_approving_review_count": 1,
        "dismiss_stale_reviews": True,
    },
    "protection/required_conversation_resolution": {"enabled": True},
    "protection/allow_force_pushes": {"enabled": False},
    "protection/allow_deletions": {"enabled": False},
    "protection/required_status_checks": {"strict": True, "contexts": []},
}


def _plane(guard: ModuleType, overrides: dict[str, object]) -> dict[str, object]:
    """A readable live plane with selected sub-payloads replaced."""
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
    responses.update(_SECURE_DETAIL_PAYLOADS)
    responses.update(overrides)
    return responses


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
    # A *secure* plane.  The previous version of this fixture stubbed
    # {"enabled": True} for every endpoint — which for allow_force_pushes and
    # allow_deletions describes force-pushes and deletion as ALLOWED — and still
    # expected VERIFIED.  Readability was being mistaken for assurance.
    responses.update(_SECURE_DETAIL_PAYLOADS)
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


# --------------------------------------------------------------------------- #
# GOV023 — a condition that stops the gate running for pull requests
# --------------------------------------------------------------------------- #
# Reproduced on the pre-fix guard: every one of the mutations below reported
# GOV023=ok, including `if: false`.  The old check only fired when the condition
# *mentioned* pull_request and started with `always()`, so it rejected almost
# nothing — a required check that never runs leaves the merge pending while the
# guard reports VERIFIED.
GUARD_IF = "    if: github.event_name == 'pull_request'\n"


@pytest.mark.parametrize(
    ("replacement", "expected_exit"),
    [
        # 1. the condition disappears: the job runs on every subscribed event.
        ("", EXIT_VERIFIED),
        # 2. a constant-false condition never runs.
        ("    if: false\n", EXIT_VIOLATION),
        # 3. an event-exclusive condition that omits pull_request.
        ("    if: github.event_name == 'push'\n", EXIT_VIOLATION),
        # 4. an unrelated condition we cannot prove preserves pull_request.
        ("    if: vars.ENABLE_GUARD == 'yes'\n", EXIT_BLOCKED),
        # 5. pull_request mentioned, but only to be negated.
        ("    if: github.event_name != 'pull_request'\n", EXIT_VIOLATION),
        ("    if: \"!contains(github.event_name, 'pull_request')\"\n", EXIT_VIOLATION),
        # 6. a conjunction that really does exclude pull_request.
        ("    if: always() && false\n", EXIT_VIOLATION),
        # a condition that keeps pull_request alongside another event is fine.
        (
            "    if: github.event_name == 'pull_request' || github.event_name == 'push'\n",
            EXIT_VERIFIED,
        ),
        # always() keeps every subscribed event.
        ("    if: always()\n", EXIT_VERIFIED),
    ],
)
def test_gov023_classifies_the_pull_request_condition(
    workflow_root: Path, replacement: str, expected_exit: int
) -> None:
    _mutate(workflow_root, GUARD_IF, replacement)
    proc = _run("check-offline", "--root", str(workflow_root))
    assert proc.returncode == expected_exit, proc.stdout
    assert "GOV023" in proc.stdout, "the condition must be reported by GOV023 either way"
    if expected_exit == EXIT_VERIFIED:
        assert "[OK       ] GOV023" in proc.stdout, proc.stdout
    else:
        assert "[OK       ] GOV023" not in proc.stdout, (
            "a condition that stops the gate running must not be reported ok"
        )


def test_gov023_never_passes_a_condition_it_cannot_decide(guard: ModuleType) -> None:
    """The fail-closed direction, stated as a property rather than a case list.

    Any condition that neither names pull_request positively nor is empty/always
    must be `unknown` (BLOCKED) or a violation — never `ok`.
    """
    for condition in (
        "vars.ENABLE_GUARD == 'yes'",
        "github.event_name != 'push'",
        "github.ref == 'refs/heads/main'",
        "inputs.run_guard",
        "success() && github.actor == 'dependabot[bot]'",
    ):
        severity, _ = guard.classify_pr_condition(condition)
        assert severity != "ok", f"{condition!r} must not be reported ok"


def test_gov023_passes_every_condition_that_genuinely_runs_on_pull_request(
    guard: ModuleType,
) -> None:
    for condition in (
        "",
        "always()",
        "github.event_name == 'pull_request'",
        "github.event_name == 'pull_request' || github.event_name == 'push'",
        "contains(github.event_name, 'pull_request')",
        "success() && github.event_name == 'pull_request'",
    ):
        severity, _ = guard.classify_pr_condition(condition)
        assert severity == "ok", f"{condition!r} genuinely runs for pull_request"


def test_the_live_repository_condition_is_accepted() -> None:
    """The fix must not break the workflow this repository actually ships."""
    proc = _run("check-offline")
    assert proc.returncode == EXIT_VERIFIED, proc.stdout
    assert "GOV023" in proc.stdout


# --------------------------------------------------------------------------- #
# GOV050–GOV054 — readability is not assurance
# --------------------------------------------------------------------------- #
# Reproduced: with every endpoint answering HTTP 200, the pre-fix guard reported
# VERIFIED for a plane that allowed force-pushes, allowed deletion, required
# zero approvals and had strict=false.  Each case below must NOT be VERIFIED.
@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        # A readable plane that genuinely enforces the policy.
        ({}, "VERIFIED"),
        # Readable, but insecure.
        ({"protection/allow_force_pushes": {"enabled": True}}, "VIOLATION"),
        ({"protection/allow_deletions": {"enabled": True}}, "VIOLATION"),
        ({"protection/required_status_checks": {"strict": False}}, "VIOLATION"),
        (
            {
                "protection/required_pull_request_reviews": {
                    "required_approving_review_count": 0,
                    "dismiss_stale_reviews": True,
                }
            },
            "VIOLATION",
        ),
        (
            {
                "protection/required_pull_request_reviews": {
                    "required_approving_review_count": 2,
                    "dismiss_stale_reviews": False,
                }
            },
            "VIOLATION",
        ),
        ({"protection/required_conversation_resolution": {"enabled": False}}, "VIOLATION"),
        # Missing field: ambiguous, so BLOCKED — never a pass.
        ({"protection/allow_force_pushes": {}}, "BLOCKED"),
        # Null field: ambiguous.
        ({"protection/allow_deletions": {"enabled": None}}, "BLOCKED"),
        # Wrong type: a boolean is not an int, and `isinstance(True, int)` is
        # True in Python, so this must not be read as "1 approval".
        (
            {
                "protection/required_pull_request_reviews": {
                    "required_approving_review_count": True,
                    "dismiss_stale_reviews": True,
                }
            },
            "BLOCKED",
        ),
        ({"protection/allow_force_pushes": {"enabled": "false"}}, "BLOCKED"),
        # Partial payload: only some of the declared fields present.
        (
            {"protection/required_pull_request_reviews": {"required_approving_review_count": 1}},
            "BLOCKED",
        ),
        # Malformed shape: an object where GitHub sends an object.
        ({"protection/required_status_checks": ["strict"]}, "BLOCKED"),
    ],
)
def test_a_readable_but_insecure_plane_is_never_verified(
    guard: ModuleType, monkeypatch: pytest.MonkeyPatch, overrides: dict, expected: str
) -> None:
    _stub_api(monkeypatch, guard, _plane(guard, overrides))
    report = guard.check_live("o/r", "main", "token")
    assert report.verdict() == expected, [f.to_dict() for f in report.findings]


def test_an_all_200_insecure_plane_is_a_violation(
    guard: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact false-green this finding was about, as one fixture.

    Every endpoint answers HTTP 200 — nothing is unreadable — yet the policy is
    wide open.  Readability must not be mistaken for assurance.
    """
    _stub_api(
        monkeypatch,
        guard,
        _plane(
            guard,
            {
                "protection/required_pull_request_reviews": {
                    "required_approving_review_count": 0,
                    "dismiss_stale_reviews": False,
                },
                "protection/required_conversation_resolution": {"enabled": False},
                "protection/allow_force_pushes": {"enabled": True},
                "protection/allow_deletions": {"enabled": True},
                "protection/required_status_checks": {"strict": False, "contexts": []},
            },
        ),
    )
    report = guard.check_live("o/r", "main", "token")
    assert report.verdict() == "VIOLATION", [f.to_dict() for f in report.findings]
    assert report.exit_code() == EXIT_VIOLATION
    violated = {f.code for f in report.violations}
    assert violated == {"GOV050", "GOV051", "GOV052", "GOV053", "GOV054"}, violated


def test_every_declared_endpoint_has_value_semantics(guard: ModuleType) -> None:
    """No endpoint may be checked for readability alone."""
    for policy in guard._DETAIL_ENDPOINTS:
        assert policy.expectations, f"{policy.endpoint} is checked for readability only"
        for field_name, holds, expectation in policy.expectations:
            assert callable(holds)
            assert expectation, f"{policy.endpoint}.{field_name} has no stated expectation"


def test_gov023_catches_a_false_conjunct_on_either_side(guard: ModuleType) -> None:
    """`false && <names pull_request>` never runs, however it is spelled.

    The first version of this fix matched only `&& false`, so a leading
    `false &&` reached the positive branch and reported ok.
    """
    for condition in (
        "false && github.event_name == 'pull_request'",
        "github.event_name == 'pull_request' && false",
        "false",
        "false && always()",
    ):
        severity, _ = guard.classify_pr_condition(condition)
        assert severity == "violation", f"{condition!r} can never be true"


def test_gov023_does_not_trust_a_mention_under_an_unknown_negation(
    guard: ModuleType,
) -> None:
    """Naming pull_request is not evidence when a negation wraps it."""
    for condition in (
        "!(github.event_name == 'pull_request')",
        "! (github.event_name == 'pull_request')",
        "not (github.event_name == 'pull_request')",
    ):
        severity, _ = guard.classify_pr_condition(condition)
        assert severity != "ok", f"{condition!r} must not be reported ok"


def test_gov023_does_not_mistake_quoted_text_for_logic(guard: ModuleType) -> None:
    """`vars.X == 'false'` is data, not a constant-false condition."""
    severity, _ = guard.classify_pr_condition("vars.X == 'false'")
    assert severity != "violation", "a quoted 'false' is not a constant-false expression"


def test_every_markdown_table_row_in_the_governance_doc_is_well_formed() -> None:
    """A table row cannot continue onto the next line.

    The GOV023 row was written across four lines; the renderer ended the row at
    the first and the rest of the invariant was lost.
    """
    lines = (
        Path(REPO_ROOT, "docs/architecture/GOVERNANCE_ENFORCEMENT.md")
        .read_text(encoding="utf-8")
        .split("\n")
    )
    in_table = False
    for number, line in enumerate(lines, start=1):
        stripped = line.strip()
        if stripped.startswith("|"):
            in_table = True
            assert stripped.endswith("|"), f"line {number}: table row does not end with a pipe"
        elif in_table and stripped:
            # A non-empty, non-pipe line ends the table only after a blank line.
            assert lines[number - 2].strip() == "", (
                f"line {number}: text continues a markdown table without a blank line"
            )
            in_table = False
        elif not stripped:
            in_table = False


# --------------------------------------------------------------------------- #
# GOV023 — a mention of pull_request is not evidence of pull_request execution
# --------------------------------------------------------------------------- #
# Reproduced: the positive branch tested `"pull_request" in low`, a raw substring
# of the quoted-inclusive text, so every one of these reported ok while the job
# would never run for a pull request:
#   vars.X == 'pull_request' · env.X == "pull_request" · format('pull_request')
#   'pull_request' · "pull_request" · # pull_request
#   contains(vars.LIST, 'pull_request')
@pytest.mark.parametrize(
    ("condition", "expected"),
    [
        # --- provably preserves pull_request ---
        ("", "ok"),
        ("github.event_name == 'pull_request'", "ok"),
        ('github.event_name == "pull_request"', "ok"),
        ("contains(github.event_name, 'pull_request')", "ok"),
        ("'pull_request' == github.event_name", "ok"),
        ("success() && github.event_name == 'pull_request'", "ok"),
        ("always()", "ok"),
        (
            "github.event_name == 'pull_request' || github.event_name == 'push'",
            "ok",
        ),
        # --- provably excludes it ---
        ("github.event_name == 'push'", "violation"),
        ("github.event_name == 'workflow_dispatch'", "violation"),
        ("false", "violation"),
        ("false && github.event_name == 'pull_request'", "violation"),
        ("github.event_name == 'pull_request' && false", "violation"),
        ("github.event_name != 'pull_request'", "violation"),
        ("!contains(github.event_name, 'pull_request')", "violation"),
        # --- a mere mention: data, not logic ---
        ("'pull_request'", "unknown"),
        ('"pull_request"', "unknown"),
        ("# pull_request", "unknown"),
        ("vars.X == 'pull_request'", "unknown"),
        ('env.X == "pull_request"', "unknown"),
        ("contains(vars.LIST, 'pull_request')", "unknown"),
        ("format('pull_request')", "unknown"),
        # --- undecidable structure ---
        ("!(github.event_name == 'pull_request')", "unknown"),
        ("not (github.event_name == 'pull_request')", "unknown"),
        ("vars.ENABLE == 'yes'", "unknown"),
        ("github.event_name != 'push'", "unknown"),
        ("github.event_name ==", "unknown"),
    ],
)
def test_gov023_separates_semantics_from_a_textual_mention(
    guard: ModuleType, condition: str, expected: str
) -> None:
    severity, reason = guard.classify_pr_condition(condition)
    assert severity == expected, f"{condition!r}: {reason}"
    # Nothing outside the whitelist may be accepted, whatever it mentions.
    if severity == "ok":
        assert (
            condition.strip() == ""
            or guard._PR_PRESERVING.search(condition.lower())
            or ("always" in condition.lower())
        ), f"{condition!r} was accepted without a provable event selection"


def test_gov023_accepts_only_conditions_comparing_against_event_name(
    guard: ModuleType,
) -> None:
    """The structural property behind the matrix above.

    Anything accepted as ok must either be empty/always(), or match the
    whitelist that compares the literal against `github.event_name`.  A condition
    can therefore never be accepted just for containing the token.
    """

    for condition in (
        "",
        "always()",
        "github.event_name == 'pull_request'",
        "contains(github.event_name, 'pull_request')",
    ):
        assert guard.classify_pr_condition(condition)[0] == "ok"

    # Every condition that mentions pull_request but is NOT in the whitelist must
    # be refused.  Generated, not enumerated, so a new shape cannot slip through.
    refusals = [
        f"{prefix}'pull_request'{suffix}"
        for prefix in ("vars.X == ", "env.X == ", "", "contains(vars.L, ", "format(")
        for suffix in ("", ")")
    ] + ["# pull_request", "pull_request"]
    for condition in refusals:
        if guard._PR_PRESERVING.search(condition.lower()):
            continue  # genuinely whitelisted
        assert guard.classify_pr_condition(condition)[0] != "ok", (
            f"{condition!r} mentions pull_request but proves nothing about the event"
        )


def test_the_pr_preserving_whitelist_never_matches_a_bare_mention(
    guard: ModuleType,
) -> None:
    """Directly pin the regex, so it cannot be loosened back to a substring."""
    for text in (
        "pull_request",
        "'pull_request'",
        "# pull_request",
        "vars.X == 'pull_request'",
        "contains(vars.LIST, 'pull_request')",
    ):
        assert guard._PR_PRESERVING.search(text) is None, f"{text!r} must not match"
    for text in (
        "github.event_name == 'pull_request'",
        'github.event_name == "pull_request"',
        "contains(github.event_name, 'pull_request')",
        "'pull_request' == github.event_name",
    ):
        assert guard._PR_PRESERVING.search(text) is not None, f"{text!r} must match"


# --------------------------------------------------------------------------- #
# GOV050–GOV054 — the readable fallback, and GOV043 rulesets
# --------------------------------------------------------------------------- #
# Probed live: with a metadata-only token the dedicated sub-endpoints answer 403,
# and allow_force_pushes / allow_deletions / required_conversation_resolution
# answer a misleading 404 "Branch not found".  That 404 must never be read as
# "the setting is off".  But GET /branches/main is readable and carries a
# `protection` object with the same-named fields, so it is a second,
# repository-supported source for the same truth.
def _plane_with_protection(guard: ModuleType, protection: dict) -> dict[str, object]:
    checks = {
        "contexts": list(guard.REQUIRED_CONTEXTS),
        "enforcement_level": "everyone",
    }
    extra = dict(protection)
    if isinstance(extra.get("required_status_checks"), dict):
        # merge, not replace: GOV041 reads `contexts` from this same object
        checks.update(extra.pop("required_status_checks"))
    responses: dict[str, object] = {
        "branches/main": {
            "name": "main",
            "protected": True,
            "protection": {"required_status_checks": checks, **extra},
        },
        "rules/branches/main": [],
    }
    responses.update(_SECURE_DETAIL_PAYLOADS)
    return responses


def test_the_readable_fallback_is_evaluated_on_its_values(
    guard: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dedicated endpoint that 403s must not hide an insecure value we CAN read."""
    responses = _plane_with_protection(guard, {"allow_force_pushes": {"enabled": True}})
    # The dedicated endpoint is refused; only the branch object is readable.
    responses["protection/allow_force_pushes"] = (None, "HTTP 403: Forbidden", 403)
    _stub_api(monkeypatch, guard, responses)

    report = guard.check_live("o/r", "main", "token")
    gov052 = [f for f in report.findings if f.code == "GOV052"]
    assert len(gov052) == 1
    assert gov052[0].severity == "violation", gov052[0].to_dict()
    assert "protection.allow_force_pushes" in gov052[0].witness, (
        "the witness must name the source that actually produced the value"
    )


def test_a_null_value_in_the_fallback_stays_blocked(
    guard: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fallback can never upgrade an unknown to a pass.

    This is the live shape of this repository: `protection.required_status_checks`
    is readable but its `strict` is null.
    """
    responses = _plane_with_protection(guard, {"required_status_checks": {"strict": None}})
    responses["protection/required_status_checks"] = (None, "HTTP 403: Forbidden", 403)
    _stub_api(monkeypatch, guard, responses)

    report = guard.check_live("o/r", "main", "token")
    assert report.verdict() == "BLOCKED", [f.to_dict() for f in report.findings]
    gov054 = [f for f in report.findings if f.code == "GOV054"]
    assert gov054 and gov054[0].severity == "unknown"
    assert "absent or null" in gov054[0].message


def test_a_secure_value_in_the_fallback_is_accepted(
    guard: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fallback is evidence, not a placeholder: a real value counts."""
    responses = _plane_with_protection(
        guard,
        {
            "allow_force_pushes": {"enabled": False},
            "allow_deletions": {"enabled": False},
            "required_conversation_resolution": {"enabled": True},
            "required_pull_request_reviews": {
                "required_approving_review_count": 1,
                "dismiss_stale_reviews": True,
            },
            "required_status_checks": {"strict": True},
        },
    )
    for endpoint in (
        "allow_force_pushes",
        "allow_deletions",
        "required_conversation_resolution",
        "required_pull_request_reviews",
        "required_status_checks",
    ):
        responses[f"protection/{endpoint}"] = (None, "HTTP 403: Forbidden", 403)
    _stub_api(monkeypatch, guard, responses)

    report = guard.check_live("o/r", "main", "token")
    assert report.verdict() == "VERIFIED", [f.to_dict() for f in report.findings]


def test_an_unreadable_setting_absent_everywhere_is_blocked_not_off(
    guard: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 404 'Branch not found' shape: unreadable, and nothing to fall back to."""
    responses = _plane_with_protection(guard, {})
    for endpoint in (
        "allow_force_pushes",
        "allow_deletions",
        "required_conversation_resolution",
        "required_pull_request_reviews",
        "required_status_checks",
    ):
        responses[f"protection/{endpoint}"] = (None, "HTTP 404: Not Found", 404)
    _stub_api(monkeypatch, guard, responses)

    report = guard.check_live("o/r", "main", "token")
    assert report.verdict() == "BLOCKED"
    assert {f.code for f in report.findings if f.severity == "unknown"} == {
        "GOV050",
        "GOV051",
        "GOV052",
        "GOV053",
        "GOV054",
    }


@pytest.mark.parametrize(
    ("rules", "expected_severity"),
    [
        ([], "ok"),
        ([{"type": "deletion"}], "violation"),
        ((None, "HTTP 403: Forbidden", 403), "unknown"),
    ],
)
def test_gov043_records_the_ruleset_plane(
    guard: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    rules: object,
    expected_severity: str,
) -> None:
    """Rulesets are readable with a metadata-only token, so they are evidence.

    An empty list proves classic branch protection is the only enforcement path.
    A non-empty list means a second mechanism this guard does not evaluate, which
    must not read as green.
    """
    responses = _plane_with_protection(guard, {})
    responses.update(_SECURE_DETAIL_PAYLOADS)
    responses["rules/branches/main"] = rules
    _stub_api(monkeypatch, guard, responses)

    report = guard.check_live("o/r", "main", "token")
    gov043 = [f for f in report.findings if f.code == "GOV043"]
    assert len(gov043) == 1, [f.to_dict() for f in report.findings]
    assert gov043[0].severity == expected_severity
