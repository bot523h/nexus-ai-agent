#!/usr/bin/env python3
"""Adversarial mutation harness for the multi-source board referee (task-207).

Evidence rule: a guard that has never been attacked is not evidence.  The
``check`` referee now consults the local board, every sibling git worktree's
board, and ``origin/main``'s board, and must answer 0 (clean) / 1 (proven
overlap) / 2 (unverifiable).  This script weakens that logic one invariant at a
time, re-runs the referee suite, and requires it to turn RED — a survivor is a
fake guard.

Every mutation is applied to ``scripts/agent_board.py`` in place and restored
from an in-memory copy in a ``finally`` block; the file's sha256 is checked
before and after so a crash can never leave the coordination tool altered.

Usage::

    python scripts/board_referee_mutations.py
    python scripts/board_referee_mutations.py --list

Exit code 0 means "N/N mutants killed"; anything else means a guard is fake.
"""

# ruff: noqa: E501 - the MUTATIONS table holds verbatim source snippets; they must
# match the shipped file byte-for-byte, so they cannot be re-wrapped.
from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "agent_board.py"
TESTS = ("tests/unit/test_agent_board.py",)


@dataclass(frozen=True)
class Mutation:
    name: str
    old: str
    new: str
    why: str


MUTATIONS: tuple[Mutation, ...] = (
    Mutation(
        "unreadable_source_becomes_a_pass",
        '    if unreadable:\n        print("UNVERIFIABLE: cannot establish the absence of overlap',
        '    if False:\n        print("UNVERIFIABLE: cannot establish the absence of overlap',
        "an unreadable remote/worktree must never be reported as 'no overlap'",
    ),
    Mutation(
        "no_remote_flag_ignored",
        '    no_remote = bool(getattr(args, "no_remote", False))',
        "    no_remote = False  # mutation: ignore --no-remote and always hit git",
        "--no-remote must actually narrow the check to the local board",
    ),
    Mutation(
        "foreign_worktree_boards_skipped",
        "    if result is None or result.returncode != 0:\n        return [], False",
        "    if result is None or result.returncode != 0:\n        return [], True  # mutation: pretend enumeration succeeded",
        "a foreign lease living in a sibling worktree must still be seen",
    ),
    Mutation(
        "proven_conflict_downgraded_to_unverifiable",
        "        print(STOP_BANNER)\n        return 1",
        "        print(STOP_BANNER)\n        return 2  # mutation: a proven overlap becomes 'unverifiable'",
        "a proven conflict must be exit 1, not hidden behind exit 2",
    ),
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_tests() -> bool:
    # ``--noconftest``: test_agent_board.py loads the CLI module directly and uses
    # only builtin fixtures, so the harness stays dependency-free (no structlog,
    # no package install) — the same targeted invocation the repo documents for
    # diagnostic runs.
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-x", "--noconftest", *TESTS],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()
    if args.list:
        for mutation in MUTATIONS:
            print(f"{mutation.name}: {mutation.why}")
        return 0

    original = SCRIPT.read_text(encoding="utf-8")
    before = _sha256(SCRIPT)

    print("baseline ... ", end="", flush=True)
    if not run_tests():
        print("RED — refusing to mutate a suite that is already failing")
        return 2
    print("GREEN")

    killed = 0
    survivors: list[str] = []
    try:
        for mutation in MUTATIONS:
            if mutation.old not in original:
                print(f"{mutation.name}: MUTATION DOES NOT APPLY (source drifted)")
                survivors.append(mutation.name)
                continue
            SCRIPT.write_text(original.replace(mutation.old, mutation.new, 1), encoding="utf-8")
            try:
                green = run_tests()
            finally:
                SCRIPT.write_text(original, encoding="utf-8")
            if green:
                print(f"{mutation.name}: SURVIVED — {mutation.why}")
                survivors.append(mutation.name)
            else:
                killed += 1
                print(f"{mutation.name}: killed")
    finally:
        # Belt and braces: never leave the shared coordination tool mutated.
        SCRIPT.write_text(original, encoding="utf-8")

    after = _sha256(SCRIPT)
    print("restored baseline ... ", end="", flush=True)
    if after != before:
        print("RED — restore changed the file (sha256 mismatch)")
        return 3
    if not run_tests():
        print("RED — restore failed")
        return 3
    print("GREEN")
    print(f"{killed}/{len(MUTATIONS)} killed")
    return 0 if killed == len(MUTATIONS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
