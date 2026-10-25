#!/usr/bin/env python3
"""Fast, stdlib-only integrity gate for ``.agents/board.json``.

Why this exists: PR #207 once carried a board that was 0 bytes and then a
truncated, byte-corrupted *base64* stub (2.7 KB vs 285 KB). It would have wiped
the coordination board and made ``agent_board.py check`` UNVERIFIABLE for every
branch, yet it was only caught minutes later by the full ``test`` job.  This gate
fails in seconds, before any install, and does not import the CLI it protects.

Checks (all must hold, exit 1 otherwise):
  * the file is non-empty, UTF-8, and parses as a JSON *object*
  * it is not an encoded blob (base64 / non-JSON text is rejected explicitly)
  * ``schema == 2``, ``protocol_version == 2`` and the required top-level keys exist
    with the right types (mirrors ``tests/unit/test_agent_board.py``)
  * every claim has a unique ``task``, a ``status`` and a ``zone``
  * the board did not shrink drastically versus ``--baseline`` (optional)
  * at most one *active* ``gates_owner`` exists

Usage:
    python scripts/check_board_integrity.py [--board PATH] [--baseline PATH]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import TypeGuard

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BOARD = ROOT / ".agents" / "board.json"
REQUIRED_KEYS: dict[str, type] = {
    "schema": int,
    "protocol": dict,
    "zones": list,
    "claims": list,
    "deferred_log": list,
    "next_work": list,
    "history": dict,
}
ACTIVE = frozenset({"active", "active_in_review"})
# A board that lost more than this fraction of its claims versus the baseline is
# treated as truncated, not as legitimate pruning.
MAX_SHRINK = 0.5
_BASE64_LINE = re.compile(r"^[A-Za-z0-9+/=\s]+$")


def _is_text(value: object) -> TypeGuard[str]:
    """A real, non-blank string. ``bool``/``int``/``list`` are not text (no truthiness)."""
    return isinstance(value, str) and bool(value.strip())


def validate(raw: bytes, baseline_claims: int | None = None) -> list[str]:
    """Return a list of human-readable problems (empty list == healthy)."""
    if not raw.strip():
        return ["board is empty (0 meaningful bytes)"]
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        return [f"board is not valid UTF-8: {exc}"]
    stripped = text.strip()
    if not stripped.startswith("{"):
        hint = " (looks base64-encoded)" if _BASE64_LINE.match(stripped[:200]) else ""
        return [f"board does not start with a JSON object{hint}"]
    try:
        board = json.loads(text)
    except json.JSONDecodeError as exc:
        return [f"board is not valid JSON: {exc}"]
    if not isinstance(board, dict):
        return ["board root must be a JSON object"]

    problems: list[str] = []
    if board.get("schema") != 2:
        problems.append(f"schema must be 2, got {board.get('schema')!r}")
    for key, kind in REQUIRED_KEYS.items():
        if key not in board:
            problems.append(f"missing required top-level key {key!r}")
        elif not isinstance(board[key], kind):
            problems.append(f"{key!r} must be {kind.__name__}, got {type(board[key]).__name__}")
    protocol = board.get("protocol")
    if isinstance(protocol, dict) and protocol.get("protocol_version") != 2:
        problems.append(f"protocol_version must be 2, got {protocol.get('protocol_version')!r}")
    history = board.get("history")
    if isinstance(history, dict):
        for block in ("waves_closed", "pull_requests"):
            if not history.get(block):
                problems.append(f"history.{block} must stay recorded (non-empty)")
    claims = board.get("claims")
    if isinstance(claims, list):
        seen: set[str] = set()
        for index, claim in enumerate(claims):
            if not isinstance(claim, dict):
                problems.append(f"claims[{index}] is not an object")
                continue
            task = claim.get("task")
            if not _is_text(task):
                problems.append(f"claims[{index}] 'task' must be a non-empty string, got {task!r}")
                continue
            if task in seen:
                problems.append(f"duplicate claim task {task!r}")
            seen.add(task)
            for field in ("status", "zone"):
                if not _is_text(claim.get(field)):
                    problems.append(
                        f"claim {task!r} {field!r} must be a non-empty string, "
                        f"got {claim.get(field)!r}"
                    )
        owners = [
            c.get("task")
            for c in claims
            if isinstance(c, dict) and c.get("gates_owner") and c.get("status") in ACTIVE
        ]
        if len(owners) > 1:
            problems.append(f"more than one active gates_owner: {owners}")
        if baseline_claims and len(claims) < baseline_claims * MAX_SHRINK:
            problems.append(
                f"board looks truncated: {len(claims)} claims vs baseline {baseline_claims}"
            )
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--board", type=Path, default=DEFAULT_BOARD)
    parser.add_argument("--baseline", type=Path, help="known-good board to compare size against")
    args = parser.parse_args(argv)
    try:
        raw = args.board.read_bytes()
    except OSError as exc:
        print(f"board-integrity: cannot read {args.board}: {exc}", file=sys.stderr)
        return 1
    baseline_claims: int | None = None
    if args.baseline:
        try:
            baseline_claims = len(json.loads(args.baseline.read_text("utf-8")).get("claims", []))
        except (OSError, ValueError) as exc:
            print(f"board-integrity: unreadable baseline: {exc}", file=sys.stderr)
            return 1
    problems = validate(raw, baseline_claims)
    if problems:
        print(f"board-integrity: FAIL ({args.board})", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print(f"board-integrity: OK ({args.board}, {len(raw)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
