#!/usr/bin/env python3
"""Mutation-prove the CI CPU-torch install contract (task-omni-ci-cpu-torch-01).

A guard test that passes is not evidence that the guard is *load-bearing*.  The
CI install contract has three independent promises — package-source isolation
(the CPU index serves torch and nothing else), a pin that covers every supported
interpreter, and a fail-closed postcondition — and each is re-attacked here.

Every probe replaces the exact source line that carries a promise with a mutant
that removes it, requires the targeted guard test to flip GREEN -> RED, restores
the original bytes, and requires GREEN again.  The run is idempotent and leaves
the tree byte-identical; a leaked mutant is a loud failure, never a silent one.
Harness errors (missing anchor, red baseline, broken restore) are recorded as
invalid and are NEVER counted as killed mutations.

=====  ================================================  ==========================
M1     workflow re-adds a global extra index             source isolation
M2     constraint loses its exact ``+cpu`` pin           CPU build pinned
M3     constraint re-pins 2.15.0 (no cp310 wheel)        Python-matrix coverage
M4     parity matrix drifts off the proven interpreters  Python-matrix coverage
M5     CPU-index step drops ``--no-deps``                torch-only provenance
M6     CPU-index step drops the constraint               CPU build survives
M7     fail-closed CUDA/NVIDIA check is removed          no CUDA stack
M8     fail-closed ``+cpu`` check is removed             CPU build survives
M9     fail-closed ``pip check`` is removed              closure complete
M10    a workflow bypasses the shared script             single install path
=====  ================================================  ==========================

Invocation boundary (same rule as ``scripts/queue_ownership_mutations.py``): the
harness spawns pytest with an **argv list** and ``shell=False``, and every test
node id is validated against :data:`_NODE_ID` before it is passed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PYTEST = [
    sys.executable,
    "-m",
    "pytest",
    "-q",
    "-p",
    "no:warnings",
    "-p",
    "no:cacheprovider",
    "--tb=no",
]

CI = REPO / ".github/workflows/ci.yml"
MAINTENANCE = REPO / ".github/workflows/maintenance.yml"
SCRIPT = REPO / "scripts/ci_install.sh"
CONSTRAINTS = REPO / "constraints/ci-cpu-torch.txt"

GUARD = "tests/unit/test_ci_torch_disk_budget.py::"

#: A test node id this harness is willing to spawn pytest for.  Deliberately
#: narrow: a repository-relative ``tests/*.py`` path plus one node name.  It
#: cannot express an option (``-`` prefix), a shell metacharacter, an absolute
#: path or a traversal segment.
_NODE_ID = re.compile(r"tests/[A-Za-z0-9_/]+\.py(::[A-Za-z0-9_\[\]\.\-]+)?")


def _validate_node_ids(tests: tuple[str, ...]) -> tuple[str, ...]:
    if not tests:
        raise ValueError("refusing to run pytest with no test selection")
    for node in tests:
        if not _NODE_ID.fullmatch(node):
            raise ValueError(f"refusing to run an unvalidated test id: {node!r}")
    return tests


@dataclass(frozen=True)
class Probe:
    name: str
    target: Path
    anchor: str
    mutant: str
    tests: tuple[str, ...]


# The first ``.[dev]`` install in ci.yml is the REQUIRED test job.
_CI_INSTALL = '      - run: bash scripts/ci_install.sh ".[dev]"'
_MAINT_INSTALL = '      - run: bash scripts/ci_install.sh "."'
_PARITY_MATRIX = '        python-version: ["3.10", "3.11", "3.12"]'

PROBES: tuple[Probe, ...] = (
    Probe(
        name="M1 workflow re-adds a global extra index",
        target=CI,
        anchor=_CI_INSTALL,
        mutant=(
            _CI_INSTALL
            + "\n        env:\n"
            + "          PIP_EXTRA_INDEX_URL: https://download.pytorch.org/whl/cpu"
        ),
        tests=(GUARD + "test_no_global_extra_index_anywhere",),
    ),
    Probe(
        name="M2 constraint loses its exact +cpu pin",
        target=CONSTRAINTS,
        anchor="torch==2.14.1+cpu",
        mutant="torch>=2.14.1",
        tests=(GUARD + "test_constraint_pins_a_cpu_torch_build",),
    ),
    Probe(
        name="M3 constraint re-pins 2.15.0 (no cp310 wheel)",
        target=CONSTRAINTS,
        anchor="torch==2.14.1+cpu",
        mutant="torch==2.15.0+cpu",
        tests=(GUARD + "test_the_pin_covers_every_supported_python",),
    ),
    Probe(
        name="M4 parity matrix drifts off the proven interpreters",
        target=CI,
        anchor=_PARITY_MATRIX,
        mutant='        python-version: ["3.11", "3.12", "3.13"]',
        tests=(GUARD + "test_the_pin_covers_every_supported_python",),
    ),
    Probe(
        name="M5 CPU-index step drops --no-deps",
        target=SCRIPT,
        anchor="  --no-deps \\\n",
        mutant="",
        tests=(GUARD + "test_script_takes_torch_from_the_cpu_index_only",),
    ),
    Probe(
        name="M6 CPU-index step drops the constraint",
        target=SCRIPT,
        anchor='  -c "${CONSTRAINTS}" \\\n',
        mutant="",
        tests=(GUARD + "test_script_resolves_everything_else_from_pypi_only",),
    ),
    Probe(
        name="M7 fail-closed CUDA/NVIDIA check is removed",
        target=SCRIPT,
        anchor='if cuda:\n    problems.append(f"CUDA/NVIDIA packages present: {cuda}")',
        mutant='if False:\n    problems.append(f"CUDA/NVIDIA packages present: {cuda}")',
        tests=(GUARD + "test_script_fails_closed_on_cuda_or_non_cpu_torch",),
    ),
    Probe(
        name="M8 fail-closed +cpu check is removed",
        target=SCRIPT,
        anchor='if not torch_version.endswith("+cpu"):',
        mutant="if False:",
        tests=(GUARD + "test_script_fails_closed_on_cuda_or_non_cpu_torch",),
    ),
    Probe(
        name="M9 fail-closed pip check is removed",
        target=SCRIPT,
        anchor='    [sys.executable, "-m", "pip", "check"],',
        mutant='    [sys.executable, "-m", "pip", "list"],',
        tests=(GUARD + "test_script_fails_closed_on_cuda_or_non_cpu_torch",),
    ),
    Probe(
        name="M10 a workflow bypasses the shared script",
        target=MAINTENANCE,
        anchor=_MAINT_INSTALL,
        mutant="      - run: pip install -e .",
        tests=(GUARD + "test_every_heavy_workflow_installs_via_the_shared_script",),
    ),
)


def _run(tests: tuple[str, ...]) -> int:
    """Run one probe's tests; returns pytest's exit code (0 = green)."""
    argv = [*PYTEST, *_validate_node_ids(tests)]
    return subprocess.run(  # noqa: S603 - argv list, shell=False, ids validated
        argv,
        cwd=str(REPO),
        env={"PYTHONPATH": "src", "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        shell=False,
        check=False,
    ).returncode


def _git(*args: str) -> str:
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, shell=False
            ["git", *args],
            cwd=str(REPO),
            capture_output=True,
            text=True,
            shell=False,
            check=False,
        )
    except OSError:  # pragma: no cover - git missing
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Mutation-prove the CI CPU-torch install contract."
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=None,
        help="write a JSON report (summary + per-probe status) to this path",
    )
    args = parser.parse_args(argv)

    failures: list[str] = []
    results: list[dict[str, object]] = []
    targets = sorted({probe.target for probe in PROBES})
    pristine = {path: path.read_bytes() for path in targets}

    def record(name: str, outcome: str, detail: str = "") -> None:
        results.append({"probe": name, "status": outcome, "detail": detail})

    try:
        for probe in PROBES:
            original = probe.target.read_bytes()
            source = original.decode("utf-8")
            if probe.anchor not in source:
                failures.append(f"{probe.name}: anchor not found (source drifted)")
                record(probe.name, "anchor_missing")
                print(f"SKIP  {probe.name}: anchor not found")
                continue
            print(f"[{probe.name}] baseline ...", flush=True)
            baseline = _run(probe.tests)
            if baseline != 0:
                failures.append(f"{probe.name}: baseline is not green")
                record(probe.name, "baseline_red", f"pytest exit {baseline}")
                print(f"FAIL  {probe.name}: baseline is not green")
                continue
            try:
                probe.target.write_bytes(
                    source.replace(probe.anchor, probe.mutant, 1).encode("utf-8")
                )
                mutated = _run(probe.tests)
            finally:
                probe.target.write_bytes(original)
            if mutated == 0:
                failures.append(f"{probe.name}: mutant SURVIVED (tests stayed green)")
                record(probe.name, "survived")
                print(f"SURVIVED  {probe.name}")
                continue
            restored = _run(probe.tests)
            if restored != 0:
                failures.append(f"{probe.name}: tree not restored to green")
                record(probe.name, "restore_broken", f"pytest exit {restored}")
                print(f"BROKEN  {probe.name}: restore is not green")
                continue
            record(probe.name, "killed", f"mutant pytest exit {mutated}")
            print(f"KILLED  {probe.name}")
    finally:
        # Self-integrity: the tree must be byte-identical to how we found it,
        # even if a probe raised.  This runs before any report is written.
        for path in targets:
            if path.read_bytes() != pristine[path]:
                failures.append(f"harness left a mutant in {path.relative_to(REPO)}")
        summary = {
            "planned": len(PROBES),
            "executed": sum(1 for r in results if r["status"] != "anchor_missing"),
            "killed": sum(1 for r in results if r["status"] == "killed"),
            "survived": sum(1 for r in results if r["status"] == "survived"),
            "invalid": sum(
                1
                for r in results
                if r["status"] in {"anchor_missing", "baseline_red", "restore_broken"}
            ),
            "not_run": len(PROBES) - len(results),
        }
        report = {
            "accepted": not failures and summary["killed"] == len(PROBES),
            "summary": summary,
            "failures": failures,
            "probes": results,
            "provenance": {
                "git_commit": _git("rev-parse", "HEAD"),
                "worktree_clean": _git("status", "--porcelain") == "",
                "interpreter": sys.version.split()[0],
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "targets": [str(path.relative_to(REPO)) for path in targets],
                "target_hashes": {
                    str(path.relative_to(REPO)): hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in targets
                },
            },
        }
        if args.report is not None:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        print(
            "summary: "
            f"planned={summary['planned']} killed={summary['killed']} "
            f"survived={summary['survived']} invalid={summary['invalid']}"
        )
        if failures:
            print("FAILURES:")
            for failure in failures:
                print(f"  - {failure}")

    return 0 if not failures and summary["killed"] == len(PROBES) else 1


if __name__ == "__main__":
    raise SystemExit(main())
