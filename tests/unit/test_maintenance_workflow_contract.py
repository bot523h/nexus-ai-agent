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


def test_the_preflight_runs_before_the_install_and_the_backup() -> None:
    body = "\n".join(_job_lines("backup-db"))
    assert body.index(PREFLIGHT_STEP_NAME) < body.index("pip install -e .")
    assert body.index(PREFLIGHT_STEP_NAME) < body.index("nexus maintenance backup")


def test_the_backup_job_cannot_mask_a_failure() -> None:
    body = _commands("backup-db")
    assert "continue-on-error" not in body
    assert "|| true" not in body
    assert "nexus maintenance backup" in body


def test_the_workflow_stays_scheduled_only_and_least_privilege() -> None:
    assert "push:" not in WORKFLOW, "the maintenance workflow must not run on push"
    assert "pull_request:" not in WORKFLOW, "secrets must not be reachable from PR runs"
    assert "permissions:\n  contents: read" in WORKFLOW
