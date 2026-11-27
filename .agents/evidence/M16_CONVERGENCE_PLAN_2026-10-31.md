# Open-PR convergence plan (M16)

Companion to `M16_TRUE_CLOSEOUT_2026-10-31.md`. Live data:
`.agents/evidence/PR_REGISTRY_2026-10-31.json` (`main ab56becc`, open **42**).

Goal: reduce the open-PR backlog to a mergeable, verified set without carrying a
single stale revert, and without weakening any gate.

## Current distribution (live)

| tier | count | action |
|---|---|---|
| `CONFLICT` | 25 | rebase-or-close triage (below) |
| `DRAFT` | 11 | leave until the author marks ready |
| `CI_RED` | 5 | #295 (rerun: runner disk), #296, #283, #97, #64 (real) |
| `REBASE_FIRST` | 1 | rebase |
| `WRONG_BASE_BRANCH` / `MERGEABILITY_UNKNOWN` / `CHANGES_REQUESTED` / `NEEDS_APPROVAL` | 0 | — |

## Per-PR disposition (KEEP / REPAIR / SPLIT / SUPERSEDED)

1. **KEEP** — `#294` (creative graph, law R28), `#295` (asset library). Green
   local evidence; both block only on the required human review.
2. **SUPERSEDED (done)** — `#189` → #294; `#218` → #295. Both closed with a full
   triage comment; their valuable parts extracted, their stale reverts dropped.
3. **SPLIT** — `#218`'s `storage/clock.py` (A-12) becomes its own task: the module
   is self-contained but the lockstep test bans `datetime.utcnow()` in 47 modules
   across many zones. Sequence it as a dedicated migration with per-zone board
   claims; do not fold it into an extraction.
4. **REPAIR (next wave, one PR each)** — any conflicting PR whose diff is *net-new*
   against main: rebase onto `main`, run `agent_board.py check`, then the four
   local gates. PRs whose diff is a **stale revert** of main (the #189/#218 shape)
   are closed as superseded, never force-rebased.
5. **DEFER** — the 11 drafts: no action until `ready_for_review`.
6. **CI_RED** — re-run first (a runner disk-exhaustion false-red is real:
   #295's job died with `No space left on device` while an earlier run of the same
   head was green). Only after a clean re-run is a red treated as a code defect.

## Merge order (main-bound)

1. `#294` creative graph — self-contained, no zone overlap.
2. `#295` asset library — self-contained (`execution/`; `execution-core` lease free).
3. The `clock.py` migration as its own sequenced task.
4. Remaining rebased PRs on disjoint zones, in parallel.

## Guard for every push

`python scripts/agent_board.py check --files <changed> --branch <branch>` must
exit **0** (exit 2 = a source was unreadable = not a pass). `pr_convergence.py
guard <n> --expect-sha <head>` must print `CLEAN` before any merge.

## What this plan explicitly does NOT do

- No force-push to a branch whose diff is a stale revert of main (it would
  reintroduce the regression the reconciliation removed).
- No disabling of a required check to make a PR mergeable.
- No merge without a green `guard` verdict on the exact head SHA.
