# External blockers — what cannot be closed from inside the repository

Each item names the blocker, the proof that it is external, and the exact owner action.
Nothing here is a wish; every entry was attempted and the refusal recorded.

| # | Blocker | Evidence of the attempt | Owner action (prerequisite) |
|---|---|---|---|
| 1 | **#218 cannot be merged by its author.** `gh pr merge 218 --merge` → "base branch policy prohibits the merge" (`REVIEW_REQUIRED`, 0 approving reviews). The author account (`bot523h`) cannot approve its own PR; `--admin` bypass was deliberately not used; splitting the PR would be a branch rewrite (forbidden). | PR #218 ledger comments 6197535030 / 6198204063; `gh pr view 218 --json reviewDecision` | Approve #218 from a second account (or temporarily relax the review requirement for `main`), then run `gh pr merge 218 --merge` and let CI run on the merged SHA. |
| 2 | **CodeRabbit will not review #218.** OSS plan caps a review at 100 changed files; #218 changes 180+. A skipped review still reports commit status *success* (vacuity trap if it is ever made a required check). | CodeRabbit comments 6194673421, 6197253259; `@coderabbitai full review` refused | Either accept a human review (item 1) or upgrade the CodeRabbit plan; do **not** add `path_filters` to shrink the count — that hides files from review. |
| 3 | **Backups are unverifiable.** The maintenance workflow needs `NEXUS_DATABASE_URL`, `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET`; none is configured (run 38704873852 failed on missing secrets; the secrets API answers 403 to the token in use). The backup/restore *code path* is covered by `tests/unit/test_maintenance_workflow_contract.py` and the PostgreSQL CI leg, but no real backup has been produced or restored. | run 38704873852; `gh api repos/…/actions/secrets` → 403 | Add the five secrets, dispatch the maintenance workflow once, then run the documented restore drill (`docs/ops/DEPLOY_RUNBOOK.md`) and attach `pg_restore --list` output to the run. Until then: `BLOCKED_BY_EXTERNAL_DEPENDENCY`. |
| 4 | **Container runtime proof is CI-only.** No Docker in the sandbox; the `container` CI job builds and probes the image with stand-in processes (no Telegram token in CI). | `SECURITY_AND_CONTAINER.md` §3 | After the first real deployment record `docker inspect --format '{{.State.Health.Status}}' <container>` (expect `healthy` after the 120 s start period) → A-16 becomes `VERIFIED`. |
| 5 | **No live-provider / real-engine validation.** Every LLM, embedding and native-engine path is tested with fakes; nothing in this package claims a live model answered. | mission constraint; no secrets in CI | Owner-run smoke with real keys, recorded as a dated note in `docs/audits/` — never as a CI job that needs production secrets. |
| 6 | **Releases lag the repository.** Version `3.13.0`, newest tag `v3.5.0`, newest GitHub Release `v3.3.0` (2026-05-30). The `release-lineage` job reports this as *open*. | `scripts/release_lineage.py --sha HEAD` | After #218 is on `main` and CI on the merged SHA is green: tag `v3.13.0` on that SHA (or bump to `3.14.0` with a CHANGELOG release section) and publish the GitHub Release. |
| 7 | **56 open PRs need rebases that only make sense after #218 lands.** | `PR_CONVERGENCE.md` | Follow the integration order; each PR gets a documented disposition before any close. |
| 8 | **Dependency advisories without fixes** (`chromadb`, `diskcache`). | `TECHNICAL_DEBT_LEDGER.md` §6 | Watch for fixed releases; pin and upgrade when published. Exposure is limited (embedded client only / transitive). |

## What is **not** blocked

Everything inside the repository that this brief scoped is done: gates green, every finding
A-01…A-19 and GOV-1/2 has either a fix with an executed regression test, a declared
`VERIFIED_WITH_LIMITATIONS` with the limitation named above, or — for A-04's structural remedy
(board state out of the merge path) — an explicit `HARDENED_BUT_NOT_COMPLETE` entry in the debt
ledger (§8) with its acceptance test. CI is a superset of the local gates and the evidence is in
Git. The single action that unblocks the mainline is item 1.
