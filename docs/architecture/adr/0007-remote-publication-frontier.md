---
status: accepted
date: 2026-09-28
deciders: arena/01a0e742-nexus-ai-agent (principal engineer session)
consulted: live `git ls-remote` frontier, PR #113/#116/#96 boards, board schema 2 tests
---

# 0007. Publication approval is a function of the observed remote frontier, not the working tree

## Context and Problem Statement

The coordination contract (AGENTS.md rule 2) approved publication with a local-board
`check --files` computed from a developer-supplied list. Two blind spots were demonstrated
live on 2026-09-28:

1. **Diff-scope blind spot.** A file introduced by an *older* outgoing commit and then
   reverted by a newer one is absent from `git diff <remote>..HEAD`, yet its content is
   part of the published history. `tests/unit/test_agent_board_remote.py::test_real_git_sees_old_commit_even_after_revert`
   reproduces this on a disposable repository: the candidate commit reintroduces a
   lease-conflicting file, a revert hides it from the diff, and the guard still rejects
   (`scripts/agent_board_remote.py::inspect_publication` walks `rev-list` + `diff-tree -m`
   over the whole outgoing range).
2. **Board-copy blind spot.** A branch may hold an *inherited, stale* copy of another
   owner's lease marked released, while the owner's actual head still fences the zone.
   The PR #112/#113 session of 2026-09-28 showed a local "safe" verdict that the live
   remote contradicted.

Publication is the one operation that cannot be rolled back for other sandboxes; its
premise set must therefore be observed, not assumed.

## Decision Drivers

- A push publishes **every** outgoing commit (merge bases included), not the final diff.
- Leases are issued by owners on their own heads; third-party copies are hearsay.
- Missing, malformed, or moving evidence must produce `NOT_VERIFIED` (exit 2), never a
  green light; known conflicts produce `REJECTED` (exit 1).
- The referee must stay pure-stdlib, deterministic, and testable on disposable repositories.

## Considered Options

1. Keep `check --files` as the approval (status quo).
2. Derive ownership from the GitHub PR API (files of open PRs).
3. **Observed remote frontier + full outgoing-range evaluation** (chosen).

## Decision Outcome

Chosen option: **3**. `scripts/agent_board.py preflight --branch <branch>` (backed by
`scripts/agent_board_remote.py`) now:

- observes `git ls-remote --heads <remote> 'main' 'arena/*'` (the *publication frontier*),
- requires a clean single fetch URL == push URL,
- refuses shallow or grafted history,
- enumerates **all** outgoing commits (`merge-base --is-ancestor` + `rev-list`) and their
  union of changed paths (`diff-tree --root -m --no-renames` — merge parents included),
- evaluates leases with **owner-head precedence**: an inherited claim is skipped only when
  the owner's own head can speak; otherwise a live inherited claim is
  `owner_head_missing` → `NOT_VERIFIED`,
- treats ambiguous active-lease metadata (unparseable time, non-positive/non-finite TTL,
  future timestamps, escaping or malformed exclusive paths, unknown `active*` statuses)
  as errors, never as "no lease",
- re-checks the frontier and local HEAD after evaluation and rejects any movement,
- binds the verdict to a `frontier_sha256` over the observed state.

The local `check` remains available as an **advisory** aid during development; it is no
longer described as publication approval. Exit codes: `0` SUCCESS (observed range only),
`1` REJECTED, `2` NOT_VERIFIED.

### Consequences

- (+) The two demonstrated blind spots fail closed instead of silently allowing a push.
- (+) Evidence is a JSON document bound to OIDs; a review can audit what was actually seen.
- (−) `NOT_VERIFIED` is now a common, honest outcome (fresh clones without history,
  network failure, malformed legacy boards) and requires an explicit fix, not a retry ritual.
- (−) Legacy boards with semantically dead statuses must be calibrated explicitly
  (2026-09-28 calibration: `active_in_review_PR33`, `done_partial_residue_queued`,
  `queued_reserved_for_B`, empty `exclusive_paths` lists on live leases).

## Confirmation

- `tests/unit/test_agent_board_remote.py` — 13 cases including real disposable Git
  repositories (`git init --bare` remotes), revert concealment, frontier movement,
  fetch/push mismatch, malformed boards, inherited-release precedence.
- `tests/unit/test_agent_board.py` — existing board CLI semantics stay green (40 total).
- Live runbook: `docs/ops/INTEGRATION_PREFLIGHT.md` records the 2026-09-28 executions
  (`shallow_history` discovery, legacy-status calibration, final SUCCESS).

## More Information

Board-layer decision; the five-rules contract it amends lives in `AGENTS.md`.
Complements `praudit` (PR visibility), which stays advisory as well.
