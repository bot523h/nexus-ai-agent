---
name: nexus-multi-agent-board
description: This skill should be used when working in the nexus-ai-agent repository in parallel with other agents, when asked to "claim a task", "check the board", "avoid conflicts", "coordinate agents", "release or defer a task", "who owns this file", or before editing any shared path. It encodes the file-based claim board (.agents/board.json), the zero-collision rules, and the exact CLI commands that make a claim visible to other sandboxes.
---

# NEXUS Multi-Agent Board

## Purpose

Several agents develop this repository in parallel from separate sandboxes. The only shared
medium is git, so coordination is **file-based**: a claim board at `.agents/board.json` (schema 2),
driven by a zero-dependency CLI `scripts/agent_board.py`. The board's behaviour is itself tested
(`tests/unit/test_agent_board.py`). An unpushed claim does not exist for any other sandbox.

The failure this skill prevents: two agents edit the same file in different sandboxes, and the
second push silently reverts the first. The board exists so that never happens.

## When to use

Use this skill for **every** piece of work that touches the repository, before writing code:

- starting any task (claim first, code second);
- touching a shared path (`bot/handlers.py`, `cli.py`, `docs/architecture/`, `.github/`, ...);
- before pushing, to prove no overlap;
- releasing finished work or deferring blocked work.

Read-only exploration needs no claim. The moment a change is intended, claim.

## The five rules (non-negotiable)

1. **Claim before you code.** `show` → `next --branch <you>` → `claim <task> --branch <you>`.
   Commit **and push** the board change immediately.
2. **One owner per file-zone.** Claims carry `exclusive_paths`. Run `check --files ... --branch ...`
   before pushing; exit 1 means overlap — do not push that work.
3. **Branch name is the canonical identity.** Letters (A/B/C) are labels only. State identity as the
   branch, after reading `git ls-remote origin "arena/*"`, the open PRs, and the board.
4. **One gates owner.** Exactly one agent (`gates_owner: true`) runs the full gates on main-bound
   work. Everyone else writes "deferred to gates owner" in the PR body. Local diagnostics
   (`ruff check`, targeted `pytest`) are always allowed.
5. **Leases expire (24 h TTL).** `show`/`next` auto-release stale leases. Renew with `claim`
   (heartbeat). `gates_owner` is exclusive: taking it clears any other.

## Procedure

### 1. Learn who you are and what is free

```bash
git ls-remote origin "arena/*"                  # other agents' branches
python scripts/agent_board.py show              # zones, claims, deferred log, gates rule
python scripts/agent_board.py next --branch <your-branch>
```

`show` only reads, but it auto-GCs expired leases and, when it frees any, **rewrites the board on
disk** before printing. If `git status` shows `.agents/board.json` changed after a mere `show`, do
**not** discard it blindly — inspect and classify first:

```bash
git diff .agents/board.json     # classify: incidental GC, or an edit you meant to keep?
```

- Incidental GC (only stale `active` cards flipped to `expired`): discard just that file —
  `git checkout -- .agents/board.json`.
- A change you intended (a claim you made): keep it and commit.

`git checkout -- .agents/board.json` also discards a real edit, so never run it as a reflex.

### 2. Claim the task

```bash
python scripts/agent_board.py claim <task> --branch <your-branch>
git add .agents/board.json
git commit -m "board: claim <task>"
git push            # an unpushed claim is invisible to other sandboxes
```

Add `--gates` only if taking the single gates-owner role. Add `--ttl <hours>` only to shorten.

### 3. Prove your changed files are disjoint before pushing

```bash
python scripts/agent_board.py check --files "$(git diff --name-only origin/main)" --branch <your-branch>
```

Exit 0 → safe. Exit 1 → the printed paths belong to another live lease. Do **not** push that work;
pick a different task or `defer`. The matching rule: an exclusive path ending in `/` fences a
directory; otherwise it is an exact file.

### 4. Finish or block

```bash
python scripts/agent_board.py release <task> --branch <your-branch>   # finished
python scripts/agent_board.py defer <task> --branch <your-branch> \
  --reason "..." --fa "..." --resume-when "..."                        # blocked
```

`defer` records the bilingual note and prints the exact lines to paste into `.agents/board.json`
`deferred_log` and `next_work` (the CLI does not rewrite those structures). Commit and push.

## The card and the PR

A claimable task carries `acceptance_criteria` and `evidence_required` — a task without them cannot
be claimed and `tests/unit/test_agent_board.py` fails the board if one appears. A PR body states:
task id, zone, the acceptance criteria met, the exact commands run, and their observed result.
"Tests pass" without a command is not evidence.

## Additional Resources

- **`references/board-schema.md`** — schema 2 shape, legal statuses, zone list, the `exclusive_paths`
  matching rule, and the exact failure each `test_agent_board.py` assertion catches.
- **`scripts/board_status.py`** — one read-only command that prints your identity, the next free
  task, and an overlap pre-check for your working-tree changes.

## Common mistakes

- Coding first and claiming after — the claim is the lock, not a formality.
- Claiming but not pushing — other sandboxes cannot see an unpushed board change.
- Editing `bot/handlers.py` (the highest-conflict file) without an explicit board note and a claim.
- Treating `show` as pure: it GCs expired leases and may rewrite the board. Classify
  `git diff .agents/board.json` before discarding anything.
- Running the full gate suite while another agent holds `gates_owner`.
