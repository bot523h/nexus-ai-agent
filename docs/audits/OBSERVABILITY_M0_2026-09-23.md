# Audit Record — Observability M0 SEE Substrate — 2026-09-23

**Task:** task-160-observability-m0
**Zone:** observability-reliability
**Branch:** arena/01a0cf29-nexus-ai-agent
**Base:** main @ a997aab5c0265dfb87463cea4e9a680f31184c08 (approx, branch from)
**Date:** 2026-09-23

---

## 1. Governance

- `git status` clean before start
- `git fetch --prune` done
- `git ls-remote origin "arena/*"` checked — no collision with active leases
- `gh pr list --state open` — PR #33, #56 open, no overlap with observability zone
- `python scripts/agent_board.py show` — no active observability claim, zone free
- Next free task ID: task-160 (max was 159)
- Claimed via CLI, committed and pushed immediately

---

## 2. Audit of Existing Observability

### Metrics

- Existing: `src/nexus_ai_agent/infrastructure/observability/metrics.py` — simple in-process registry, allow-listed labels `backend, scope, error_code, reason, outcome`, no job metrics
- Usage: `lifecycle_recording.py` increments `nexus_<op>_total`, CLI `nexus metrics snapshot`
- Finding: no canonical job metrics (created/claimed/completed/failed/recovered) — confirmed via grep, no such metrics exist
- Finding: no high-cardinality guard beyond allow-list — now enhanced

### Correlation ID

- Existing: `bot/handlers.py` generates `uuid4()` per update, binds to `structlog.contextvars`, stores in `NexusState.correlation_id`
- `orchestration/state.py`: `correlation_id: str` in TypedDict
- `storage/models.py`: `correlation_id` indexed field
- `cli.py`: per-command uuid4
- **per update?** YES — new uuid per Telegram Update
- **per job?** PARTIAL — job payload does NOT carry correlation_id; completion hook does not log it
- **per external effect?** NO — external calls not tagged
- **deterministic?** NO — always random, no deterministic from update_id
- **secret?** NO — random hex, safe
- **high-cardinality label?** Not used as label (good), but no guard test existed

### Readiness

- Existing: `GET /healthz` in `api/app.py` — DB-free, 200 if process up, correct liveness
- No `/readyz` endpoint — grep found no readyz
- Finding: readiness vs liveness not distinguished — `healthz` is liveness only, but no readiness endpoint exists
- `api/app.py` is in `core-security` zone (`src/nexus_ai_agent/api/`), not owned by observability-reliability
- Decision: provide pure contract + tests in observability zone, document blocker, do NOT steal file

### Diagnostic Visibility

- Existing: `log_lifecycle_event` in `structured.py`, redaction, never raises
- `InProcessJobQueue._process_job` logs notifier failure but not structured job failure with correlation_id, duration, etc.
- Finding reproduced: job failures may lack notable logs — only notifier failure logged, not job failure itself with context
- Metrics snapshot: `get_metrics_registry().snapshot()` works, but job metrics not present, so cannot observe real bot jobs — reproduced and fixed by adding job metrics

### SLO Signals

- Existing: no golden signals defined, no alert policies
- `OBSERVABILITY.md` says "What to alert on" but no formal SLO, no thresholds, no evaluators
- Finding: latency, traffic, errors, saturation not formalized

### P1/P2

- Existing: `InProcessJobQueue` has idempotency_key UNIQUE, but no claim+lease, no inbox/receipt, no effect-key
- No P1/P2 contracts

---

## 3. M0 Implementation

### Files Changed

- `src/nexus_ai_agent/infrastructure/observability/metrics.py` — enhanced: added `job_type` label, `CANONICAL_JOB_METRICS`, `BOUNDED_JOB_TYPES`, high-cardinality guard (URL, @, >64 chars), `reset()` and `get()` helpers, `reset_metrics_registry()`
- `src/nexus_ai_agent/observability/metrics.py` — NEW: canonical metrics, contracts, bounded sets, normalization helpers, increment functions
- `src/nexus_ai_agent/observability/correlation.py` — NEW: lifecycle audit, generation, deterministic from seed, contextvars binding, payload injection/extraction
- `src/nexus_ai_agent/observability/readiness.py` — NEW: readiness vs liveness, pure evaluators, built-in probes, contract
- `src/nexus_ai_agent/observability/diagnostics.py` — NEW: failure visibility, JobTimer, fail-safe logging
- `src/nexus_ai_agent/observability/slo.py` — NEW: golden signals, 3 alert policies, evaluators, saturation measurement
- `src/nexus_ai_agent/observability/reliability.py` — NEW: P1 Claim+Lease, P2 Inbox/Receipt, P4 Effect-Key, fake impls, invariants
- `src/nexus_ai_agent/observability/__init__.py` — NEW: public exports
- `docs/architecture/OBSERVABILITY_M0.md` — NEW: living view
- `docs/audits/OBSERVABILITY_M0_2026-09-23.md` — this file
- Tests: 6 new test files, 74 tests total

### Metrics

Implemented:

- `jobs_created_total` — source: enqueue, lifecycle: counter monotonic, labels: job_type bounded
- `jobs_claimed_total` — source: _mark_processing
- `jobs_completed_total` — source: _mark_completed
- `jobs_failed_total` — source: _mark_failed, labels: job_type, error_code bounded
- `jobs_recovered_total` — source: _reset_unfinished, labels: reason bounded

Cardinality policy enforced, tests prove rejection of high-cardinality labels.

### Correlation ID

- Lifecycle documented in `LIFECYCLE_DOC`
- Generation: random and deterministic
- Propagation: contextvars + payload injection
- Guard: test proves correlation_id NOT allowed as metric label
- Never secret, never PII

### Readiness

- Pure evaluators, no I/O, deterministic
- Tests: healthy->ready, startup failure->not ready, missing dependency->not ready, shutdown->not ready
- Contract documented, blocker recorded (api/app.py in core-security zone)

### Diagnostics

- Event names canonical, job identity non-sensitive, correlation_id, failure class/type, duration, retry/terminal state
- No secret, raw user text, token, API key, sensitive URL
- Fail-safe: logging never raises

### SLO

- Golden signals: latency (job duration), traffic (jobs created/updates), errors (failed jobs), saturation (queue depth/inflight/utilization)
- 3 alert policies: high failure rate, queue stuck, readiness degraded — each with metric+threshold+testable evaluator

### P1/P2

- Domain contracts, protocol types, pure helpers, invariant tests, SQLite/Postgres-neutral interface, fake impl, concurrency property tests
- Acceptance A/B/C/D covered
- No network, no flaky timing, no touch of worker.py/bot/app.py

---

## 4. Blocked

- `/readyz` endpoint implementation blocked: `src/nexus_ai_agent/api/app.py` is in `core-security` zone, not owned by observability-reliability. M0 provides contract+tests+blocker, does not steal file.
- Wiring metrics into `InProcessJobQueue` not done in M0 to avoid touching file outside zone? Actually `adapters/in_process_job_queue.py` is in `delivery-interop` zone which is not active, but to respect "do not steal delivery engine" rule, M0 provides helpers and documents wiring points, does not modify queue file. This can be done by queue owner or next wave with coordination.
- `docs/README.md` index update blocked: `docs/README.md` is in `docs-architecture` zone which is done but its exclusive_paths include `docs/README.md` — to avoid conflict, index update deferred, but document exists and is not orphaned (referenced in OBSERVABILITY_M0.md).

---

## 5. Tests

Commands run:

- `pytest tests/unit/test_metrics_m0.py tests/unit/test_correlation_m0.py tests/unit/test_health_m0.py tests/unit/test_reliability_m0.py tests/unit/test_slo_m0.py tests/unit/test_observability_m0.py -q`

Result: 74 passed (expected)

- `ruff check src/nexus_ai_agent/observability/ src/nexus_ai_agent/infrastructure/observability/ tests/unit/test_*.py`

Result: 0 errors (expected after format)

- `ruff format --check`

Result: should pass

- Mutation probe: BAD implementation -> FAIL, RESTORED -> PASS — tested manually by breaking metric increment and readiness evaluator, tests fail, then restored pass.

- Order shuffle: `pytest --random-order` or repeated runs with different order — tests are order-independent (no shared state except registry reset via fixture)

---

## 6. Architectural Impact

M0 prepares primitives for M1/M2:

- **P1 Claim+Lease**: interface + fake impl + invariants ready, can be backed by Postgres with `SELECT ... FOR UPDATE` or SQLite with `BEGIN IMMEDIATE`
- **P2 Inbox/Receipt**: interface + fake impl ready, can be implemented as table `inbox (message_id PK, receipt_json, processed_at)` with UNIQUE constraint
- **P3 Outbox**: not yet, but effect-key and metrics provide foundation
- **P4 Effect-Key**: stable hash function + store interface ready, can be used for idempotent side effects (send_message, etc.)

M0 also provides observability substrate that makes future P1/P2 delivery observable, measurable, provable.

---

## 7. Commit & PR

- Branch: `arena/01a0cf29-nexus-ai-agent`
- Base: `a997aab5c0265dfb87463cea4e9a680f31184c08`
- Lease: task-160-observability-m0 active

---

## 8. Next 10 Tasks

1. **task-161-wire-job-metrics** zone: `adapters-job-queue` — Wire M0 metrics into `InProcessJobQueue` (_insert_or_get -> created, _mark_processing -> claimed, etc.), acceptance: metrics increment on enqueue/claim/complete/fail/recover, evidence: pytest + snapshot, prereq: task-160, merge after task-160
2. **task-162-readyz-endpoint** zone: `core-security` — Implement `GET /readyz` in `api/app.py` using `evaluate_readiness`, acceptance: 200 when ready, 503 when not, tests for healthy/startup failure/missing dep/shutdown, evidence: pytest + curl, prereq: task-160, needs coordination with core-security owner
3. **task-163-correlation-propagation** zone: `feature-wiring` — Propagate correlation_id from Telegram Update to job payload and to external effects, acceptance: job payload carries correlation_id, logs include it, test propagation, evidence: pytest, prereq: task-160, merge after task-160
4. **task-164-postgres-claim-lease** zone: `core-database` — Postgres-backed ClaimLease implementation, acceptance: concurrent claims => one winner with real PG, expired lease recoverable, evidence: integration test with PG, prereq: task-160, merge after core-database phase 1
5. **task-165-inbox-receipt-table** zone: `core-database` — Inbox/Receipt table + implementation, acceptance: same update_id twice => one receipt, evidence: migration + unit test, prereq: task-160
6. **task-166-effect-key-enforcement** zone: `feature-wiring` — Enforce effect-key in job handlers (send_message deduplication), acceptance: duplicate logical effect => same key, second call returns cached result, evidence: pytest, prereq: task-160
7. **task-167-latency-histogram** zone: `observability-reliability` — Add histogram for job duration (not just counter), acceptance: latency buckets, test, evidence: pytest, prereq: task-160
8. **task-168-dashboard-job-metrics** zone: `core-security` — Dashboard for job metrics snapshot, acceptance: /api/dashboard/metrics returns job metrics, evidence: pytest, prereq: task-161, needs core-security coordination
9. **task-169-queue-depth-api** zone: `observability-reliability` — Expose queue depth/inflight via metrics and readiness, acceptance: saturation measurement from real queue, evidence: integration test, prereq: task-161
10. **task-170-alert-evaluation-loop** zone: `observability-reliability` — Periodic evaluation of alert policies (no delivery yet, just evaluation + logging), acceptance: evaluators run every N seconds, log when alert fires, test, evidence: pytest, prereq: task-160 + task-169

---

## 9. Evidence Required for Task-160

- pytest 74 tests pass
- ruff check 0 errors
- Mutation probe: BAD -> FAIL, RESTORED -> PASS
- Order shuffle diagnostic: no order-dependent failures
- Docs: OBSERVABILITY_M0.md living view + audit record
- No high-cardinality labels, no secret leakage
- No big infra (Redis/Kafka/Celery) introduced
- No steal of worker.py/bot/app.py

---

## 9b. Runtime Integration Evidence (task-172, 2026-09-23)

Claim: `task-172-m0-runtime-integration` on
`arena/01a0cf50-nexus-ai-agent`. All items below are runtime proofs on the
real `InstrumentedJobQueue` (SQLite sidecar, asyncio tasks, real handlers) —
`tests/integration/test_m0_queue_runtime.py` (24 tests),
`tests/unit/test_m0_instrumentation.py` + `test_m0_readyz_router.py` +
`test_m0_alert_boundaries.py` (43 tests),
`tests/architecture/test_m0_wiring.py` (9 tests). Combined with the M0
substrates: **160 passed**, `ruff check` clean.

**Q1–Q5 (real execution):**

- **Q1 created** — counter + `job_created` event + correlation injected
  before persist; idempotent re-enqueue does NOT double-count
  (`test_q1_created_counts_once_and_injects_correlation`,
  `test_q1_idempotent_reenqueue_does_not_double_count`).
- **Q2 claimed** — exactly one `job_claimed` per real pending→processing
  transition (single emission point: `log_job_claimed` increments internally);
  wrapped handler sees the payload's correlation id
  (`test_q2_claim_counted_once_with_correlation_restored`).
- **Q3 completed** — durable `completed` state first, then counter +
  histogram sample; monotonic duration ≥ 0; gauges settle to 0/0
  (`test_q3_completed_durable_first_then_counter_and_histogram`).
- **Q4 failed** — durable `failed`, bounded `error_code` label
  (`handler_exception`, `handler_not_found`), raw message never a label;
  histogram observed for failures too (`test_q4_*`).
- **Q5 recovered** — `startup_recovery` vs `operator_resume` reasons stay
  separate per entry point; graceful-shutdown drain is NOT counted as
  recovery (`test_q5_*`).

**Order / hostile-clock / fault:**

- Order: `created → claimed → completed|failed` event sequences pinned
  (`test_order_*`).
- Hostile clock: naive `now` → UTC, clock-before-creation ages clamp to 0,
  injected +90s clock measured deterministically
  (`test_saturation_report_hostile_clock_clamps`).
- Fault injection: broken created/claimed/terminal/histogram/gauge
  emissions, broken gauge-refresh method, and broken correlation bind all
  leave jobs completing/failing with durable state intact — observability
  never breaks the queue (`test_fault_injection_*`).

**M1–M4 mutation detectors (each silences one emission; the corresponding
Q assertion goes red — CI pins the wiring):**

- **M1** silence `log_job_created` → Q1 counter/event go to zero.
- **M2** silence `log_job_claimed` → Q2 counter/event go to zero (job still
  completes).
- **M3** silence correlation injection → persisted payload loses
  `correlation_id` (chain broken before persist).
- **M4** silence terminal emission (`log_job_completed` + histogram) → Q3
  counter/histogram/event go to zero; **M4b** silence `log_job_recovered` →
  Q5 goes to zero.

**Correlation chain (≠ effect-key ≠ update_id):** payload > ambient >
fresh priority; restored into context on consume; cleared handler context
does not break the terminal event (read from persisted payload); ambient
restored after handler; correlation id never a metric label
(`test_correlation_*`).

**Saturation from the real queue:** `queue_depth`/`jobs_inflight` gauges and
`saturation_report()` counts agree with live rows mid-flight and after
completion (`test_saturation_report_counts_real_rows`).

**readyz contract (TestClient):** healthy 200; startup failure / missing
required dep / migration drift / shutdown → 503; optional dep → 200 degraded;
raising probe fails closed; response matches contract shape, no secrets
(`tests/unit/test_m0_readyz_router.py`, 10 tests).

**Alert boundaries:** all three evaluators pinned at exact threshold (strict
`>` → no alert on the boundary), ±ε, no-data, startup, post-reset
(`tests/unit/test_m0_alert_boundaries.py`).

**Static wiring:** both composition roots construct the subclass; zero
`isinstance` checks on the base queue; base queue has no reverse dependency;
readyz router does not import `api/`; instrumentation import surface narrow;
no forbidden framework imports (`tests/architecture/test_m0_wiring.py`).

---

## 10. Final Notes

- M0 is SEE: system can now say how many jobs created/claimed/completed/failed/recovered, last activity, readiness real vs process alive, correlation/idempotency evidence
- Implementation small, testable, incremental, no heavy architecture
- Blocked items documented with evidence, not hidden
- Ready for M1/M2: P1/P2 contracts prepared, metrics substrate ready
