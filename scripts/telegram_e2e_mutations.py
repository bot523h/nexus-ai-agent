#!/usr/bin/env python3
"""Adversarial mutation harness for the Telegram end-to-end proof (task-214).

Same evidence rule as ``scripts/chat_memory_mutations.py`` and
``scripts/shell_sandbox_mutations.py``: **a guard that has never been attacked
is not evidence.**  Every mutation below breaks exactly one load-bearing part of
the real ``on_message`` → graph → store chain, re-runs the end-to-end suite, and
requires it to turn RED.

The suite is driven through the genuine Telegram entry point, so these mutants
attack the span that ``test_chat_memory_reachability.py`` structurally cannot
reach: everything ``on_message`` does around the graph.

Every touched source file is restored afterwards, including on failure, and the
final run must be GREEN again.  A surviving mutant means the guard is
documentation rather than code; survivors are reported by name and the exit code
is non-zero.

Usage::

    python scripts/telegram_e2e_mutations.py
    python scripts/telegram_e2e_mutations.py --list

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
HANDLERS = ROOT / "src" / "nexus_ai_agent" / "bot" / "handlers.py"
GRAPH = ROOT / "src" / "nexus_ai_agent" / "orchestration" / "graph.py"
SUITE = "tests/unit/test_telegram_memory_e2e.py"

#: The exact substrings the mutations replace.  A snippet that is not present is
#: a HARD ERROR, never a silent skip: a harness that quietly tests nothing is
#: worse than no harness.
ANCHORS: dict[str, tuple[Path, str, str]] = {
    "E1_thread_id_from_user_instead_of_chat": (
        HANDLERS,
        '        thread_id = f"tg:{chat_id}"',
        '        thread_id = f"tg:{int(update.effective_user.id)}"',
    ),
    "E2_base_state_not_used": (
        HANDLERS,
        "            state = _base_state(update, update.message.text)",
        '            state = {"messages": [{"role": "user", "content": update.message.text}],'
        ' "memory_context": "", "response": "", "tool_results": [], "error": None,'
        ' "turn_count": 0, "intent": "unknown", "active_persona": ""}',
    ),
    "E3_h1_removed_chat_bypasses_the_reader": (
        GRAPH,
        "        # H1: every remaining intent is a conversational turn, and a\n"
        '        # conversational turn is exactly the one that asks "what did I tell\n'
        '        # you?".  Route it through the reader before the persona agent.\n'
        '        return "memory_reader_chat"',
        '        if intent == "memory":\n'
        '            return "memory_reader_chat"\n'
        '        return "route_persona"',
    ),
    "E4_reply_replaced_by_a_canned_string": (
        HANDLERS,
        '            await _reply(update, result.get("response") or "")',
        '            await _reply(update, "ok")',
    ),
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


def _apply(path: Path, old: str, new: str) -> str:
    """Return a short description of what was done, or raise."""
    text = path.read_text(encoding="utf-8")
    if new in text and old not in text:
        return "already-applied"
    if old not in text:
        raise SystemExit(
            f"ANCHOR DRIFT: the snippet this mutant replaces is no longer in "
            f"{path.relative_to(ROOT)}:\n  {old!r}\n"
            f"The harness must be updated to match the code, never skipped."
        )
    backup = path.with_suffix(path.suffix + ".mutbak")
    shutil.copy2(path, backup)
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
    return "applied"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    if args.list:
        for name in ANCHORS:
            print(name)
        return 0

    green, out = _run_suite()
    if not green:
        print("baseline ... RED — refusing to mutate a suite that is already failing")
        print(out)
        return 1
    print("baseline ... GREEN")

    results: list[Result] = []
    for name, (path, old, new) in ANCHORS.items():
        backup = path.with_suffix(path.suffix + ".mutbak")
        try:
            try:
                action = _apply(path, old, new)
            except SystemExit as exc:
                results.append(Result(name, False, f"ANCHOR DRIFT — {exc}"))
                print(f"{name}: ANCHOR DRIFT")
                continue
            if action == "already-applied":
                results.append(Result(name, False, "already applied before the run"))
                continue
            green, out = _run_suite()
            if green:
                line = next(
                    (ln for ln in out.splitlines() if "passed" in ln or "failed" in ln), ""
                ).strip()
                results.append(Result(name, False, f"SURVIVED — suite stayed green ({line})"))
                print(f"{name}: SURVIVED")
            else:
                failed = re.findall(r"^FAILED (\S+)", out, re.M)
                results.append(
                    Result(name, True, f"killed by {len(failed)} test(s): {', '.join(failed[:3])}")
                )
                print(f"{name}: killed")
        finally:
            if backup.exists():
                shutil.move(str(backup), str(path))

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
