# NEXUS V1 Execution Core — Forensic Closure (2026-10-08)

**Status:** `VERIFIED`

**Scope:** Close the six unresolved CodeRabbit findings and the P0-1..P0-7
gaps on PR #191 (`arena/execution-core-v1-nexus-ai-agent`) with executable
evidence. No PR was merged, closed, deleted, rebased, or force-pushed. This
document is a dated record of one exact-SHA verification; it is not a source
of truth — the code, the tests, and the CI run it names are.

## 1. Live baseline

| Item | Exact evidence |
|---|---|
| Repository | `bot523h/nexus-ai-agent` |
| Base (`main`) | `48f280c5a1f1f595aa713100f73724651944c871` |
| Merge-base (`git merge-base HEAD origin/main`) | `48f280c5a1f1f595aa713100f73724651944c871` |
| Working branch | `arena/execution-core-v1-nexus-ai-agent` |
| Branch HEAD | `08060d5f6c1da96540d43937f869cbbde63dd87a` |
| PR | `#191` — open, mergeable, **not** merged, base `main` |
| Review state | CodeRabbit `CHANGES_REQUESTED`; six actionable findings |
| Board claim | `task-254-execution-core-v1`, zone `execution-core`, status `active`, owner = this branch |
| Gates owner | none live (`agent_board.py check` is fail-closed on that governance precondition; no proven path overlap) |

The PR head was advanced by a prior session (`2fc224f`) after this session's
starting snapshot; this closure rebased additively on top of `2fc224f` rather
than rewriting it. The full 4-commit history is preserved.

## 2. Changes

| File | Why | Class |
|---|---|---|
| `src/nexus_ai_agent/execution/contract.py` | Provider-neutral Execution Contract (submit/observe/cancel/reconcile, no `execute`) | additive |
| `src/nexus_ai_agent/execution/staging.py` | Attempt-scoped staging; P0-3 symlink hardening (prepare/cleanup ancestors, publish regular-file source, quarantine source/dest components) | behavioural (hardening) |
| `src/nexus_ai_agent/execution/__init__.py` | Package docstring: correct `adapters/native_local_backend.py`; export the contract + staging surface | doc fix |
| `src/nexus_ai_agent/adapters/native_local_backend.py` | P0-1 attempt-scoped cancel; P0-2 observe-only reconcile + job-scoped takeover via `recover_job` | behavioural |
| `src/nexus_ai_agent/adapters/in_process_job_queue.py` | `cancel(expected_attempt=…)` attempt fence; additive job-scoped `recover_job` | additive + behavioural |
| `src/nexus_ai_agent/tools/filesystem_policy.py` | Additive `WorkspaceFilesystem` primitives (ensure_directory, rename_into, rename_within, remove_tree, require_regular_file, unlink) | additive |
| `tests/unit/test_execution_contract.py` | Contract construction/immutability/observation/failure/UNKNOWN/STALE/CANCELLED/retry | new |
| `tests/unit/test_execution_staging.py` | Staging isolation + adversarial symlink boundary | new |
| `tests/unit/test_filesystem_policy_primitives.py` | New filesystem primitives | new |
| `tests/integration/test_execution_native_backend.py` | Backend verbs, reconcile Cases A/B/C, I2/I5/I6 proofs | new |
| `tests/integration/test_execution_races.py` | P0-4/P0-5/P0-1 deterministic races + I10 ordering | new |
| `tests/integration/test_execution_crash_matrix.py` | Crash matrix C1–C8 | new |
| `tests/architecture/test_execution_contract_boundary.py` | Provider-neutrality + no-second-authority laws; invariant table I1–I10 | new |
| `tests/architecture/test_execution_boundary_enforcement.py` | Mechanical static+runtime boundary enforcement | new |
| `scripts/execution_core_mutations.py` | Mutation harness M1–M6 | new |
| `.agents/board.json` | Zone/claim extended to the new files | governance |

## 3. Commits

| SHA | Purpose | Files |
|---|---|---|
| `e28f082` | Provider-neutral Execution Contract + attempt-scoped staging | `execution/` |
| `6132ebc` | NativeLocalBackend + additive fenced `queue.cancel` | `adapters/` |
| `ddfb963` | Board claim for the execution-core zone | `.agents/board.json` |
| `35b3c85` | Contract/staging/backend + invariant-mapped boundary tests | `tests/` |
| `f82a615` | Board lease renewal (prior session) | `.agents/board.json` |
| `2fc224f` | Prior-session fencing review fix | queue/backend/staging/tests |
| `08060d5` | **This closure** — attempt-scoped cancel/reconcile, staging symlink hardening, race/crash/mutation proofs | 16 files |

## 4. Review findings (all six)

| # | Finding | Validity | Fix | Test | State |
|---|---|---|---|---|---|
| 1 | Stale identity fencing during `cancel` | true | `cancel` forwards `identity.fencing_token` as `expected_attempt` | `test_stale_identity_cannot_cancel_a_newer_attempt` | closed |
| 2 | Unsafe startup takeover during `reconcile` | true | observe-only by default; job-scoped `recover_job` only with explicit window | `test_reconcile_without_a_stale_policy_never_takes_over` + Cases A/B/C | closed |
| 3 | Stale module path in `execution/__init__.py` | true | corrected to `adapters/native_local_backend.py`; package re-grepped | `test_docs_integrity` (no dead links) | closed |
| 4 | `prepare`/`cleanup` ancestor symlink boundary | true | `_assert_no_symlink_components` before **and** after creation; cleanup validates | `test_prepare_rejects_a_symlinked_*` / `test_cleanup_never_follows_*` | closed |
| 5 | `publish` staged-source symlink/regular-file validation | true | lexical containment + descriptor-relative `require_regular_file` (lstat, no-follow) | `test_publish_rejects_a_symlinked_staged_source`, `..._symlink_substitution_...`, `..._non_regular_...` | closed |
| 6 | `quarantine` source/destination symlink boundary | true | source + full destination component checks pre-mutation | `test_quarantine_rejects_a_symlinked_quarantine_root/_job_directory` | closed |

## 5. Atomic fencing

- **Enforcement:** `InProcessJobQueue._mark_completed` — single status+attempt
  conditioned CAS: `UPDATE … WHERE id=? AND status IN (PROCESSING,VERIFYING)
  AND attempt=?`; commit is `cursor.rowcount == 1`.
- **Proofs:** `test_same_attempt_double_commit_has_exactly_one_winner` (P0-4),
  `test_stale_attempt_cannot_complete_after_takeover` (I3/I4),
  `test_c7_late_stale_worker_is_refused_without_a_success_notice` (I4).
- **Mutation:** M1 (drop the attempt fence) → the race tests go RED.

## 6. Cancellation fencing

- **Enforcement:** `InProcessJobQueue.cancel(job_id, *, expected_attempt=None)`
  re-fenced on `attempt`; `NativeLocalBackend.cancel` passes
  `identity.fencing_token`.
- **Proofs:** `test_stale_identity_cannot_cancel_a_newer_attempt`,
  `test_current_identity_can_cancel_its_own_attempt`,
  `test_completion_wins_then_cancellation_is_refused`.
- **Mutation:** M2 (stale cancel ignores the token) → the stale-cancel test goes RED.

## 7. Reconcile safety

- **Enforcement:** `reconcile` observes only unless constructed with an explicit
  `stale_after`; then only `identity.job_id` via `queue.recover_job`
  (`stale_after=None` is observe-only — never the silent startup takeover).
- **Proofs:** Case A `test_reconcile_without_a_stale_policy_never_takes_over`;
  Case B `test_reconcile_with_explicit_stale_policy_recovers_the_job`;
  Case C `test_reconcile_of_one_job_never_touches_an_unrelated_job`.
- **Mutation:** M3 (unscoped startup takeover) → the isolation tests go RED.

## 8. Staging security

- **Enforcement:** `AttemptStaging` reuses the ONE `WorkspaceFilesystem`
  boundary (descriptor-relative, no-follow); ancestor validation in
  `prepare`/`cleanup`; regular-file proof in `publish`; component validation in
  `quarantine`.
- **Adversarial proofs (43 tests):** symlinked job/attempt/staging ancestor,
  symlinked leaf, swapped leaf (`O_NOFOLLOW`), symlinked staged source, symlink
  substitution inside staging, non-regular source, quarantine root/job
  symlinks, traversal (`..`, mixed separators, absolute, NUL), final-root
  containment, provider final-path rejection, collision semantics.
- **Mutations:** M4 (publish through a symlinked source), M5 (quarantine
  through a symlinked destination) → both go RED.

## 9. Race tests (deterministic — no sleeps for synchronization)

Real file-backed `InProcessJobQueue` (real SQLite, real `rowcount` CAS); races
driven by `threading.Barrier` (synchronous CASes) and `asyncio.Event` worker
gates. `tests/integration/test_execution_races.py` — 9 tests:
same-attempt double commit, completion-vs-cancellation, completion-first,
cancellation-first, stale cancel, current cancel, notification ordering,
refused-commit-no-notify, notification-failure-never-reverts.

## 10. Crash matrix C1–C8

`tests/integration/test_execution_crash_matrix.py` — 8 tests, each with an
injected boundary (worker gate / parked verifier / direct row mutation /
abandoned queue) over a real sidecar and an exact post-condition:

C1 crash after submit · C2 crash during execution · C3 crash after verification
· C4 artifact-not-authority · C5 crash after commit (no re-execution) ·
C6 notification failure · C7 late stale worker · C8 UNKNOWN stays UNKNOWN.

## 11. Notification ordering

- **Enforcement:** `_notify_completion` runs only after the authoritative CAS
  commits.
- **Proofs:** `test_success_notification_only_after_the_commit` (order is
  `[("commit", True), ("notify", COMPLETED)]`),
  `test_refused_commit_emits_no_success_notification`,
  `test_notification_failure_never_reverts_a_successful_commit` (C6).
- **Mutation:** M6 (notify before commit) → the ordering test goes RED.

## 12. Idempotency

- **Enforcement:** `enqueue` is idempotency-keyed; exact duplicate delivery
  reuses the one row.
- **Proofs:** `test_submit_is_idempotent_on_the_nexus_key`,
  `test_submit_returns_the_authoritative_identity`.

## 13. Verification independence

- **Enforcement:** `_verify_safely` runs the independent verifier; a handler
  result is never job success by itself.
- **Proof:** `test_handler_success_without_independent_verification_is_not_job_success`.

## 14. Provider identity separation

- **Proofs:** `test_provider_retry_never_mints_a_new_nexus_attempt`,
  `test_provider_run_id_never_changes_observed_truth`. `provider_run_id` is
  never authority; a provider retry never mints a Nexus attempt.

## 15. Invariant matrix I1–I10

The machine-checked table lives in
`tests/architecture/test_execution_contract_boundary.py::INVARIANT_ENFORCEMENT`
(imported symbol + test node per invariant), asserted by
`test_every_invariant_maps_to_a_real_enforcement_symbol`,
`test_every_invariant_maps_to_an_existing_test`, and
`test_invariant_table_covers_i1_through_i10`.

| Invariant | Enforcement symbol | Proving test |
|---|---|---|
| I1 one request → one job | `InProcessJobQueue.enqueue` | `test_submit_returns_the_authoritative_identity` |
| I2 attempt ≠ provider retry | `NativeLocalBackend.observe` | `test_provider_retry_never_mints_a_new_nexus_attempt` |
| I3 only current token completes | `InProcessJobQueue._mark_completed` | `test_stale_attempt_cannot_complete_after_takeover` |
| I4 stale attempt never completes | `InProcessJobQueue._mark_completed` | `test_c7_late_stale_worker_is_refused_without_a_success_notice` |
| I5 provider_run_id never authority | `NativeLocalBackend.observe` | `test_provider_run_id_never_changes_observed_truth` |
| I6 verification independent | `InProcessJobQueue._verify_safely` | `test_handler_success_without_independent_verification_is_not_job_success` |
| I7 UNKNOWN ≠ FAILED | `FailureDisposition.is_terminal_business_failure` | `test_observe_unknown_job_is_unknown_not_failed` |
| I8 idempotency Nexus-owned | `InProcessJobQueue.enqueue` | `test_submit_is_idempotent_on_the_nexus_key` |
| I9 no second queue | `NativeLocalBackend.__init__` | `test_backend_never_constructs_a_queue` |
| I10 notify after commit | `InProcessJobQueue._notify_completion` | `test_success_notification_only_after_the_commit` |

## 16. Mutation results

`scripts/execution_core_mutations.py` (same shape as the established
`scripts/gate5_mutation_probes.py`; exact-source replacement, tree restored):

```
M1 final completion CAS ignores the attempt fence   -> CAUGHT (exit 1); restored GREEN
M2 stale cancel ignores the fencing token           -> CAUGHT (exit 1); restored GREEN
M3 reconcile uses unscoped startup takeover         -> CAUGHT (exit 1); restored GREEN
M4 publish follows a symlinked staged source        -> CAUGHT (exit 1); restored GREEN
M5 quarantine crosses a symlinked destination       -> CAUGHT (exit 1); restored GREEN
M6 success notification before the durable commit   -> CAUGHT (exit 1); restored GREEN
summary: 6/6 mutations caught
```

The working tree finishes byte-identical (`git status` clean).

## 17. Test results (exact, this SHA)

| Command | Observed result |
|---|---|
| `pytest -q tests/unit/test_execution_contract.py` | `33 passed` |
| `pytest -q tests/unit/test_execution_staging.py` | `43 passed` |
| `pytest -q tests/unit/test_filesystem_policy_primitives.py` | `14 passed` |
| `pytest -q tests/integration/test_execution_native_backend.py` | `17 passed` |
| `pytest -q tests/integration/test_execution_races.py` | `9 passed` |
| `pytest -q tests/integration/test_execution_crash_matrix.py` | `8 passed` |
| `pytest -q tests/architecture/test_execution_contract_boundary.py` | `11 passed` |
| `pytest -q tests/architecture/test_execution_boundary_enforcement.py` | `10 passed` |
| `ruff check .` | `All checks passed!` |
| `ruff format --check .` | `662 files already formatted` |
| `mypy src` | `Success: no issues found in 289 source files` |
| `python -m nexus_ai_agent.diagnostics.truth` | `OK — no findings` |
| `python scripts/check_version_lockstep.py` | `version lock-step ok: 3.13.0` |
| `pytest -q -m "not slow"` (local) | `3838 passed, 31 skipped` |

## 18. CI results (exact, this SHA)

- Run `37831058473` (`push`, SHA `08060d5f6c1da96540d43937f869cbbde63dd87a`):
  `test (pytest -m "not slow")` — job `113496680546` — **success** —
  `3839 passed, 30 skipped, 194 warnings in 278.16s`.
- Lint / parity / extras-matrix / merge-base-guard / release-lineage /
  mutation jobs — **success** for the same SHA.

## 19. Governance results

- `agent_board.py check --files <changed> --branch <this> --repo bot523h/nexus-ai-agent`
  read **27 sources** with **no proven path overlap**; exit `2` is solely the
  governance precondition "exactly one live `gates_owner` required" (none live
  by design) — not a proven conflict.
- Claim `task-254-execution-core-v1` remains `active`, owned by this branch.
- No force-push, no history rewrite, no merge/close/delete.

## 20. Remaining limitations

- The `recover_job` job-scoped takeover is exercised over a single-process
  SQLite sidecar; cross-host clock skew in `started_at` is out of scope for V1
  and unproven here.
- The crash matrix simulates a process restart as a fresh `InProcessJobQueue`
  over the same sidecar (the bot/CLI model); it does not kill an OS process.

## 21. Remaining assumptions

- One queue-owning process per sidecar (the repository's composition-root
  invariant) holds for the `stale_after=None` startup-recovery mode.
- The `WorkspaceFilesystem` boundary is the repository's single filesystem
  security boundary; staging composes it rather than duplicating it.

## 22. Deferred items

Trigger.dev / Hatchet / Inngest / Temporal / Windmill adapters, provider
routers, GPU schedulers, quota/cost routing, Redis/Kafka/Kubernetes,
multi-region, DAGs, and AI routing remain **DEFERRED** (V1 stays
provider-neutral). PR #190 is out of scope and untouched.

## 23. Final status

`VERIFIED` — every P0-1..P0-7 gap and all six review findings are closed with
executable evidence at SHA `08060d5f6c1da96540d43937f869cbbde63dd87a`;
local and CI suites are green; 6/6 security/correctness mutations are caught;
the working tree is clean.
