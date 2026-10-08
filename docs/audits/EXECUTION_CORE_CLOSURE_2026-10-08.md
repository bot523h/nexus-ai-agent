# NEXUS V1 Execution Core — Live Closure Record

**Task:** `task-254-execution-core-v1`
**PR:** `#191`
**Branch:** `arena/execution-core-v1-nexus-ai-agent`
**Evidence capture:** 2026-10-08

> This record captures the live PR state at tip `118af102…` immediately before this report-only publication commit; the publication commit changes documentation only.

> **Verdict at capture:** `HARDENED_BUT_NOT_COMPLETE`
>
> The execution-core implementation and local proofs are hardened and green, but this record does **not** claim final closure: the current-head GitHub CI is still pending, CodeRabbit has no newly submitted review record for the current head, and the repository board has no valid live `gates_owner`.

## 1. Live truth

| Item | Current evidence |
|---|---|
| Repository | `bot523h/nexus-ai-agent` |
| `origin/main` | `48f280c5a1f1f595aa713100f73724651944c871` |
| Current PR tip at final audit | `3cd735b64a95b6a30938f6f60148b7a3237a494b` |
| Branch | `arena/execution-core-v1-nexus-ai-agent` |
| Base | `main` at `48f280c5a1f1f595aa713100f73724651944c871` |
| PR state | Open, not merged, not closed |
| Merge state | `MERGEABLE`; exact-SHA CI terminal green (see §8) |
| Working tree | Clean after fast-forwarding to the live branch before this report-only update |

The current PR tip `3cd735b…` is report-only history: since the unchanged
technical evidence parent `edcdc733…` the only non-doc change is the board
claim note (M1–M6 → M1–M7). No execution code changed.

The immediately preceding implementation commit was:

- `edcdc733` — align cancellation race tests with cancellation-reschedule semantics.

The complete execution-core history from `origin/main` to the implementation evidence head is explicitly:

1. `e28f082` — provider-neutral contract and attempt-scoped staging
2. `6132ebc` — `NativeLocalBackend` and fenced queue cancellation
3. `ddfb963` — execution-core board claim
4. `35b3c85` — contract, staging, backend, and invariant tests
5. `f82a615` — board lease renewal
6. `2fc224f` — native-core fencing review fix
7. `08060d5` — forensic closure, staging hardening, race/crash/mutation proofs
8. `a50d874` — initial forensic closure evidence report
9. `d0ab29f` — concurrent directory-creation race fix and M7 proof
10. `525cb29` — cancellation cleanup and successor scheduling
11. `edcdc73` — cancellation-race test alignment
12. `44b9322` — live closure report correction
13. `5a270b4` — parallel full-suite report note
14. `1825313` — non-destructive merge preserving the authoritative report
15. `118af10` — final live closure evidence report publication

## 2. Task-254 acceptance criteria

### AC1 — Execution Contract: PASS locally

`src/nexus_ai_agent/execution/contract.py` exposes exactly the provider-neutral backend verbs:

- `submit`
- `observe`
- `cancel`
- `reconcile`

It does not expose `execute`, imports no provider SDK, and remains an intent/contract layer rather than an authority. Evidence:

- source inspection;
- `tests/architecture/test_execution_contract_boundary.py`;
- `tests/unit/test_execution_contract.py`;
- focused suite green on the current code head.

### AC2 — One Queue / One Authority: PASS locally

`NativeLocalBackend` accepts an injected `InProcessJobQueue`. Architecture and source checks prove that it does not construct a queue, open a database, create a table, or introduce a second persistence/execution authority.

Evidence:

- `test_backend_never_constructs_a_queue`;
- `test_backend_opens_no_database_and_creates_no_table`;
- `test_backend_does_not_define_a_queue_or_persistence_class`;
- focused suite green.

### AC3 — Attempt-scoped staging: PASS locally

Staging is rooted at `root / job_id / attempt_id / staging` and reuses the descriptor-relative `WorkspaceFilesystem` boundary. Traversal, symlinked ancestors/components, unsafe staged sources, non-regular sources, unsafe destinations, quarantine redirection, and provider-controlled paths are fail-closed.

Evidence:

- `tests/unit/test_execution_staging.py`;
- `tests/unit/test_filesystem_policy_primitives.py`;
- `tests/architecture/test_execution_boundary_enforcement.py`;
- focused suite green.

### AC4 — T1/T2 fencing: PASS locally

The real file-backed SQLite queue proves both required races:

- attempt N is superseded by attempt N+1; the stale completion CAS is refused while the current attempt can complete;
- a stale attempt-scoped identity cannot cancel the current attempt; current cancellation is accepted only for the current fencing token.

Evidence:

- `test_stale_attempt_cannot_complete_after_takeover`;
- `test_c7_late_stale_worker_is_refused_without_a_success_notice`;
- `test_stale_identity_cannot_cancel_a_newer_attempt`;
- `test_completion_vs_cancellation_exactly_one_transition`;
- `test_cancellation_reschedules_a_successor_worker`.

## 3. Recent fixes verified

### Concurrent mkdir race: PASS locally

Both `_parent_fd` and `ensure_directory` tolerate a losing `FileExistsError`, then reopen with `O_NOFOLLOW | O_DIRECTORY`. A symlink placed in the race window is still rejected. Deterministic tests cover both valid concurrent creation and symlink substitution.

### Cancellation cleanup/reschedule race: PASS locally

`cancel()` now:

1. fences the durable reset by the current attempt;
2. cancels and awaits the local task;
3. removes the completed local task entry when still owned;
4. records the reopen event;
5. schedules the pending successor, which mints the next fencing token.

Tests prove the successor is actually processing, the attempt advances, the successor completes, and the cancelled worker's late completion is refused.

### Prior fencing/security findings: PASS locally

The prior stale-cancel, reconcile-scope, staging-boundary, publish, quarantine, verification, notification-ordering, and one-authority findings are covered by executable tests and mutation probes on the current code head.

## 4. I1–I10 executable evidence

| Invariant | Enforcement symbol | Proving test |
|---|---|---|
| I1 one request → one job | `InProcessJobQueue.enqueue` | `test_submit_returns_the_authoritative_identity` |
| I2 attempt ≠ provider retry | `NativeLocalBackend.observe` | `test_provider_retry_never_mints_a_new_nexus_attempt` |
| I3 only current token completes | `InProcessJobQueue._mark_completed` | `test_stale_attempt_cannot_complete_after_takeover` |
| I4 stale attempt never completes | `InProcessJobQueue._mark_completed` | `test_c7_late_stale_worker_is_refused_without_a_success_notice` |
| I5 provider run id never authority | `NativeLocalBackend.observe` | `test_provider_run_id_never_changes_observed_truth` |
| I6 verification independent | `InProcessJobQueue._verify_safely` | `test_handler_success_without_independent_verification_is_not_job_success` |
| I7 UNKNOWN ≠ FAILED | `FailureDisposition.is_terminal_business_failure` | `test_observe_unknown_job_is_unknown_not_failed` |
| I8 Nexus-owned idempotency | `InProcessJobQueue.enqueue` | `test_submit_is_idempotent_on_the_nexus_key` |
| I9 no second queue | `NativeLocalBackend.__init__` | `test_backend_never_constructs_a_queue` |
| I10 notify after commit | `InProcessJobQueue._notify_completion` | `test_success_notification_only_after_the_commit` |

The architecture test imports each enforcement symbol, resolves each proving test node, and asserts that the table covers exactly I1 through I10.

## 5. Race, crash, and security matrix

Current focused evidence covers stale completion, stale cancellation, current cancellation, completion-vs-cancel, completion-first, cancellation-first, reconcile safety, cross-job reconcile isolation, symlink staging, symlink publish, symlink quarantine, concurrent mkdir, and cancellation/reschedule.

The crash matrix covers C1–C8, including late stale workers, notification failure, artifact authority, and UNKNOWN-state preservation.

## 6. Mutation proof

The current source and harness define **M1–M7**, not M1–M6. The harness header and catalog have been checked for that consistency. Running the harness on the current code head produced:

```text
M1 caught; restored GREEN
M2 caught; restored GREEN
M3 caught; restored GREEN
M4 caught; restored GREEN
M5 caught; restored GREEN
M6 caught; restored GREEN
M7 caught; restored GREEN
summary: 7/7 mutations caught
```

The harness restores the source after each mutation and leaves the tree clean.

## 7. Test evidence at current code head

| Evidence | Result |
|---|---|
| Focused execution, staging, filesystem, race, crash, and architecture tests | `148 passed` |
| Mutation campaign | `7/7 caught`, baseline restored GREEN |
| Ruff | passed |
| Ruff format check | passed |
| Mypy | `Success: no issues found in 289 source files` |
| Full non-slow local suite | `pytest -q -m "not slow"` on the unchanged code head `edcdc733`: `3841 passed, 31 skipped, 0 failed` (306.68s) |

## 8. Exact-SHA CI

The exact PR tip `3cd735b64a95b6a30938f6f60148b7a3237a494b` has **terminal
green** CI on both triggering events:

- push run `37841306966` — **success**;
- pull_request run `37841316342` — **success**.

`gh pr checks 191` reports 0 pending / 0 failing; every job is `pass` (or the
by-design `skipping` of `merge-base-guard` on the `push` event).

`test (pytest -m "not slow")` job `113531088191` (push run) observed:

```text
3842 passed, 30 skipped, 194 warnings in 262.27s (0:04:22)
```

Lint (`ruff + mypy + version lockstep`), `lint-fast`, `python-parity`
(3.10/3.11/3.12), `extras-matrix` (core/pdf/speech/translate),
`continuum-evidence` (3.10/3.11/3.12), `migrate-postgres`, `release-lineage`,
and the mutation jobs (`trust`, `temporal`, `remote-key`) all passed for the
same SHA. Earlier SHAs' green runs are not used as evidence here.

## 9. CodeRabbit closure

The latest completed CodeRabbit review record is:

- review `5461982618`;
- reviewed commit `a50d87428bd3c6e2081ce94aa01fec1bc003df55`;
- state `CHANGES_REQUESTED`.

A valid inline finding on the prior report commit `edcdc733` remained on the report at line 27: the historical commit-count wording was ambiguous because the report listed seven pre-report commits. The finding is actionable and has not been dismissed. A later CodeRabbit status is `Review paused`, but there is no newly submitted review record for the current PR tip `118af102c0d7ff5fef76d27a99c5e5c839b1ed2b` proving zero actionable findings.

This report rewrite addresses the finding by explicitly listing the complete commit history and removing the ambiguous phrase. A fresh CodeRabbit review is still required for the current report head.

## 10. Governance

Task-254 remains `active` on the board:

- owner branch: `arena/execution-core-v1-nexus-ai-agent`;
- claim generation: `1`;
- lease: active through `2026-10-09T18:49:26Z`;
- delivered: not recorded as final closure;
- `gates_owner`: `null`.

`python3 scripts/agent_board.py check` is fail-closed in the current environment because it cannot enumerate open-PR branches without a GitHub token and because exactly one active, unexpired `gates_owner` is required but zero are present. No gates owner is being fabricated.

## 11. Remaining limitations and blockers

Evidence-backed limitations that remain:

1. **Governance (not a correctness gap):** the board has no live `gates_owner`,
   and `agent_board.py check` is fail-closed on that precondition. Per AGENTS.md
   §1.4, only the single `gates_owner` runs the full gates on main-bound work;
   this session's green CI/local gates are diagnostic, not the gate. Release
   authority remains governed — the PR is **not merged**.
2. **Fresh review record:** all 8 review threads (6 original + `filesystem_policy`
   + docs) are `RESOLVED`, and the latest CodeRabbit check is `pass`
   (`Review paused`); there is no newly *submitted* CodeRabbit review for the
   exact tip `3cd735b` proving zero findings at review time.
3. **Crash model:** the crash matrix simulates a process restart as a fresh
   `InProcessJobQueue` over the same SQLite sidecar (the bot/CLI model); it does
   not kill an OS process or exercise a hostile filesystem concurrently with a
   live writer.
4. **Clock/sidecar scope:** `recover_job`'s expiry-gated takeover assumes a
   single-host SQLite sidecar and monotonic-ish `started_at`; cross-host clock
   skew is out of scope for V1.

## 12. Final verdict

**`VERIFIED_WITH_LIMITATIONS`** — for the execution-core implementation.

Every V1 property (ONE CONTRACT, ONE QUEUE, ONE AUTHORITY, ONE FENCING MODEL,
ONE VERIFIED COMPLETION PATH) has a live-code enforcement symbol, an executable
proof, and a passing mutation probe; the full non-slow suite is green locally
(`3841 passed, 31 skipped`) and in exact-SHA CI (`3842 passed, 30 skipped`).

The limitations in §11 are governance/scope, not unproven correctness. The PR
is left **open, unmerged, unclosed**; no branch was deleted, no force-push was
used, no history was rewritten, and no unrelated feature or architecture was
changed.
