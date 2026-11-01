# TASK-256 FINAL STATUS — Heartbeat staleness proof

timestamp_utc: 2026-10-29T23:10:00Z
branch: arena/1f7a74db-nexus-ai-agent
final_head: f488672 (docs governance repair)
implementation_commit: 34c8754
flake_fix_commit: f227f0e
m8_probe_fix_commit: 889e0f6
docs_commit: f488672
origin_main: b3e20f9
CI_run: 40968581609 (completed success, 1h0m15s)

## Mission
NEXUS-TASK-256-HEARTBEAT-LIVENESS: replace started_at-age-based recovery
liveness with a queue-owned heartbeat so recover_job distinguishes
slow-alive from dead, and UNKNOWN fails closed including startup/upgrade.

## Final status: HARDENED_BUT_NOT_COMPLETE

## What is DONE

### 1. Implementation (commit 34c8754)
- Additive nullable `heartbeat_at TEXT` via the existing queue-owned
  `additions` map. No Alembic migration; idempotent re-init.
- First heartbeat written atomically with the reservation CAS that mints
  the attempt (never derived from started_at / migration / wall-clock).
- Bounded child heartbeat refreshes the same attempt every 5 s in
  PROCESSING and VERIFYING under an attempt-fenced CAS
  (`id + attempt + allowed status + expected heartbeat`).
- Recovery CAS binds the observed heartbeat (`status + attempt +
  heartbeat_at`) so a concurrent refresh defeats takeover.
- Strict staleness: valid heartbeat strictly `< cutoff` eligible;
  `>= cutoff` (fresh/equal) and UNKNOWN refused.
- `_parse_heartbeat()` rejects naive / non-UTC / non-canonical / future /
  corrupt heartbeats → UNKNOWN.
- Startup recovery (`stale_after=None`) recovers valid-HB rows regardless
  of age; explicit `stale_after` recovers valid-HB strictly older than
  cutoff. UNKNOWN legacy in-flight rows refused at startup too.
- No fake heartbeat injected anywhere.

### 2. Proof (test_job_queue_heartbeat.py, 42 tests)
- Strict boundary (fresh/equal/stale), both in-flight states,
  unknown/invalid/future/non-UTC heartbeats, legacy schema upgrade with
  identity/history/terminal preservation, cadence refresh,
  recovery-vs-refresh CAS race, stale-worker heartbeat/finish/failure/
  cancel fencing, invalid attempts, manual cancel/reconcile/resume with
  repeated cancel, terminal preservation, stale-worker publication
  refusal, heartbeat write failure, cleanup, causal ordering.
- 15x isolation run: 0 failures.
- Flake fix (commit f227f0e): `_wait_event` helper polls the causal
  observer for the EventKind before asserting, eliminating the
  async-observe race in test_takeover_and_reopen_causal_order.

### 3. Scope amendment (issue #280, board rev 4)
- 7 frozen fixture/mutation files adapted under this claim:
  - test_execution_native_backend.py
  - test_in_process_job_queue.py
  - test_gate5_execution_fencing.py
  - test_execution_crash_matrix.py
  - test_execution_process_crash.py
  - scripts/gate5_mutation_probes.py
  - scripts/execution_core_mutations.py
- Inline backdate SQL now sets heartbeat_at (controlled-clock stale
  heartbeat) instead of only started_at; legacy-startup coverage split so
  UNKNOWN-refusal is asserted separately from valid-stale recovery.
- No fake fresh heartbeats injected; no safety assertion weakened.

### 4. A5 mutation-anchor repairs
- gate5 M1/M2/M9 anchors re-pointed to heartbeat-based source → caught.
- execution_core M8 anchor fixed to the heartbeat gate. M8 is now a
  TWO-EDIT mutation:
  - edit 1: remove the UNKNOWN guard (heartbeat is None → not refused)
  - edit 2: change the recovery CAS from `heartbeat_at = ?` to
    `(heartbeat_at IS NULL OR heartbeat_at = ?)`
  Rationale: the original single-edit mutant was inert because SQLite
  `heartbeat_at = NULL` never matches a NULL column, so NULL-heartbeat
  rows were still not taken over (CAS rowcount 0). The two-edit mutant
  makes the guard removal actually cause the fail-open takeover.

### 5. Phase B: publish-path side-effect fencing audit
- `_publish_and_reprove` re-checks `_owns_execution(claim, VERIFYING)`
  immediately before `publication.publish`; stale execution publishes
  nothing (reason `stale_execution`). Proven by
  `test_t8b_publication_is_fenced_immediately_before_the_rename`.
- Publication is attempt-scoped (staging dir `root/job_id/attempt_id/
  staging`); passport validates
  `attempt_identifier == attempt_id(job_id, attempt_number)`.
- `_mark_completed` commit attempt-fenced; refused commit → no
  notification, no filesystem action.
- Residual check→rename window = documented classic fencing limit
  (JOB_LIFECYCLE.md §3), bounded to microseconds, only enterable via
  resume_pending takeover. Closing requires token-aware storage
  (D-0020 out of scope).
- Handler-level external side effects NOT fenced (architectural: handler
  runs before ownership re-check). Closing requires a separate registered
  task.

### 6. Governance repair
- PR #283 body updated to reflect A4/A5 repairs, green suite, 42/42
  mutation harness, M8 two-edit fix, Phase B audit.
- JOB_LIFECYCLE.md §3 updated: takeover now keys on heartbeat staleness
  (not started_at age); heartbeat contract documented (reservation-minted
  first HB, 5s attempt-fenced refresh, strict <cutoff, UNKNOWN fails
  closed including startup).
- Board claim task-256 active, generation 1, scope amendment recorded.

## Test / gate status (final head f488672)
- Focused heartbeat suite: 42 passed (15x isolation, 0 failures).
- 6-file A4+A5 suite: 88 passed.
- Full suite: 5364 passed / 39 skipped / exit 0 (THREE consecutive
  fully-green runs after flake fix: run2, run3, run4).
- make lint: green (835 files).
- make types: green (332 source files).
- Mutation harness: gate5 17/17 caught + execution_core 25/25 caught =
  42/42 (detached worktree, byte-identical restore each probe).
- CI (GitHub Actions run 40968581609, head f488672): **ALL GREEN**.
  22/22 jobs passed including:
  - python-parity (3.10, 3.11, 3.12) — all success
  - execution-core-mutations (25 probes) — success
  - governance-mutations (Gate-5 fencing 17) — success
  - lint (ruff + mypy + version lockstep) — success
  - test (pytest -m "not slow") — success
  - continuum-evidence (3.10/3.11/3.12) — all success

## Remaining / not done in this session
- Merge: PR #283 open, awaiting review. Lease active, not released as
  done.
- Handler-level external side effects: not fenced (architectural);
  requires a separate registered task.

## Files changed (cumulative)
- src/nexus_ai_agent/adapters/in_process_job_queue.py (implementation)
- tests/integration/test_job_queue_heartbeat.py (new, 42 tests)
- tests/integration/test_execution_native_backend.py (A4)
- tests/integration/test_in_process_job_queue.py (A4)
- tests/integration/test_gate5_execution_fencing.py (A4)
- tests/integration/test_execution_crash_matrix.py (A4)
- tests/integration/test_execution_process_crash.py (A4)
- scripts/gate5_mutation_probes.py (A5 M1/M2/M9)
- scripts/execution_core_mutations.py (A5 M8 two-edit)
- docs/architecture/JOB_LIFECYCLE.md (governance repair)
- .agents/board.json (claim coordination only)
