#!/usr/bin/env python3
"""Adversarial mutation harness for the memory-reader query (task-211).

Same evidence rule as the other harnesses in ``scripts/``: **a guard that has
never been attacked is not evidence.**  Each mutation breaks the query
construction in ``_memory_reader`` in a different plausible way, re-runs the
suite, and requires it to turn RED.

Three mutants matter, because there are three distinct ways to get this wrong
and only one of them is the bug that actually shipped:

* ``M1``  revert to ``messages[-1]``            — the shipped defect
* ``M2``  take the FIRST user message           — plausible "fix", wrong direction
* ``M3``  take any message, ignoring the role   — looks role-aware, is not

``M2`` is the important one.  A reviewer who "fixed" this by reading the first
user message would pass a test that only checks `messages[-1]` is gone, and would
silently break multi-turn recall.  The suite has to be able to tell them apart.

Every touched file is restored afterwards, including on failure, and the final
run must be GREEN again.  A drifted anchor is a hard error, never a silent skip.

Usage::

    python scripts/memory_reader_query_mutations.py
    python scripts/memory_reader_query_mutations.py --list

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
GRAPH = ROOT / "src" / "nexus_ai_agent" / "orchestration" / "graph.py"
SUITE = "tests/unit/test_memory_reader_query.py"

FIXED = """    messages = state.get("messages", [])
    last_user = next(
        (m.get("content", "") for m in reversed(messages) if m.get("role") == "user"),
        "",
    )"""

MUTANTS: dict[str, str] = {
    # The defect as it shipped.
    "M1_reverts_to_messages_last": """    messages = state.get("messages", [])
    last_user = messages[-1]["content"] if messages else \"\"""",
    # A plausible over-correction: the first user message instead of the last.
    "M2_takes_the_first_user_message": """    messages = state.get("messages", [])
    last_user = next(
        (m.get("content", "") for m in messages if m.get("role") == "user"),
        "",
    )""",
    # Looks role-aware, is not: any message will do.
    "M3_ignores_the_role_filter": """    messages = state.get("messages", [])
    last_user = next(
        (m.get("content", "") for m in reversed(messages) if m.get("content")),
        "",
    )""",
}


@dataclass
class Result:
    name: str
    killed: bool
    detail: str


def _run_suite() -> tuple[bool, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", SUITE, "-q", "--no-header", "-p", "no:cacheprovider"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0, (proc.stdout or "")[-1500:]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    if args.list:
        for name in MUTANTS:
            print(name)
        return 0

    text = GRAPH.read_text(encoding="utf-8")
    if FIXED not in text:
        print("ANCHOR DRIFT: _memory_reader no longer contains the task-211 fix.")
        print("The harness must be updated to match the code, never skipped.")
        return 1

    green, out = _run_suite()
    if not green:
        print("baseline ... RED — refusing to mutate a suite that is already failing")
        print(out)
        return 1
    print("baseline ... GREEN")

    results: list[Result] = []
    backup = GRAPH.with_suffix(".py.mutbak")
    try:
        for name, mutant in MUTANTS.items():
            shutil.copy2(GRAPH, backup)
            try:
                GRAPH.write_text(text.replace(FIXED, mutant, 1), encoding="utf-8")
                green, out = _run_suite()
                if green:
                    line = next(
                        (ln for ln in out.splitlines() if "passed" in ln or "failed" in ln), ""
                    ).strip()
                    results.append(Result(name, False, f"SURVIVED — stayed green ({line})"))
                    print(f"{name}: SURVIVED")
                else:
                    failed = re.findall(r"^FAILED (\S+)", out, re.M)
                    short = ", ".join(f.split("::")[-1].split("[")[0] for f in failed[:3])
                    results.append(Result(name, True, f"killed by: {short}"))
                    print(f"{name}: killed")
            finally:
                shutil.move(str(backup), str(GRAPH))
    finally:
        if backup.exists():
            shutil.move(str(backup), str(GRAPH))

    green, out = _run_suite()
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
