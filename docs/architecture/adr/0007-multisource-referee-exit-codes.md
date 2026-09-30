---
status: accepted
date: 2026-09-30
deciders: arena/hardening-task207-foreign-check
consulted: .agents/board.json (task-207, task-219)
informed: all agents sharing the repository
---

# 0007. The overlap referee reports unverifiable data as exit 2, not as a pass

## Context and Problem Statement

`scripts/agent_board.py check` is the mechanical answer to "may I push this?" —
it must never answer "no overlap" from data it did not actually read. The
pre-task-207 implementation loaded only the **local** `.agents/board.json`
(`load_board()`), so a lease that existed only in another sandbox, in a sibling
git worktree, or already pushed to `origin/main` was invisible: the referee
returned `0` — a green light — while a foreign lease was live. There was also no
distinction between "I read everything and it is clean" and "I could not reach a
source", which is exactly the asymmetry that makes a referee untrustworthy.
Board task: `task-207-multisource-check`.

## Decision Drivers

- **Honesty over green** — a pass must be a claim about *read* data, not about
  the absence of an error.
- **Cross-sandbox reality** — agents coordinate only through git; the local file
  is the weakest of the available sources.
- **Low blast radius** — `check` is a read-only referee used before every push;
  a wrong `0` is a merge collision, a wrong `2` is a retry.
- **Backward compatibility** — existing callers (pre-push hooks, CI) must keep
  working without a flag change.

## Considered Options

1. **Keep local-only `check`, add a separate `--remote` opt-in command.**
2. **Make multi-source the default, with a three-valued exit (`0` clean / `1`
   overlap / `2` unverifiable) and an explicit `--no-remote` narrow mode.**
3. **Make multi-source the default but keep the old two-valued exit (`0`/`1`),
   treating unreadable sources as clean.**

## Decision Outcome

Chosen option: **2**, because it is the only one where "no overlap" is always a
statement about data that was actually read. The referee now consults the local
board, every sibling git worktree's board (`git worktree list --porcelain`), and
`origin/main`'s board (`git fetch` + `git show`, never mutating the tree). Any
source that cannot be read forces exit `2`; a *proven* overlap is exit `1` even
when another source is unreadable, because the conflict is confirmed and hiding
it behind `2` would be worse. `--no-remote` narrows to the local board and prints
an explicit `NOT a global pass` scope line, so a local verdict can never be
mistaken for a global one.

### Consequences

- (+) A pre-push hook or CI job can now trust `0`: it means every consulted
  source was readable and disjoint.
- (+) A network outage or a missing worktree fails loudly (`2`) instead of
  silently green-lighting a colliding push.
- (−) `check` now runs `git fetch origin main`, so it is no longer offline by
  default; `--no-remote` restores the old behaviour for offline/diagnostic use.
- (−) Callers that treated any non-zero exit as "overlap" must now distinguish
  `1` from `2` (documented in `docs/MULTI_AGENT_PROTOCOL.md` §4).
- (~) The `git` dependency is isolated behind one helper (`_git`) that returns
  `None` on any start failure, which callers map to "unverifiable".

## Confirmation

- `tests/unit/test_agent_board.py::test_check_unreadable_source_is_exit_2_never_a_pass`
- `tests/unit/test_agent_board.py::test_check_unreliable_worktree_enumeration_is_exit_2`
- `tests/unit/test_agent_board.py::test_check_consults_sibling_worktree_boards`
- `tests/unit/test_agent_board.py::test_check_no_remote_narrows_the_claim_loudly`
- `tests/unit/test_agent_board.py::test_check_proven_conflict_wins_over_unreadable_remote`
- `scripts/board_referee_mutations.py` — kills all nine mutations (the four
  referee invariants — unreadable-source-as-pass, ignored `--no-remote`, skipped
  worktree boards, downgraded conflict — plus five lease/governance guards added
  by task-219/220: unreadable local board as pass, ignored fencing guard,
  non-advancing takeover/release epochs, and a `gc` that clobbers the owner's
  evidence note) and restores `scripts/agent_board.py` byte-for-byte
  (sha256 checked).

## Pros and Cons of the Options

### Option 1 — separate opt-in command

- (+) Zero change to the existing default.
- (−) The unsafe path stays the default; an agent that forgets the flag is
  silently green. Rejected: honesty must be the default.

### Option 2 — three-valued default (chosen)

- (+) `0` is always meaningful; unverifiable data is never a pass.
- (−) Adds a `git fetch` on the default path and a new exit code to document.

### Option 3 — three sources, two exit codes

- (+) No new exit code for callers to handle.
- (−) Collapses "clean" and "could not verify" into the same green light — the
  exact failure this decision exists to remove. Rejected.

## More Information

- `docs/MULTI_AGENT_PROTOCOL.md` §4 — command reference and exit-code table.
- ADR [`0004`](0004-board-schema-2.md) — board schema and claim semantics.
- Board tasks `task-207-multisource-check` (this decision) and
  `task-219-lease-fencing` (the follow-up: generation/epoch fencing).
