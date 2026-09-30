#!/usr/bin/env python3
"""Adversarial mutation harness for the governance check (task-207).

The tool this guards is the one every agent runs before pushing.  If it can be
reverted to fail-open without anything turning red, then every "exit 0, safe to
proceed" in this repository's history is unfalsifiable — and it already was,
which is how task-202 and task-203 were both leased while the tool reported
clearance.

Four mutants, one per way the safety property can be undone:

  C1  foreign boards ignored     — the original defect
  C2  fail OPEN on a bad remote — visibility without fail-closed
  C3  ``--no-remote`` made quiet — the opt-out stops disclosing its own gap
  C4  exit 2 collapsed into 0   — "could not verify" shares a code with "clear"

Every touched file is restored afterwards, including on failure, and the final
run must be GREEN.  A drifted anchor is a hard error, never a silent skip.

Usage::

    python scripts/agent_board_check_mutations.py
    python scripts/agent_board_check_mutations.py --list

Exit code 0 means "N/N mutants killed".
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BOARD = ROOT / "scripts" / "agent_board.py"
SUITE = "tests/unit/test_agent_board.py"

# Split so the mutation snippets stay readable and under the line limit.
_C3_OLD = '    if no_remote:\n        print(\n            "\\n!!  --no-remote: FOREIGN'
_C3_NEW = '    if False:\n        print(\n            "\\n!!  --no-remote: FOREIGN'


# (name, old, new, tests that must go red)
MUTANTS: dict[str, tuple[str, str]] = {
    # C1: back to reading exactly one board — the shipped defect. This must make
    # the FOREIGN board invisible, not merely quiet about failing to read it. The
    # first draft of this mutant only cleared `unconsulted`, and the whole suite
    # stayed green because the foreign board was still being consulted: a mutant
    # that does not reproduce the defect is worse than no mutant, because it
    # reports a green run that means nothing.
    "C1_foreign_boards_ignored": (
        '                boards.append((path, json.loads(Path(path).read_text(encoding="utf-8"))))',
        "                pass  # C1: read the local board only, as before task-207",
    ),
    # C2: an unreadable foreign board is treated as "nothing found".
    "C2_fails_open_on_a_bad_remote": (
        '                print(f"could not read board {path}: {exc}", file=sys.stderr)\n'
        "                unconsulted.append(path)",
        '                print(f"could not read board {path}: {exc}", file=sys.stderr)\n'
        "                pass  # C2: swallow it and keep going",
    ),
    # C3: the explicit local-only opt-out stops saying what it skipped.
    "C3_offline_opt_out_goes_quiet": (_C3_OLD, _C3_NEW),
    # C4: "could not verify" is handed the exit code for "verified clear".
    "C4_unverified_collapses_to_clear": (
        "        return CHECK_CLEAR if no_remote else CHECK_UNVERIFIED",
        "        return CHECK_CLEAR  # C4: never distinguish unverified from clear",
    ),
}


@dataclass
class Result:
    name: str
    killed: bool
    detail: str


def _run() -> tuple[bool, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", SUITE, "-q", "--no-header", "-p", "no:cacheprovider"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0, (proc.stdout or "")[-2000:]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()
    if args.list:
        for name in MUTANTS:
            print(name)
        return 0

    text = BOARD.read_text(encoding="utf-8")
    for name, (old, _new) in MUTANTS.items():
        if old not in text:
            print(f"ANCHOR DRIFT: {name} — snippet not found in scripts/agent_board.py.")
            print("The harness must be updated to match the code, never skipped.")
            return 1

    green, out = _run()
    if not green:
        print("baseline ... RED — refusing to mutate a suite that is already failing")
        print(out)
        return 1
    print("baseline ... GREEN")

    results: list[Result] = []
    backup = BOARD.with_suffix(".py.mutbak")
    try:
        for name, (old, new) in MUTANTS.items():
            shutil.copy2(BOARD, backup)
            try:
                BOARD.write_text(text.replace(old, new, 1), encoding="utf-8")
                green, out = _run()
                if green:
                    line = next(
                        (ln for ln in out.splitlines() if "passed" in ln or "failed" in ln), ""
                    ).strip()
                    results.append(Result(name, False, f"SURVIVED — stayed green ({line})"))
                    print(f"{name}: SURVIVED")
                else:
                    failed = re.findall(r"^FAILED (\S+)", out, re.M)
                    short = ", ".join(f.split("::")[-1].split("[")[0] for f in failed[:2])
                    results.append(Result(name, True, f"killed by: {short}"))
                    print(f"{name}: killed")
            finally:
                shutil.move(str(backup), str(BOARD))
    finally:
        if backup.exists():
            shutil.move(str(backup), str(BOARD))

    green, out = _run()
    if not green:
        print("restored baseline ... RED — the harness did not clean up after itself")
        print(out)
        return 1
    print("restored baseline ... GREEN")

    killed = sum(1 for r in results if r.killed)
    print("=" * 72)
    for r in results:
        print(f"  {'KILLED  ' if r.killed else 'SURVIVED'} {r.name}: {r.detail}")
    print("=" * 72)
    print(f"{killed}/{len(results)} killed")
    return 0 if killed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
