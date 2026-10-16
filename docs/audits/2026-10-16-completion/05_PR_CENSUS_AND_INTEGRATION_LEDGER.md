# 05 — PR census (2026-10-16T08:22Z) and the ordered integration ledger

The census was **regenerated** against the live API; the closeout and overnight
tallies are historical and are not reused.

```bash
GITHUB_TOKEN=$(gh auth token) python scripts/pr_census.py \
  --repo bot523h/nexus-ai-agent --reference 218 \
  --overrides docs/audits/2026-10-15-overnight-stabilization/pr_overrides.json \
  --out docs/audits/2026-10-16-completion/pr_census
```

`main` at census time: `6b398565cc1ce0c51fac77d7623350b299c3938f` (unchanged).
Open PRs: **63**. Files: [`pr_census.csv`](pr_census.csv), [`pr_census.json`](pr_census.json).

## States

| State | Count | Meaning |
|---|---|---|
| `REBASE_REQUIRED` | 40 | behind `main`/conflicting; needs a rebase (and, for 51 of the 63, the stale board archive from before A-04 — see below) |
| `BLOCKED_BY_FAILURE` | 11 | red checks on the exact head |
| `DEFERRED` | 7 | distinct work not on the critical path |
| `BLOCKED_BY_GOVERNANCE` | 2 | #208, #213 — no lawful approval path from this environment |
| `REVIEW_REQUIRED` | 1 | #210 (the FS/storage hardening PR) |
| `REFERENCE` | 1 | #218 — this branch |
| `SUPERSEDED_BY_VERIFIED_CHANGE_PENDING_MERGE` | 1 | deferred closure until #218 is merged (evidence-based override, listed in `pr_overrides.json`) |

`touching_board` 51 · `board_in_both_sides` 43 · `dirty_only_because_of_board` 7 —
the last number is the A-04 acceptance metric: seven PRs whose only conflict with
`main` is the archived `.agents/board.json`. The merge-tree proof
(`2026-10-15-overnight-stabilization/a04_merge_tree_proof.json`) already shows 7/7
clean once that archive is restored, and the rebase recipe is one command:
`git checkout origin/main -- .agents/board.json`.

## Ordered integration ledger (recomputed, not inherited)

| Order | PR | Why here | Precondition | Next action | Owner |
|---|---|---|---|---|---|
| 1 | **#218** | the audited baseline every later PR must rebase onto | one approving review from a second account (author cannot self-approve) | `gh pr merge 218 --merge` | repository owner (external) |
| 2 | **#210** | storage/FS hardening + the #205 remainder; 41 files, conflicts with nothing else after #218 | fresh CI on the post-#218 base; file-level comparison against #205 | rebase, then review → merge | owner |
| 3 | **#213** | asset ingest races; depends on the storage helpers #210 lands | its parent integrated | retarget to `main`, fresh CI | owner |
| 4 | **#211**, **#202** | governance/board changes — must be reconciled with the shipped ref-board, keeping claim history | #218's board ref is live | rebase onto the ref implementation; drop any second board file | owner |
| 5 | **#198** | privacy fixes, unique | rebase onto post-#218 `main` | rebase + review | owner |
| 6 | **#122 / #190 / #206** | smaller, independent | ancestry + exact-head CI proven | refresh, review, integrate one at a time | owner |
| 7 | **#118** | Wave 3-C compat repair | its own independent proof, as required by the standing instruction | stays deferred; no claim of "fixed" | Arena A |

Rules applied: no mass-merge, no mass-close, no branch deletion, no force-push,
no umbrella PR. **A PR is only closed as superseded after a file-level comparison
against the merged `main` and a durable written rationale**; #205 and #209 keep
their own states (the #209 remainder is already integrated as `7bfd628`, which is
stated in its own comment thread, but the PR itself is not closed here).

## PRs whose only problem is the board archive (rebase recipe)

For the 51 PRs carrying `.agents/board.json`, the preflight in
`scripts/board_merge_preflight.py` predicts a clean merge once that single file is
taken from `main`. Re-run the proof at any time:

```bash
python scripts/board_merge_preflight.py \
  --census docs/audits/2026-10-16-completion/pr_census.json \
  --out docs/audits/2026-10-16-completion/a04_merge_tree_proof.json
```

## What this ledger does **not** claim

* No PR is "ready" because its title looks similar to an integrated change; the
  file-level comparison is the gate.
* No merge is performed in this session except #218 itself, and only if GitHub
  reports an approving review — which, at the time of writing, it does not.
* The 11 `BLOCKED_BY_FAILURE` PRs are not triaged one-by-one here; each needs its
  own failure read on the exact head before any rebase, and that work is listed as
  the next session's first task rather than asserted.
