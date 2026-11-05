# NEXUS-OMNI — response to the independent audit (2026-11-05)

An independent reviewer audited the three delivered PRs (#316, #317, #318) and
found real gaps the author's own report had not stated. This note records, per
finding, what was **verified in code**, what was **fixed**, and what remains.
Nothing here is claimed without a reproduction command.

Live base: `main` @ `aadaf53157670b64af6978fcf41b592e108ac9ae` (2026-11-05).

## P0 — the board could be opened by a forged merge SHA (fixed, #316)

**Finding (confirmed).** `cmd_record_pr_merge` hard-coded
`evidence.source = "github_api"` while trusting an operator-supplied
`--merge-sha`; it only checked the string was 40 hex. `_pr_satisfied` then
accepted any such record, so a fake SHA (`"a"*40`) opened a typed
`pull_request` dependency. The author's own test asserted that forged record
satisfied the dependency — the gap was written into the suite.

**Fix.** `record-pr-merge` now calls `_verify_pr_merge`:
the PR must really be `merged`, its base must be `main`, the API's
`merge_commit_sha` must equal the optional `--merge-sha` cross-check, and that
commit must be an ancestor of `origin/main` (`git merge-base --is-ancestor`).
Any network/parse/ancestry failure **refuses** (fails closed). `--merge-sha` is
now optional; the recorded SHA always comes from the API.

**Evidence.** `tests/unit/test_agent_board.py` monkeypatches `_gh_get` (no
network) and asserts refusal for: a non-merged PR whose SHA *is* a real ancestor
(isolates the merged-state guard), a non-`main` base, a `--merge-sha` mismatch,
and an SHA that is not an ancestor — plus acceptance for a real ancestor. The
referee mutation campaign grew **33 → 37**; all four new mutants are killed.

## P0 — `completed_released` satisfied prerequisites (fixed, #316)

**Finding (confirmed).** `SATISFIED_STATUSES` contained `completed_released`
while the file's own comment said an epic that released its lease with open
slices "must keep blocking them". So the released epic satisfied Slice 2.

**Fix.** Removed `completed_released` from `SATISFIED_STATUSES`; only `done`
(and the historical `completed_merged`) satisfy a prerequisite. A released task
is still *claimable again*; it just is not *complete*. The `release` message was
corrected to say dependents stay blocked until `--complete`.

**Evidence.** `test_a_completed_released_dependency_does_not_satisfy_a_prerequisite`
and the `released_status_satisfies_a_prerequisite` mutant (killed).

## P0 — task-282 Slice 2 dependency corrected (fixed, #317)

**Finding (confirmed).** Slice 2 depended on the epic id, whose status is now
(correctly) not a satisfier — an unsatisfiable, mislabeled gate.

**Fix.** Slice 2 now depends on the **merged** Slice 1 as a typed
`pull_request` dependency (PR #315), satisfied by the existing verified
completion record. The record's `recorded_at` was corrected to the real GitHub
merge time (`2026-11-05T13:13:23Z`); the epic note no longer says
`active_in_review`.

## P1 — dependency gate hardening (fixed, #318)

- **`no-fix-released` now self-expires.** `_pin_still_holds` consumes
  pip-audit's own `fix_versions`; a non-empty list means a fix exists, so the
  acceptance goes STALE and reds the gate. (Previously it held forever while the
  package stayed installed.)
- **Duplicate advisories no longer drop.** `_collect_ids` keys by
  `(package, advisory_id)` instead of id alone, so one advisory id reported for
  two packages yields two findings, both examined.
- **Merge-blocking, not advisory.** The audit now also runs inside the
  **required** `test` job (branch protection requires only `lint`, `test`,
  `merge-base-guard`; the standalone `dependency-audit` job is *not* required).
- **Anti-vacuity strengthened.** The empty-allow-list step asserts the gate's own
  `UNACCEPTED` verdict (and the absence of `STALE`/`MISMATCH`), not merely a
  non-zero exit — which a `pip-audit` error could also produce.

**Evidence.** 15 gate tests pass; live gate green; the anti-vacuity shell logic
was run locally (PASS).

## P2 — evidence hygiene (fixed, #318)

The `2026-11-06` audits were future-dated (the session is 2026-11-05); renamed
to `NEXUS_OMNI_DEPENDENCY_GATE_2026-11-05.md` and
`NEXUS_OMNI_MORNING_HANDOFF_2026-11-05.md`, and the stale head/run SHA in the
handoff corrected to `16ec17ee` / run `42076117097`.

## Accepted, with reason

- **`redelivery` claim `expired` on `main`.** PR #317 is board-data only and
  does not settle `task-nagar-workspace-redelivery-01`; it is left for a
  dedicated, evidence-bearing change rather than re-declared here.
- **`task-282` still `active_in_review` on `main`.** True until #317 merges; #317
  carries the remodel.

## Still NOT proven (unchanged by this round)

- Production deploy / restore — no production access → **NOT_PROVEN**.
- The remaining ~42 open PRs were not re-triaged here; the authoritative
  classification remains
  `docs/audits/NEXUS_OMNI_PR_CONVERGENCE_LEDGER_2026-11-03.md`.
- NAGAR Studio end-to-end (Studio intent → artifact) remains unproven.

## Merge order

**#318 → #316 → #317** (#317's board uses the #316 engine and depends on it).
Every head must re-run the required checks green immediately before merge.
