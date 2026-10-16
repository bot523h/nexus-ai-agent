# 07 — Final report (2026-10-16, session 2)

This file replaces the 08:22Z version of the same name. Everything below was
measured again on the tree that carries this document; the previous version's
`8a733a7` head, 269-file count, 81-test count and "0 reviews" line are superseded.

## Where the repository stands

| | |
|---|---|
| `main` | `6b398565cc1ce0c51fac77d7623350b299c3938f` — **unmoved** by this session; no direct commit, no force-push, no tag |
| Work branch | `arena/26f7d958-nexus-ai-agent` — the only branch this session writes to |
| Work head at this session's start | `f930064b48bbde8f0b1096e6563bcbf8d7855bf5` (`f930064`), plus the commit that adds this document |
| PR | [#218](https://github.com/bot523h/nexus-ai-agent/pull/218) — 293 files, +63 117 / −1 787 vs `main`, `MERGEABLE`, `mergeStateStatus=BLOCKED`, `reviewDecision=REVIEW_REQUIRED`, **0 reviews** |
| CI, exact head `337cf70` | [run 38873962245](https://github.com/bot523h/nexus-ai-agent/actions/runs/38873962245) — **success, 21/21 jobs, 22 check contexts** (three consecutive green runs on that head) |
| CI, exact head `f930064` | [run 38889442506](https://github.com/bot523h/nexus-ai-agent/actions/runs/38889442506) — still running at the time this file was written; the three `continuum-evidence` jobs were the only ones left, 18/21 already `success`. **No conclusion is claimed here.** |
| CI for the head that carries this document | recorded in the PR #218 ledger comment posted immediately after this push (run id + per-job conclusions), not asserted in advance |
| Board | `refs/nexus/board` at `d919635` (published by this session's claim renewal); schema 2; **exactly one active claim** — `task-270-full-audit-remediation`, generation 3, `gates_owner=true`, claimed 2026-10-16T08:28:43Z with a 24 h TTL → valid until 2026-10-17T08:28:43Z |

## What was done in this session

1. **Live truth re-read before touching anything**: remote `main`/board SHAs,
   per-job CI conclusions, merge state, the reviewer roster, the collaborator list,
   the board's active claim, and the clone's completeness. The clone was found
   **still shallow** (`main` = 1 commit locally) and was unshallowed
   (`git fetch --unshallow origin` → 576 commits; PR refs re-fetched: 214). Every
   ancestry-based statement in this package was re-derived after that fix.
2. **Recovered the sandbox checkout** from a stale snapshot without losing work
   (tree `4f731758…` compared equal to the remote tip; rescue branch kept local).
3. **Hardened the HuggingFace provider against malformed keys** (P1-1): +9 input
   classes, mutation-proven. Restoring the old parse now fails **20 of 41** tests.
4. **Found and fixed two defects in the census tool itself** (P1-2) — the compare
   direction, and then the *base* of the compare, which had published
   base-relative commit counts under main-relative names for the two stacked PRs
   (#213: claimed 12/19, true 26/6). See
   [`04_DEFECTS_AND_FIXES.md`](04_DEFECTS_AND_FIXES.md).
5. **Regenerated the census and reconciled all 63 open PRs** with categories,
   dependencies, owners and evidence: `NEEDS_REBASE` 40, `NEEDS_FIX` 11,
   `DEFERRED_WITH_OWNER_AND_REASON` 7, `BLOCKED_EXTERNAL` 4,
   `DUPLICATE_OR_SUPERSEDED` 1, **`READY_TO_MERGE` 0**. See
   [`05_PR_CENSUS_AND_INTEGRATION_LEDGER.md`](05_PR_CENSUS_AND_INTEGRATION_LEDGER.md).
6. **Established the decisive governance fact for this repository**: across all 63
   open PRs, `coderabbitai[bot]` submitted **every** review (11 `APPROVED`, 48
   `CHANGES_REQUESTED`, 27 `COMMENTED`, 12 `DISMISSED`); `bot523h` only comments;
   `arena-ai-coding-agent[bot]` submits no reviews. A bot approval satisfies the
   required-review rule — and CodeRabbit is therefore the **only** route to the
   approval #218 needs, which it refuses because the diff exceeds its 100-file cap.
7. **Re-verified the #209 supersession claim** by reproducing it (not inheriting the
   override): all five tests it adds exist by name in the #218 tree, commit
   `7bfd628` is an ancestor, and its rejected `O_DIRECTORY` change is absent.
8. **Acted where action is lawful**: requested the obtainable CodeRabbit review on
   **#210** — 41 files, 0 commits behind `main`, every check green, `reviewDecision`
   `CHANGES_REQUESTED` from two 2026-10-13 reviews whose head (15:46Z) is newer than
   both but was never re-reviewed — as [comment 6214117556](https://github.com/bot523h/nexus-ai-agent/pull/210#issuecomment-6214117556).
   No branch of another PR was pushed to, rebased, or rewritten. (The first version of
   that row said "blocked only by the required review"; the live review list corrected it
   the same hour.)

## Local gates on this tree

| Gate | Result |
|---|---|
| `ruff check src tests scripts` | clean |
| `ruff format --check` | clean (597 files) |
| `mypy --strict src` | 0 errors / 284 files |
| `deptry src` | clean |
| `pytest -m "not slow"` | **4278 passed / 37 skipped** (214.76 s, `rm -rf data` first, exit 0) |
| `tests/unit/test_docs_integrity.py` | pass (this package is indexed in `docs/README.md`) |
| mutation: HuggingFace parse reverted | **20 of 41** provider tests fail on the old behaviour; restored → 41 green |
| mutation: board referee | **14/14 killed**, baseline restored green |

Local results are evidence about the tree, **not** a substitute for CI. The
canonical result for an exact head is the CI run on that head.

## Statuses, honestly applied

| Item | Status | Why |
|---|---|---|
| A-04 board-ref remedy | `REMEDIATED_CI_VERIFIED_PENDING_MERGE` | implemented with adversarial tests and a live ref read; the production proof (a foreign claim landing after merge) has not happened |
| A-01…A-19 remediation set | `VERIFIED-on-PR` (not merged) | reviewed and CI-green on this branch; **not** on `main` |
| P1-1 HuggingFace key parsing | `REMEDIATED_WITH_TESTS` (on this PR) | mutation-killed; exact-head CI in the ledger comment |
| P1-2 census tool | `REMEDIATED_WITH_TESTS` | 7 tests in `test_pr_census_compare.py`; regenerated artifacts |
| Module audit | 284/284 dispositioned, 0 deferred | method and limits in [`03_MODULE_DISPOSITION.md`](03_MODULE_DISPOSITION.md) |
| PR #218 | **BLOCKED** (external reviewer capacity) | protections are not bypassable from this environment |
| PR #210 | **BLOCKED** pending a re-review | green, level with `main`, 41 files ≤ 100; two open change requests from 2026-10-13 that the newer head never answered on the record |
| Other 61 PRs | `NEEDS_REBASE` / `NEEDS_FIX` / `DEFERRED` / `SUPERSEDED` | one row each in [`pr_reconciliation.md`](pr_reconciliation.md) |
| Backups | `BLOCKED_BY_EXTERNAL_DEPENDENCY` | run 38872121719 fails the preflight: `NEXUS_DATABASE_URL R2_ACCOUNT_ID R2_ACCESS_KEY_ID R2_SECRET_ACCESS_KEY R2_BUCKET` are absent. **Secret names only — no values anywhere in this package.** |
| Dependency-audit workflow | `BLOCKED` until it is on the default branch | `gh workflow run dependency-audit.yml` → HTTP 404 while the workflow exists only on this branch |
| Container / deployment | container probe green in CI; **live deployment not performed** | no deployment access from this environment |

Nothing in this session is described as merged, deployed, or production-verified.

## The one actionable blocker

#218 needs exactly one approving review, and the only account that approves in this
repo is CodeRabbit, which skips the PR:

```
Review skipped: 182 files exceed the limit of 100.        (comment 6197253259)
```

`gh pr merge 218 --merge` therefore refuses, and the refusal was observed, not
assumed:

```
$ gh pr merge 218 --merge
X Pull request #218 is not mergeable: the base branch policy prohibits the merge.
$ echo $?
1
```

`--admin` and `--auto` were **not** used, and the head was **not** rewritten to
dodge review. The owner has three honest routes:

1. raise the CodeRabbit limit (plan or organization settings) and re-trigger;
2. set review-scope path filters in CodeRabbit's organization settings — note that
   a repo-level `.coderabbit.yaml` narrowing to `src/**` was considered and
   **rejected** here: it would permanently remove review coverage from tests,
   migrations and workflows, and would leave any future docs/test-only PR with
   nothing reviewable at all;
3. split the delivery into PRs of ≤100 files (which is what the bot itself
   suggests) — heavier, and it costs the single exact-head CI evidence this branch
   already holds.

Until one of those happens, #218 stays where it is: implementation complete,
locally and CI-verified, and **not** merged.

## Ordered next actions

```bash
# A. after the owner unblocks review capacity (route 1/2), or after a <=100-file split:
gh pr merge 218 --merge                    # no --admin, no --auto
git fetch origin main && git log --oneline -1 origin/main
gh run list --branch main --limit 3

# B. #210: the review request is already placed (comment 6214117556)
gh pr view 210 --json reviewDecision,mergeStateStatus

# C. backups: add the five repository secrets, then
gh run list --workflow=maintenance.yml --limit 1   # expect the preflight to pass

# D. dependency audit: merge this branch first, then
gh workflow run dependency-audit.yml --ref main

# E. first engineering item after #218 lands (unchanged, still out of #218's scope):
#    CommandBus per-command deadline — acceptance in the overnight package's
#    05_COMMANDBUS_AUDIT.md.
```
