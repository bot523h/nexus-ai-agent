"""The CI job that enforces the Continuum evidence contract cannot be softened.

A gate that exists only on paper is worse than none: these tests pin that the
``continuum-evidence`` job is blocking, runs on every supported interpreter,
checks out full history, binds its evidence to the commit under test, runs
all three verdict producers, and uploads SHA-named artifacts from an emptied
directory.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
JOB = "continuum-evidence"


def _job_lines() -> list[str]:
    lines = WORKFLOW.splitlines()
    start = lines.index(f"  {JOB}:")
    body: list[str] = []
    for line in lines[start + 1 :]:
        if re.match(r"^  [A-Za-z0-9_-]+:\s*$", line):
            break
        body.append(line)
    return body


def _commands() -> str:
    """Executable lines only — comments cannot satisfy or violate the contract."""
    return "\n".join(line for line in _job_lines() if not line.strip().startswith("#"))


def test_the_job_is_blocking_on_every_supported_interpreter() -> None:
    body = _commands()
    assert "continue-on-error" not in body
    assert "fail-fast: false" in body
    assert 'python-version: ["3.10", "3.11", "3.12", "3.14"]' in body
    assert "if: always()" in body  # only on the artifact upload, never on a verdict step
    for line in _job_lines():
        if line.strip().startswith("if:"):
            assert line.strip() == "if: always()"


def test_the_job_checks_out_full_history_and_binds_to_the_sha() -> None:
    body = _commands()
    assert "fetch-depth: 0" in body
    assert 'echo "HEAD=$(git rev-parse HEAD) GITHUB_SHA=$GITHUB_SHA' in body
    assert 'test "$(git rev-parse HEAD)" = "$GITHUB_SHA"' in body
    assert 'test "$(git rev-parse --is-shallow-repository)" = "false"' in body
    assert "rm -rf ci-artifacts && mkdir ci-artifacts" in body
    assert "d['provenance']['git_commit'] == os.environ['GITHUB_SHA']" in body


def test_the_job_runs_every_verdict_producer_without_diagnostic_flags() -> None:
    body = _commands()
    assert "python scripts/pack_coverage.py --json-out ci-artifacts/pack-coverage.json" in body
    assert "cmp ci-artifacts/pack-coverage.json ci-artifacts/pack-coverage.rerun.json" in body
    assert "--verify-artifact ci-artifacts/pack-coverage.json" in body
    assert "python scripts/continuum_gate.py --out ci-artifacts/continuum-gate.json" in body
    assert "cmp ci-artifacts/continuum-gate.json ci-artifacts/continuum-gate.rerun.json" in body
    assert (
        "python scripts/continuum_mutations.py --out ci-artifacts/continuum-mutations.json" in body
    )
    assert (
        "cmp ci-artifacts/continuum-mutations.json ci-artifacts/continuum-mutations.rerun.json"
        in body
    )
    for weakening in ("--threshold", "--pack ", "--tests", "--only", "|| true", "| tee", "set +e"):
        assert weakening not in body, weakening


def test_every_uploaded_artifact_is_checksummed() -> None:
    body = _commands()
    assert "sha256sum *.json > SHA256SUMS" in body


def test_artifacts_are_named_for_the_commit_and_interpreter() -> None:
    body = _commands()
    assert "name: continuum-evidence-${{ github.sha }}-py${{ matrix.python-version }}" in body
    assert "if-no-files-found: error" in body
