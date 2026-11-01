#!/usr/bin/env python3
"""Focused mutation probes for the P0 redelivery fix (task-nagar-workspace-redelivery-01).

Why this exists: a regression suite that stays GREEN after its security invariant
is removed proves nothing. Each probe below removes exactly ONE invariant — and
where that invariant is defended in more than one place, it removes *all* of the
defences, because a probe that only disables half a guard measures nothing. The
probe then runs the focused tests that are supposed to catch it and requires a RED
result. The tree is restored byte-identically (verified by sha256) after every
probe.

The probes are deliberately narrow and textual: this repository already ships
focused mutation campaigns (``scripts/*_mutations.py``) and a broad framework is
not needed for six anchors.

Usage:
    python scripts/creative_redelivery_mutations.py            # run every probe
    python scripts/creative_redelivery_mutations.py --list     # list probes
"""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SURFACE = "src/nexus_ai_agent/bot/creative_surface.py"
WORKER = "src/nexus_ai_agent/creative/render_jobs.py"

REDELIVERY = "tests/integration/test_creative_surface_redelivery.py"
SURFACE_UNIT = "tests/unit/test_creative_surface.py"
WORKER_UNIT = "tests/unit/test_creative_render_jobs.py"


@dataclass(frozen=True)
class Edit:
    """One textual replacement inside one file."""

    path: str
    old: str
    new: str


@dataclass(frozen=True)
class Probe:
    """One invariant, its mutation(s), and the test selection that must go RED."""

    name: str
    invariant: str
    edits: tuple[Edit, ...]
    tests: tuple[str, ...] = field(default=())


PROBES: tuple[Probe, ...] = (
    Probe(
        name="M1-surface-drops-input_sha256",
        invariant="the surface must persist the measured digest in the payload",
        edits=(
            Edit(
                SURFACE,
                '            "input_sha256": input_sha256,',
                '            "input_sha256": None,  # MUTANT: digest dropped from the payload',
            ),
        ),
        tests=(REDELIVERY, SURFACE_UNIT, WORKER_UNIT),
    ),
    Probe(
        name="M2-worker-digest-verification-removed",
        invariant="the worker must re-measure the bytes before any engine runs",
        edits=(
            Edit(
                WORKER,
                "        verify_staged_input(data, workspace)",
                "        # MUTANT: preflight digest verification disabled",
            ),
            Edit(
                WORKER,
                "    _verify_input_digest(candidate, payload.input_sha256)\n    return candidate",
                "    # MUTANT: per-branch digest verification disabled\n    return candidate",
            ),
        ),
        tests=(REDELIVERY, WORKER_UNIT),
    ),
    Probe(
        name="M3-destructive-shared-rmtree-reintroduced",
        invariant="the surface must never recursively delete a job workspace",
        edits=(
            Edit(
                SURFACE,
                "            try:\n                workspace = _ensure_private_directory("
                'workspace, temp_root, "workspace")',
                "            try:\n"
                "                shutil.rmtree(workspace, ignore_errors=True)  # MUTANT\n"
                "                workspace = _ensure_private_directory("
                'workspace, temp_root, "workspace")',
            ),
        ),
        tests=(REDELIVERY,),
    ),
    Probe(
        name="M4-corrupt-destination-trusted",
        invariant="reusing an immutable input must verify its bytes",
        edits=(
            Edit(
                SURFACE,
                "    if _sha256_of_file(destination) != digest:",
                "    if False:  # MUTANT: an existing destination is trusted unverified",
            ),
        ),
        tests=(REDELIVERY,),
    ),
    Probe(
        name="M5-surface-workspace-symlink-guard-disabled",
        invariant="a pre-planted workspace symlink must not redirect",
        edits=(
            Edit(
                SURFACE,
                "    if stat.S_ISLNK(stat_result.st_mode):\n"
                '        raise _StagingError("media_missing", f"{label} is a symlink: {path}")\n'
                "    if not stat.S_ISDIR(stat_result.st_mode):\n"
                '        raise _StagingError("media_missing", '
                'f"{label} is not a directory: {path}")',
                "    return  # MUTANT: the directory-symlink guard is gone entirely",
            ),
        ),
        tests=(REDELIVERY,),
    ),
    Probe(
        name="M6-worker-lstat-file-type-guard-disabled",
        invariant="the worker must reject a symlinked/non-regular input",
        edits=(
            Edit(
                WORKER,
                "    if stat.S_ISLNK(stat_result.st_mode):\n"
                "        raise CreativeRenderError("
                '"input_integrity", '
                'f"staged input is a symlink: {path}")\n'
                "    if not stat.S_ISREG(stat_result.st_mode):\n"
                "        raise CreativeRenderError(\n"
                '            "input_integrity", f"staged input is not a regular file: {path}"\n'
                "        )",
                "    # MUTANT: the file-type guard is gone entirely",
            ),
        ),
        tests=(REDELIVERY, WORKER_UNIT),
    ),
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run(tests: tuple[str, ...]) -> tuple[int, str]:
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--tb=no", "-p", "no:cacheprovider", *tests],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    lines = (completed.stdout or "").strip().splitlines()
    return completed.returncode, lines[-1] if lines else "(no output)"


def main() -> int:
    parser = argparse.ArgumentParser(description="P0 redelivery mutation campaign")
    parser.add_argument("--list", action="store_true", help="list probes and exit")
    args = parser.parse_args()

    if args.list:
        for probe in PROBES:
            print(f"{probe.name}: {probe.invariant}")
        return 0

    failures: list[str] = []
    for probe in PROBES:
        touched: dict[Path, bytes] = {}
        digests: dict[Path, str] = {}
        applied = True
        for edit in probe.edits:
            target = ROOT / edit.path
            if target not in touched:
                touched[target] = target.read_bytes()
                digests[target] = _sha256(target)
            text = target.read_text(encoding="utf-8")
            if edit.old not in text:
                print(f"SKIP {probe.name}: anchor not found in {edit.path}")
                applied = False
                break
            target.write_text(text.replace(edit.old, edit.new, 1), encoding="utf-8")

        try:
            if not applied:
                failures.append(f"{probe.name} (anchor missing)")
                continue
            code, summary = _run(probe.tests)
        finally:
            for target, original in touched.items():
                target.write_bytes(original)

        restored = all(_sha256(target) == digest for target, digest in digests.items())
        verdict = "RED (killed)" if code != 0 else "GREEN (SURVIVED — vacuous test)"
        print(f"{probe.name:46s} exit={code:<3d} {verdict:28s} {summary}")
        print(f"{'':46s} tree restored byte-identical: {restored}")
        if code == 0 or not restored:
            failures.append(probe.name)

    print()
    if failures:
        print(f"MUTATION CAMPAIGN FAILED: {len(failures)}/{len(PROBES)} survived: {failures}")
        return 1
    print(f"MUTATION CAMPAIGN PASSED: {len(PROBES)}/{len(PROBES)} mutants killed, tree restored")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
