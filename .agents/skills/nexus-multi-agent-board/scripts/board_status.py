#!/usr/bin/env python3
"""Read-only board status helper for the nexus-ai-agent multi-agent board.

Prints, in one pass:
  1. your identity (the branch name) and whether it already holds a live lease;
  2. the next claimable task from the board (``queued``/``expired``/``deferred``
     entries that are not blocked by another live lease);
  3. an overlap pre-check of your current working-tree changes against every
     other agent's live ``exclusive_paths``.

This mirrors the repository's own technique for consuming the board CLI without
importing the unpackaged ``scripts`` namespace: ``scripts/agent_board.py`` is
loaded by file path with ``importlib`` and its pure functions are reused, so the
matching semantics stay identical to ``agent_board.py check``.

Usage:
    python .agents/skills/nexus-multi-agent-board/scripts/board_status.py [--branch NAME]

If ``--branch`` is omitted, the current git branch is used. Exit code is 1 when
an overlap is detected (same contract as ``agent_board.py check``), else 0.
"""

from __future__ import annotations

import argparse
import importlib.util
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
AGENT_BOARD = REPO_ROOT / "scripts" / "agent_board.py"


def _load_board_module():
    if not AGENT_BOARD.is_file():
        sys.exit(f"board CLI not found at {AGENT_BOARD}")
    spec = importlib.util.spec_from_file_location("_agent_board", AGENT_BOARD)
    if spec is None or spec.loader is None:
        sys.exit(f"cannot load {AGENT_BOARD}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _current_branch() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""
    return out.stdout.strip()


def _changed_files() -> list[str]:
    """Union of working-tree and staged changes vs origin/main (best effort)."""
    files: set[str] = set()
    for cmd in (
        ["git", "diff", "--name-only"],
        ["git", "diff", "--name-only", "--cached"],
        ["git", "diff", "--name-only", "origin/main...HEAD"],
        ["git", "ls-files", "--others", "--exclude-standard"],
    ):
        try:
            out = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True, check=True)
        except (subprocess.CalledProcessError, FileNotFoundError):
            continue
        files.update(line.strip() for line in out.stdout.splitlines() if line.strip())
    return sorted(files)


def _live_own_claim(board: dict, branch: str, ab) -> list[str]:
    held = []
    for claim in board.get("claims", []):
        if claim.get("agent_branch") == branch and ab._claim_live(claim):
            held.append(claim.get("task", "?"))
    return held


def _next_task(board: dict, branch: str, ab) -> str | None:
    for claim in board.get("claims", []):
        if claim.get("status") not in ("queued", "expired", "deferred"):
            continue
        blocked = False
        for entry in board.get("deferred_log", []):
            if entry.get("task") != claim.get("task"):
                continue
            owner = ab._find(board, entry["task"])
            if owner is not None and ab._is_active_claim(owner):
                blocked = True
                break
        if not blocked:
            return claim.get("task")
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only board status helper")
    parser.add_argument(
        "--branch", default=None, help="your branch (defaults to current git branch)"
    )
    args = parser.parse_args()

    ab = _load_board_module()
    board = ab.load_board()
    branch = args.branch or _current_branch() or "<unknown>"

    print(f"identity (branch): {branch}")
    held = _live_own_claim(board, branch, ab)
    print(f"  live leases held: {held if held else 'none'}")

    nxt = _next_task(board, branch, ab)
    print(f"next claimable task: {nxt if nxt else 'none — propose one in .agents/board.json'}")

    files = _changed_files()
    print(f"changed files vs origin/main ({len(files)}):")
    for f in files:
        print(f"  {f}")

    hits = ab._conflicting_paths(board, branch, files)
    if hits:
        print("\nOVERLAP with another agent's live exclusive paths:")
        for task, path in hits:
            print(f"  {path}  <- claimed by {task}")
        print("\nDo NOT push this work. Pick a disjoint task or defer.")
        return 1

    print("\nno overlap — safe to proceed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
