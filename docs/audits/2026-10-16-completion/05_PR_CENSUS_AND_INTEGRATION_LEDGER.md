# 05 — PR census (2026-10-16T11:04:15Z) and the per-PR reconciliation

This document replaces the 08:22Z version of the same file. It was regenerated
because **the census tool itself was wrong about stacked PRs** (defect 2 below);
the old numbers for #208 and #213 are not carried over.

```bash
GITHUB_TOKEN=$(gh auth token) python scripts/pr_census.py \
  --repo bot523h/nexus-ai-agent --reference 218 \
  --overrides docs/audits/2026-10-15-overnight-stabilization/pr_overrides.json \
  --out docs/audits/2026-10-16-completion/pr_census          # rc=0, 131 s

python scripts/pr_reconcile.py \
  --census docs/audits/2026-10-16-completion/pr_census.json \
  --dispositions docs/audits/2026-10-16-completion/pr_dispositions.json \
  --reference 218 \
  --out docs/audits/2026-10-16-completion/pr_reconciliation  # rc=0
```

`main` at census time: `6b398565cc1ce0c51fac77d7623350b299c3938f` (unchanged).
Open PRs: **63**. Files: [`pr_census.csv`](pr_census.csv),
[`pr_census.json`](pr_census.json), [`pr_dispositions.json`](pr_dispositions.json),
[`pr_reconciliation.md`](pr_reconciliation.md) (+ `.csv` / `.json`).

## Two tooling defects found and fixed before this ledger was trusted

| # | Defect | Effect | Fix + guard |
|---|---|---|---|
| 1 | `pr_census.py` compared `<head>...<base>` (2026-10-16, earlier) | GitHub's `ahead_by`/`behind_by` were published under each other's names; #218 read "26 behind" although `main` is its ancestor | compare `main...<head>`; `tests/unit/test_pr_census_compare.py::test_compare_is_asked_in_the_base_then_head_direction` |
| 2 | the compare ran against **each PR's own base branch**, and the row then published those numbers under main-relative names | for the two stacked PRs the ledger would have ordered merges wrongly: #213 read `unique_commits=12` / `commits_behind_main=19` while the main-relative truth is `26` / `6` | new `_main_relative_counts()`; a second `main...<head>` compare whenever `base_ref != main`; base-relative pair renamed `unique_commits_vs_base` / `base_commits_not_in_head`; `changed_files_vs_main` added; 3 new tests (7 in that file) |

Stacked PRs are exactly two of the 63: **#213** (base = #210's branch) and
**#208** (base = #203's branch). Their base-relative and main-relative numbers are
both recorded now, so neither can be quoted as the other.

## States (this run, `auto_state`)

| State | Count | What it means here |
|---|---|---|
| `REBASE_REQUIRED` | 40 | conflicts or a moved base; needs the base taken into the branch and CI re-run |
| `BLOCKED_BY_FAILURE` | 11 | at least one check on the exact head is red |
| `DEFERRED` | 7 | drafts |
| `BLOCKED_BY_GOVERNANCE` | 2 | stacked on another PR's branch (#213 → #210, #208 → #203) |
| `REVIEW_REQUIRED` | 1 | green, level with `main`, waiting only on the required review (#210) |
| `SUPERSEDED_BY_VERIFIED_CHANGE_PENDING_MERGE` | 1 | #209, whose remainder is already inside #218 (verified below) |
| `REFERENCE` | 1 | #218 |

Review decisions (collected per PR over GraphQL at 10:5xZ, `pr_review_states.json`):
`APPROVED` 9, `CHANGES_REQUESTED` 16, `REVIEW_REQUIRED` 36, none 2.
Merge states: `DIRTY` 49, `BEHIND` 11, `BLOCKED` 2, `UNSTABLE` 1.

## The reviewer roster — the decisive fact for this repository

Across all 63 open PRs, **one entity has ever submitted a review**:

```
48 coderabbitai[bot]|CHANGES_REQUESTED
27 coderabbitai[bot]|COMMENTED
12 coderabbitai[bot]|DISMISSED
11 coderabbitai[bot]|APPROVED
18 bot523h|COMMENTED          <- comments only, never a review state
```

`arena-ai-coding-agent[bot]` submits no reviews at all. So a bot approval *does*
satisfy the required-review rule (those 11 approvals produced `reviewDecision:
APPROVED`), and **CodeRabbit is the only route to the required approval for any
PR in this repository** — including #218, whose 293-file diff CodeRabbit refuses
("Review skipped: 182 files exceed the limit of 100", comment 6197253259).

## Reconciliation result (63/63 dispositioned)

| Category | Count | Note |
|---|---|---|
| `NEEDS_REBASE` | 40 | mostly long-lived branches behind `main` |
| `NEEDS_FIX` | 11 | red checks on the exact head |
| `DEFERRED_WITH_OWNER_AND_REASON` | 7 | drafts, owner = repository owner |
| `BLOCKED_EXTERNAL` | 4 | #218, #210 (review), #213, #208 (stacked) |
| `DUPLICATE_OR_SUPERSEDED` | 1 | #209 |
| `READY_TO_MERGE` | **0** | no PR on this board satisfies every protection today |

26 rows carry hand-written categories with reasons, dependencies and owners
(`category_source=hand`); the remaining 37 are derived from the live census row by
the documented rules in `scripts/pr_reconcile.py`. **No PR was closed and no
branch was deleted by this reconciliation.**

## Rows that matter for the merge order

| PR | head | files@main | unique@main | behind | category | dependency / next action |
|---|---|---|---|---|---|---|
| #218 | `f930064` | 293 | 26 | 0 | BLOCKED_EXTERNAL | CodeRabbit's 100-file cap; owner must raise the cap or split ≤100 files |
| #210 | `1d7390c` | 41 | 27 | 0 | BLOCKED_EXTERNAL | review requested 2026-10-16 (comment 6214117556); merges after #218 (shared `.github/workflows/ci.yml` + storage/fs sources) |
| #213 | `29ca3d6` | 42 | 26 | 6 | BLOCKED_EXTERNAL | stacked on #210's branch and dirty against it; restack after #210 |
| #208 | `45b5e17` | 1 | 2 | 6 | BLOCKED_EXTERNAL | stacked on #203's branch; `merge-base-guard` red because the base moved |
| #211 | `878842b` | 8 | 4 | 1 | NEEDS_FIX | test + python-parity red; shares `scripts/agent_board.py` with #202 |
| #205 | `ff2792b` | 43 | 15 | 6 | NEEDS_FIX | four red checks; Asset Library collision with #213 |
| #209 | `62bef4f` | 2 | 3 | 6 | DUPLICATE_OR_SUPERSEDED | close **after** #218 is on main, citing the evidence below |

### #209 supersession — reproduced, not inherited

The overnight package recorded this as an override claim. It was re-verified
against the trees on 2026-10-16:

* all **5** tests #209 adds are present **by exact name** in the #218 tree
  (`test_a_fifo_as_an_intermediate_component_never_blocks`,
  `test_a_fifo_in_the_workspace_never_blocks_directory_opens`,
  `test_a_path_through_a_regular_file_is_a_typed_boundary_error`,
  `test_final_symlink_to_a_directory_is_refused_by_every_directory_primitive`,
  `test_intermediate_symlink_and_traversal_are_refused`);
* the introducing commit `7bfd628` — "re-scoped remainder of #209" — is an
  ancestor of the #218 head;
* #209's rejected change (dropping `O_DIRECTORY`) is **absent** from the #218
  tree, which still defines `O_DIRECTORY`;
* #209's own head is red (`test`, `python-parity` 3.10/3.11/3.12), so it must not
  be merged as-is regardless.

## Board-archive collisions

51 of 63 PRs touch `.agents/board.json`; `board_in_both_sides` is true for the
same 51. The A-04 ref cutover (`refs/nexus/board`, already on `main` as `5bc146e`)
is what removes this class of collision for *new* work; **no PR is currently
`dirty_only_because_of_board`**, so the board archive is not the binding conflict
on any open PR today.

## What this ledger deliberately does not do

* No PR was merged, closed, rebased or force-pushed here.
* Every PR except #218 belongs to another branch, and this session writes only to
  `arena/26f7d958-nexus-ai-agent`; the rows therefore name an owner per PR
  instead of performing branch surgery on someone else's branch.
* Counts are from the live API at the timestamps above; a later push changes them,
  and re-running the two commands above is the only way to refresh them.
