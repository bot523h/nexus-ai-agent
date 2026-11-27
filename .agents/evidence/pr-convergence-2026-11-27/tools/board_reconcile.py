#!/usr/bin/env python3
"""Reconcile the canonical board for the 2026-11-27 PR backlog convergence.

Adds one durable, queued *family* registration per deferred PR group (so no
closed PR loses its resume anchor), records the live-successor fact on
``task-w35-ibpc-security-closure``, and appends a dated board revision note.
All writes go through the board CLI's own ``load_board``/``save_board`` so the
schema validation and on-disk formatting are the canonical ones.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import agent_board  # noqa: E402

FORENSICS = Path(__file__).resolve().parents[1] / "PR_FORENSICS.json"

FAMILIES = {
    "pr-backlog-security-2026-11-27": {
        "title": "PR backlog convergence 2026-11-27: security, authorization and command wiring",
        "zone": "pr-backlog-security",
        "priority": "P0",
        "prs": [33, 58, 89, 99, 114, 115, 149, 257],
        "why": (
            "These PRs predate the merged Wave-1/Wave-3 authorization spine (#365/#366/#367) "
            "and each carries 12-20 merge-tree conflicts; none may be replayed as a whole without "
            "re-deriving it against the canonical CommandBus/policy path."
        ),
    },
    "pr-backlog-runtime-2026-11-27": {
        "title": "PR backlog convergence 2026-11-27: runtime, queue, storage and memory",
        "zone": "pr-backlog-runtime",
        "priority": "P1",
        "prs": [57, 60, 63, 92, 113, 117, 118, 121, 122, 193, 204],
        "why": (
            "These PRs target runtime/queue/storage/memory surfaces that main has since reworked "
            "(resource-pack sync, engine-owned schema, async DB, sqlite-vec). #193 additionally "
            "requires an owner decision on the Python 3.14 baseline."
        ),
    },
    "pr-backlog-creative-2026-11-27": {
        "title": "PR backlog convergence 2026-11-27: creative runtime, operations and assets",
        "zone": "pr-backlog-creative",
        "priority": "P1",
        "prs": [64, 66, 69, 73, 97, 327, 328],
        "why": (
            "The creative spine (#126/#183/#128) and the canonical asset library "
            "(creative/assets/*) already landed; these PRs carry older, conflicting variants "
            "and the Studio PWA (#328) is a product decision on top of the read API."
        ),
    },
    "pr-backlog-studio-2026-11-27": {
        "title": "PR backlog convergence 2026-11-27: Studio transactions and agent intelligence",
        "zone": "pr-backlog-studio",
        "priority": "P1",
        "prs": [129, 150, 230],
        "why": (
            "PlanTransaction/CommandBus landed on main via the Studio substrate; these PRs "
            "predate it and conflict with creative/studio/{bus,plan_execution,runtime}.py."
        ),
    },
    "pr-backlog-causal-memory-2026-11-27": {
        "title": "PR backlog convergence 2026-11-27: causal memory",
        "zone": "pr-backlog-causal-memory",
        "priority": "P2",
        "prs": [273, 284],
        "why": (
            "Draft causal-memory contracts (NEL semantic time, read-only Causal Memory) have no "
            "counterpart on main and need an architecture decision before a rebase."
        ),
    },
    "pr-backlog-governance-2026-11-27": {
        "title": "PR backlog convergence 2026-11-27: governance, documentation and automation",
        "zone": "pr-backlog-governance",
        "priority": "P1",
        "prs": [84, 137, 157],
        "why": (
            "ENGINEERING_CONSTITUTION.md, the collision/governance tests and the agent "
            "constitution are absent from main; they touch ci.yml, Makefile, AGENTS.md and "
            "agent_board.py and need a governance decision rather than a blind replay."
        ),
    },
}


def main() -> int:
    forensics = json.loads(FORENSICS.read_text(encoding="utf-8"))
    branches = {r["number"]: r["head_ref"] for r in forensics["records"]}

    with agent_board.board_lock():
        board = agent_board.load_board()
        existing = {claim["task"] for claim in board["claims"]}
        added = []
        for task, spec in FAMILIES.items():
            if task in existing:
                continue
            lines = "\n".join(f"- PR #{n} (branch `{branches.get(n, '?')}`)" for n in spec["prs"])
            board["claims"].append(
                {
                    "task": task,
                    "title": spec["title"],
                    "zone": spec["zone"],
                    "priority": spec["priority"],
                    "status": "queued",
                    "scope": (
                        f"Resume anchor for the 2026-11-27 PR backlog convergence. "
                        f"The following PRs were closed as DEFERRED_TO_BOARD_AND_CLOSED; their "
                        f"branches are preserved and remain reopenable:\n{lines}\n"
                        f"{spec['why']} Resume only after rebasing the selected PR onto current "
                        f"main and re-deriving its delta against the canonical implementation; "
                        f"reopen the PR or open a successor branch from main."
                    ),
                    "prerequisites": [],
                    "acceptance_criteria": [
                        "The selected PR is rebased onto current main (or replaced by a successor branch) with its published history preserved.",
                        "The delta is re-derived against the canonical implementation; no second parallel runtime/queue/storage/authority/transaction path is introduced.",
                        "make lint, make types and make test pass on the exact new HEAD; adversarial regression coverage is added for the repaired defect.",
                        "The ownership referee (agent_board.py check) exits 0 on the actual changed files before every push, and the PR is handed off for independent review.",
                    ],
                    "evidence_required": [
                        "Exact head/base/main SHAs, the rebase or successor branch, and the changed-file inventory.",
                        "Gate output (lint/types/test) with commands, results and the SHA they ran against.",
                        "The latest required CI conclusions and unresolved review threads on the exact HEAD.",
                    ],
                    "exclusive_paths": [],
                }
            )
            added.append(task)

        for claim in board["claims"]:
            if claim.get("task") == "task-w35-ibpc-security-closure":
                note = (
                    " Reconciled 2026-11-27 by the PR backlog convergence: PR #366 is merged into "
                    "main at b8dee44c5f430e65acb28e80356fd502f17cfefa (completion_records evidence), "
                    "and the live successor is PR #367 (head a8a06eee) which closes the two "
                    "authorization gaps left by #365."
                )
                if note.strip() not in claim.get("scope", ""):
                    claim["scope"] = claim.get("scope", "") + note

        board["history"]["board_revisions"].append(
            {
                "rev": max((entry.get("rev", 0) for entry in board["history"]["board_revisions"]), default=0) + 1,
                "date": "2026-11-27",
                "note": (
                    "PR backlog convergence 2026-11-27: live inventory of 44 open PRs at main "
                    "54073d1f; #368 and #366 merge evidence recorded; pr-repair-247-phase3 completed; "
                    "6 family resume anchors registered for the PRs closed as deferred; evidence in "
                    ".agents/evidence/pr-convergence-2026-11-27/."
                ),
            }
        )
        agent_board.save_board(board)

    print(f"added family claims: {added or 'none (already present)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
