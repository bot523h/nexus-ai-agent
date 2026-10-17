# Open-PR convergence — disposition of the 62 other open pull requests

Source of truth for the rows: `pr_convergence_ledger.csv` (62 rows), converted from the live
GitHub read published as PR #218 comment 6197535030 (2026-10-15T16:50Z). Columns: GitHub's
mergeability *against today's `main`*, the number of files each PR shares with #218
(`overlap_with_218` > 0 means a rebase is certain once #218 lands), the disposition in the
project vocabulary, and the evidence / next step.

| Disposition | Count | Meaning | Who acts |
|---|---|---|---|
| `REBASE_REQUIRED` | 56 | CONFLICTING with `main` today and/or overlapping #218. **None was closed**: a close needs a file-level unique-diff comparison against post-#218 `main`, which is only meaningful after #218 merges. | PR authors / next integration session, in the order below |
| `MERGE_ELIGIBLE` | 2 | #122, #190 — small, non-colliding; still need their heads brought current and a review | reviewer |
| `BLOCKED_BY_GOVERNANCE` | 2 | #213 (base = #210's branch), #208 (base = #203's branch) — `merge-base-guard` requires base == `main`; retarget after their parents land | PR authors |
| `BLOCKED_BY_FAILURE` | 1 | #209 — own CI red; kernel rationale refuted on Linux 6.1 (comment 6197534460). **Resolved in #218 commit `7bfd628`**: the valid remainder (typed `FilesystemBoundaryError` for `file/child` + its 5 tests) is integrated; the `O_DIRECTORY` removal is not. Close as `SUPERSEDED_BY_VERIFIED_CHANGE` once #218 is on `main`. | this session (close after merge) |
| `DEFERRED` | 1 | #118 (draft) — stays deferred; never described as fixed | owner |

## Integration order after #218 (serial, one lineage at a time)

1. **#218** — audited baseline (this package). Blocked only by the required approving review.
2. **#210** rebased onto post-#218 `main` (single asset-library lineage; absorb #205's six newer
   commits or close #205 with a file-level comparison), then **#213** retargeted to `main`.
3. **#211** / **#202** governance: reconcile with GOV-1/GOV-2 already in #218; keep generation
   fencing and claim history.
4. **#198** rebase (unique privacy fixes); **#118** stays deferred/draft.
5. **#122**, **#190**, **#206**: small, non-colliding — eligible once current and reviewed.

## Rules applied (and why nothing was force-closed)

* A PR is closed only with a documented reason in the vocabulary
  (`UNIQUE_CHANGES_ALREADY_IN_MAIN`, `SUPERSEDED_BY_VERIFIED_CHANGE`, `DUPLICATE_WITH_PROOF`,
  `CLOSE_WITH_DOCUMENTED_RATIONALE`) and a file-level comparison — "conflicting" alone is not a
  reason.
* Other PRs' code is integrated by cherry-pick onto the session branch with its tests executed
  (as done for #209), never by checking out or rewriting their branches.
* `--admin` merges and branch rewrites are not used; every merge is `gh pr merge <n> --merge`
  after CI is green on the exact head.
* The 148 remote branches are left in place until their PRs are dispositioned; deleting a
  branch with an open PR closes the PR without a record.
