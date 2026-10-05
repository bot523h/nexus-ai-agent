"""The production DB backup job cannot silently do nothing, or the wrong thing.

Forensic background (task-164): every scheduled ``backup-db`` run from
2026-09-21 to 2026-10-05 failed with the *same* signature — the R2 secrets were
never configured, so the job died five minutes in with a generic error, and the
only green runs in the series were housekeeping runs with ``backup-db`` skipped
(false green). A preflight now makes that failure immediate, exact and
actionable, and the production job is forbidden from falling back to a
CI-local SQLite file that no operator would ever restore.

These tests treat the workflow as the contract it is:

* the preflight is executed for real (extracted from the YAML and run under
  bash) for the configuration-failure paths, so the assertions are behavioural
  and not string matching;
* the remaining tests pin the invariants that cannot be executed here
  (ordering, least privilege, no failure masking, scheduled-only triggers).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "maintenance.yml"
WORKFLOW = WORKFLOW_PATH.read_text(encoding="utf-8")

PREFLIGHT_STEP_NAME = "Preflight — required production backup configuration"
REQUIRED_NAMES = (
    "NEXUS_DATABASE_URL",
    "R2_ACCOUNT_ID",
    "R2_ACCESS_KEY_ID",
    "R2_SECRET_ACCESS_KEY",
    "R2_BUCKET",
)
POSTGRES_URL = "postgresql://nexus:canary-db-password@db.example.invalid:5432/nexus"
R2_CANARIES = {
    "R2_ACCOUNT_ID": "canary-account-id",
    "R2_ACCESS_KEY_ID": "canary-access-key",
    "R2_SECRET_ACCESS_KEY": "canary-secret-key",
    "R2_BUCKET": "canary-bucket",
}


def _job_lines(job: str) -> list[str]:
    lines = WORKFLOW.splitlines()
    start = lines.index(f"  {job}:")
    body: list[str] = []
    for line in lines[start + 1 :]:
        if re.match(r"^  [A-Za-z0-9_-]+:\s*$", line):
            break
        body.append(line)
    return body


def _commands(job: str) -> str:
    """Executable lines only — comments cannot satisfy or violate the contract."""
    return "\n".join(line for line in _job_lines(job) if not line.strip().startswith("#"))


def _top_block(key: str) -> list[str]:
    """Raw lines of a top-level YAML mapping (``on:``, ``permissions:``)."""
    lines = WORKFLOW.splitlines()
    start = lines.index(f"{key}:")
    body: list[str] = []
    for line in lines[start + 1 :]:
        if line and not line.startswith((" ", "#")):
            break
        body.append(line)
    return body


def _mapping(block: list[str], indent: int) -> dict[str, str]:
    """``key: value`` pairs at exactly ``indent`` spaces; comments/blank dropped."""
    prefix = " " * indent
    deeper = " " * (indent + 1)
    pairs: dict[str, str] = {}
    for line in block:
        if not line.startswith(prefix) or line.startswith(deeper):
            continue
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, _, value = stripped.partition(":")
        pairs[key.strip()] = value.strip()
    return pairs


def _steps(job: str) -> list[list[str]]:
    """Each step's raw lines (a step starts with ``      - `` in this workflow)."""
    steps: list[list[str]] = []
    current: list[str] | None = None
    for line in _job_lines(job):
        if line.startswith("      - "):
            current = [line]
            steps.append(current)
        elif current is not None:
            current.append(line)
    return steps


def _workflow_permissions() -> dict[str, str]:
    return _mapping(_top_block("permissions"), 2)


def _job_permissions(job: str) -> dict[str, str]:
    lines = _job_lines(job)
    for index, line in enumerate(lines):
        if line == "    permissions:":
            block: list[str] = []
            for follow in lines[index + 1 :]:
                if follow.strip() and not follow.startswith(" " * 6):
                    break
                block.append(follow)
            return _mapping(block, 6)
    return {}


def _effective_permissions(job: str) -> dict[str, str]:
    """GitHub applies job-level permissions *after* workflow-level ones."""
    return _job_permissions(job) or _workflow_permissions()


def _triggers() -> dict[str, str]:
    return _mapping(_top_block("on"), 2)


def _preflight_script() -> str:
    """Extract the preflight ``run:`` block from the workflow verbatim."""
    lines = _job_lines("backup-db")
    start = next(
        i for i, line in enumerate(lines) if line.strip() == f"- name: {PREFLIGHT_STEP_NAME}"
    )
    run_index = next(i for i in range(start, len(lines)) if lines[i].strip() == "run: |")
    run_indent = len(lines[run_index]) - len(lines[run_index].lstrip())
    body: list[str] = []
    body_indent: int | None = None
    for line in lines[run_index + 1 :]:
        if not line.strip():
            body.append("")
            continue
        current = len(line) - len(line.lstrip())
        if current <= run_indent:
            break
        if body_indent is None:
            body_indent = current
        body.append(line[body_indent:])
    script = "\n".join(body).rstrip("\n")
    assert script, "preflight run block must not be empty"
    return script + "\n"


def _run_preflight(**overrides: str) -> subprocess.CompletedProcess[str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in REQUIRED_NAMES and key != "GITHUB_REPOSITORY"
    }
    env["GITHUB_REPOSITORY"] = "bot523h/nexus-ai-agent"
    env.update({name: "" for name in REQUIRED_NAMES})
    env.update(overrides)
    return subprocess.run(
        ["bash", "-c", _preflight_script()],
        capture_output=True,
        text=True,
        env=env,
    )


# ── executable behaviour of the preflight ────────────────────────────────


def test_missing_configuration_fails_fast_and_names_every_required_secret() -> None:
    result = _run_preflight()
    output = result.stdout + result.stderr
    assert result.returncode != 0, "an unconfigured production backup must never exit 0"
    for name in REQUIRED_NAMES:
        assert name in output, f"the failure must name the missing secret {name}"
    assert "::error" in output
    assert "docs/ops/r2-storage.md" in output
    assert "preflight ok" not in output


def test_partial_configuration_reports_only_what_is_missing() -> None:
    result = _run_preflight(**R2_CANARIES)
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "NEXUS_DATABASE_URL" in output
    for name in R2_CANARIES:
        assert name not in output, f"{name} is configured and must not be reported missing"


def test_a_non_postgres_database_url_is_refused_without_echoing_it() -> None:
    result = _run_preflight(NEXUS_DATABASE_URL="sqlite:///canary-local.sqlite3", **R2_CANARIES)
    output = result.stdout + result.stderr
    assert result.returncode != 0, "a CI-local SQLite fallback must never be accepted"
    assert "not a PostgreSQL URL" in output
    assert "canary-local.sqlite3" not in output, "the raw URL must never reach the log"


def test_secret_values_are_never_printed_on_failure() -> None:
    result = _run_preflight(**R2_CANARIES)  # the database URL stays missing
    output = result.stdout + result.stderr
    assert result.returncode != 0
    for canary in R2_CANARIES.values():
        assert canary not in output, f"secret value leaked into the log: {canary}"


@pytest.mark.skipif(
    shutil.which("pg_dump") is None,
    reason="pg_dump is required by the PostgreSQL backup leg (present on the CI runner image)",
)
def test_a_fully_configured_preflight_passes() -> None:
    result = _run_preflight(NEXUS_DATABASE_URL=POSTGRES_URL, **R2_CANARIES)
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "preflight ok" in output
    assert "canary-db-password" not in output


# ── invariants that cannot be executed here ─────────────────────────────


def _step_index(job: str, needle: str) -> int:
    steps = _steps(job)
    for index, step in enumerate(steps):
        if any(needle in line for line in step):
            return index
    raise AssertionError(f"no step of {job} contains {needle!r}")


def test_the_preflight_runs_before_the_install_and_the_backup() -> None:
    preflight = _step_index("backup-db", PREFLIGHT_STEP_NAME)
    install = _step_index("backup-db", "pip install -e .")
    backup = _step_index("backup-db", "nexus maintenance backup")
    assert preflight < install < backup, (
        "the preflight must run before the dependency install and the backup itself"
    )


def test_the_backup_job_cannot_mask_a_failure() -> None:
    body = _commands("backup-db")
    assert "continue-on-error" not in body
    assert "|| true" not in body
    assert "nexus maintenance backup" in body


def test_the_workflow_exposes_only_the_scheduled_and_manual_triggers() -> None:
    triggers = _triggers()
    assert set(triggers) == {"schedule", "workflow_dispatch"}, (
        "the maintenance workflow must be scheduled + manual only: secrets must never be "
        f"reachable from push/pull_request runs (found triggers: {sorted(triggers)})"
    )
    crons = [line.strip() for line in _top_block("on") if line.strip().startswith("- cron:")]
    assert crons == ['- cron: "17 3 * * *"', '- cron: "23 4 * * 1"'], (
        "the nightly backup and weekly housekeeping schedules must stay unchanged"
    )


@pytest.mark.parametrize("job", ["backup-db", "housekeeping"])
def test_every_job_effectively_grants_only_contents_read(job: str) -> None:
    assert _workflow_permissions() == {"contents": "read"}
    # A job-level block replaces the workflow-level one, so it is checked too.
    assert _effective_permissions(job) == {"contents": "read"}, (
        f"{job} must not be able to escalate its token beyond contents: read"
    )


@pytest.mark.parametrize("job", ["backup-db", "housekeeping"])
def test_checkout_credentials_are_never_persisted(job: str) -> None:
    checkouts = [step for step in _steps(job) if "uses: actions/checkout@" in step[0]]
    assert checkouts, f"{job} must check the repository out"
    for step in checkouts:
        assert any(line.strip() == "persist-credentials: false" for line in step), (
            f"{job} persists the checkout token in .git/config while "
            "`pip install -e .` executes dependency build code"
        )
