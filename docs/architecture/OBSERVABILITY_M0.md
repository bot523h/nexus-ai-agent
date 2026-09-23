# Observability M0 — SEE Substrate

**Status:** Living document — M0 implementation
**Scope:** canonical job metrics, correlation-id lifecycle, readiness vs liveness, diagnostic visibility, SLO signals, P1/P2 preparation
**Verified against:** `arena/01a0cf29-nexus-ai-agent` @ M0
**Owner zone:** `observability-reliability`

This document is the M0 living view for observability. It extends `OBSERVABILITY.md` (which documents the O1 lifecycle events and low-cardinality registry) with the M0 SEE substrate required by research/v2 target architecture.

---

## 1. M0 Goals — SEE

System must answer:

- How many jobs created? `jobs_created_total`
- How many claimed? `jobs_claimed_total`
- How many completed? `jobs_completed_total`
- How many failed? `jobs_failed_total`
- How many recovered? `jobs_recovered_total`
- Last activity when? via `queue_depth` + `time_since_last_completion` (SLO)
- Readiness real or just process alive? via `/readyz` contract
- Correlation/idempotency evidence where? via correlation-id lifecycle + effect-key

---

## 2. Canonical Metrics (M0)

All metrics live in `src/nexus_ai_agent/infrastructure/observability/metrics.py` (low-cardinality registry) and `src/nexus_ai_agent/observability/metrics.py` (typed facade).

| Metric | Canonical Name | Alias | Labels | Cardinality Policy | Source Event | Lifecycle |
|---|---|---|---|---|---|---|
| jobs created | `jobs_created_total` | `nexus_jobs_created_total` | `job_type` (bounded) | job_type from bounded set, unknown→"unknown", no telegram_id/URL/UUID/raw text, max 64 chars | `InProcessJobQueue._insert_or_get` after INSERT | counter, monotonic, process-local, reset only on restart/test |
| jobs claimed | `jobs_claimed_total` | `nexus_jobs_claimed_total` | `job_type` | same | `_mark_processing` | counter, monotonic |
| jobs completed | `jobs_completed_total` | `nexus_jobs_completed_total` | `job_type` | same | `_mark_completed` | counter, monotonic, terminal |
| jobs failed | `jobs_failed_total` | `nexus_jobs_failed_total` | `job_type`, `error_code` | job_type bounded, error_code bounded, unknown→"unknown", no raw exception text as label | `_mark_failed` | counter, monotonic, terminal, paired with diagnostics event |
| jobs recovered | `jobs_recovered_total` | `nexus_jobs_recovered_total` | `reason` | reason bounded | `_reset_unfinished` / `resume_pending` | counter, monotonic |

**Bounded sets:**

- `job_type`: `pdf_extract`, `story`, `slideshow_render`, `image_gen`, `video_render`, `audio_process`, `caption`, `ad_delivery`, `channel_schedule`, `reminder`, `unknown` (max 11 values, extensible but capped at 20)
- `error_code`: `handler_not_found`, `handler_exception`, `payload_invalid`, `timeout`, `cancelled`, `unknown`, `validation_error`, `external_api_error`
- `reason`: `expired_lease`, `startup_recovery`, `orphaned_processing`, `pending_requeue`, `operator_resume`, `unknown`

**High-cardinality guard:**

- Any label value >64 chars rejected
- Any value containing `://` or starting with `@` rejected
- Unsupported label names (e.g., `telegram_id`, `url`, `random_uuid`, `api_key`) raise `ValueError`
- Unknown `job_type` for canonical metrics normalized to `"unknown"` to cap cardinality

**Tests:** `tests/unit/test_metrics_m0.py` — 15 tests, deterministic, no network.

---

## 3. Correlation ID Lifecycle

**Audit result (2026-09-23):**

Existing code:

- `bot/handlers.py`: `correlation_id = str(uuid4())`, bound via `structlog.contextvars.bind_contextvars`, stored in `NexusState.correlation_id`
- `orchestration/state.py`: `NexusState` TypedDict has `correlation_id: str`
- `storage/models.py`: `correlation_id` field indexed
- `cli.py`: correlation_id per CLI command via uuid4()

Answers:

- **per update?** YES — new uuid4 per Telegram Update in `handlers.py`
- **per job?** PARTIAL — job payload does NOT yet carry correlation_id in production; M0 provides helpers `inject_into_payload`, `extract_from_payload`, `ensure_correlation_id`
- **per external effect?** NO — external API calls do not yet receive correlation_id header; M0 provides contract that correlation_id should be log field, never metric label

**M0 implementation:** `src/nexus_ai_agent/observability/correlation.py`

- `new_correlation_id()`: random 32 hex chars
- `deterministic_correlation_id(seed)`: SHA-256 truncated, stable for same seed (e.g., `update_id`) for tracing retries
- `bind_correlation_id`, `get_correlation_id`, `clear_correlation_id`: structlog contextvars integration
- `inject_into_payload`, `extract_from_payload`, `ensure_correlation_id`: job payload propagation
- `LIFECYCLE_DOC`: documented lifecycle

**Properties:**

- deterministic enough for tracing: `deterministic_correlation_id(update_id)` stable
- never secret: uuid4 hex, not credential
- never user-sensitive: no telegram_id, no user text
- never high-cardinality metric label: guarded by test that registry rejects `correlation_id` label

**Test:** `tests/unit/test_correlation_m0.py` — 12 tests.

---

## 4. Readiness vs Liveness

**Liveness:** `GET /healthz` — DB-free, 200 if process up, defined in `api/app.py`

**Readiness:** `GET /readyz` — must distinguish readiness from liveness.

**Contract:** `src/nexus_ai_agent/observability/readiness.py`

- 200 only when:
  - application substrate ready (engines initialized)
  - dependencies present (DB reachable, job queue initialized)
  - migration/startup invariant holds (at head)
- 503 otherwise (startup failure, missing dependency, shutdown)

**Why not just process alive => ready?** Because scale-to-zero platforms (Koyeb) may route traffic to a process that is up but not yet initialized; DB may be cold; migrations may be pending. Readiness must fail closed.

**M0 provides:**

- `ReadinessCheck`, `ReadinessState`, `ReadinessStatus` types
- `evaluate_readiness(probes, is_shutting_down)` pure function
- Built-in probes: `probe_db_connection`, `probe_migration_head`, `probe_job_queue_initialized`, `probe_application_substrate`, `probe_always_ready/not_ready`
- `READYZ_CONTRACT` dict documenting endpoint shape and rules

**Blocked:** actual HTTP endpoint lives in `api/app.py` which is in `core-security` zone (not owned by observability-reliability). M0 provides contract+tests+blocker, endpoint implementation requires coordination with core-security owner.

**Tests:** `tests/unit/test_health_m0.py` — 10 tests:

- healthy -> ready (200)
- startup failure -> not ready (503)
- missing dependency -> not ready (503)
- migration drift -> not ready (503)
- shutdown -> not ready (503)
- probe exception -> not ready fail-closed
- liveness vs readiness separation

---

## 5. Diagnostic Visibility

**Audit findings reproduced:**

1. metrics snapshot maybe cannot observe real bot — reproduced and fixed: `test_metrics_snapshot_observes_real_bot` proves snapshot contains job metrics after lifecycle
2. job failures may lack notable logs — reproduced and fixed: `log_job_failure` emits both structured event and standard log, with metric increment

**M0 provides:** `src/nexus_ai_agent/observability/diagnostics.py`

For each failure:

- event name: `job_failed` (canonical)
- job identity non-sensitive: `job_type`, truncated `job_id` (8 chars)
- correlation id: from context or explicit
- failure class/type: `failure_class` (bounded error_code)
- duration: `duration_ms` via `JobTimer` (monotonic)
- retry/terminal state: `retry_state` = `terminal` | `retryable` | `recovered`

Never logged:

- secret, raw user text, token, API key, sensitive URL (redaction via `log_lifecycle_event`)

**Helpers:**

- `JobTimer`: monotonic timer for job duration
- `log_job_failure`, `log_job_completed`, `log_job_created`, `log_job_claimed`, `log_job_recovered`: fail-safe wrappers that never raise

**Tests:** `tests/unit/test_observability_m0.py` — 8 tests.

---

## 6. SLO Signals — Golden Signals

Defined in `src/nexus_ai_agent/observability/slo.py`:

| Signal | SLI | Source | Metric | Unit |
|---|---|---|---|---|
| LATENCY | job_duration_seconds | JobTimer, started_at/finished_at | histogram/summary of duration | seconds |
| TRAFFIC | jobs_created_total + updates_observed | jobs_created_total + handlers | jobs_created_total, nexus_touch_total | jobs/sec |
| ERRORS | jobs_failed_total / jobs_created_total | jobs_failed_total metric | jobs_failed_total, error_code | ratio 0..1 |
| SATURATION | queue_depth, inflight, worker_utilization | SELECT COUNT(*) WHERE status | queue_depth, inflight_jobs | count/ratio |

**Three alert policies:**

1. **high_failure_rate**: failure rate >20% over 5m → critical
   - metric: `jobs_failed_total / jobs_created_total`
   - threshold: 0.2, comparison gt, window 300s
2. **queue_stuck_no_progress**: pending>0 and no completion for 10m → warning
   - metric: `queue_depth + time_since_last_completion`
   - threshold: 600s, gt, window 600s
3. **readiness_degraded**: readiness failing >60s → critical
   - metric: `readiness_status`
   - threshold: 60s, gt, window 60s

No real alert delivery for M0; metric+threshold+testable evaluator provided.

Pure evaluators: `evaluate_failure_rate`, `should_alert_failure_rate`, `should_alert_queue_stuck`, `should_alert_readiness_degraded`, `measure_saturation_from_counts`

**Tests:** `tests/unit/test_slo_m0.py` — 15 tests.

---

## 7. P1/P2 Preparation — Without Stealing Delivery

Constraints: must NOT touch `worker.py` or `bot/app.py` for full implementation.

**M0 provides:** `src/nexus_ai_agent/observability/reliability.py`

- **P1 Claim+Lease:**
  - `ClaimLeasePort` protocol (SQLite/Postgres-neutral)
  - `Lease` dataclass with `is_expired`, `is_owned_by`
  - `InMemoryClaimLease` fake: thread-safe, in-memory, exactly one winner for concurrent claims
  - Invariant: two claims concurrent => exactly one winner (test A)
  - Expired lease => recoverable (test C)

- **P2 Inbox/Receipt:**
  - `InboxMessage`, `Receipt` types
  - `InboxPort` protocol
  - `InMemoryInbox` fake: deduplicates by message_id, one logical receipt for same update_id twice (test B)
  - Invariant: same message_id twice => one logical receipt

- **P4 Effect-Key:**
  - `compute_effect_key(operation, params, namespace)` — stable hash, sorted canonical JSON, SHA-256 truncated
  - `EffectRecord`, `EffectStorePort`, `InMemoryEffectStore`
  - Invariant: duplicate logical effect => effect-key stable (test D)
  - No secret leakage: key is hash, not raw params

**Acceptance tests (no network, no flaky timing):**

- A: `test_claim_single_winner_concurrent` — two threads claim same resource, exactly one winner
- B: `test_inbox_same_update_id_one_receipt` — same update_id twice => one receipt
- C: `test_claim_expired_recoverable` — expired lease => new owner can claim
- D: `test_effect_key_stable` — same operation+params => same key

Plus property test: 5 owners concurrent, still one winner.

**Tests:** `tests/unit/test_reliability_m0.py` — 14 tests.

---

## 8. No Big Infrastructure

M0 uses only existing primitives:

- SQLite (job queue sidecar)
- In-process registry (no Redis, no Kafka, no Celery)
- structlog contextvars (no distributed tracing backend)
- Pure Python helpers (no microservices)

If Postgres contract needed: interface + acceptance tests provided, not migration.

---

## 9. Integration with Existing Job Queue

M0 metrics are designed to be wired into `InProcessJobQueue` without stealing delivery:

- `log_job_created` in `_insert_or_get` after new row
- `log_job_claimed` in `_mark_processing`
- `log_job_completed` in `_mark_completed`
- `log_job_failure` in `_mark_failed`
- `log_job_recovered` in `_reset_unfinished`

This wiring is **not** done in M0 to avoid touching `adapters/in_process_job_queue.py` which is not in observability zone? Actually `adapters/` is in `delivery-interop` zone (not active), but to be safe M0 provides the helpers and tests; wiring can be done by job queue owner or next wave.

Blocker recorded in audit doc if needed.

---

## 10. Files Delivered

- `src/nexus_ai_agent/observability/metrics.py` — canonical metrics, contracts, bounded sets, helpers
- `src/nexus_ai_agent/observability/correlation.py` — lifecycle audit, generation, propagation
- `src/nexus_ai_agent/observability/readiness.py` — readiness vs liveness, pure evaluators, contract
- `src/nexus_ai_agent/observability/diagnostics.py` — failure visibility, JobTimer, fail-safe logging
- `src/nexus_ai_agent/observability/slo.py` — golden signals, 3 alert policies, evaluators
- `src/nexus_ai_agent/observability/reliability.py` — P1/P2/P4 contracts, fake impls, invariants
- `src/nexus_ai_agent/observability/__init__.py` — public exports
- `src/nexus_ai_agent/infrastructure/observability/metrics.py` — enhanced with job_type label, bounded sets, high-cardinality guard, reset helper

Tests:

- `tests/unit/test_metrics_m0.py`
- `tests/unit/test_correlation_m0.py`
- `tests/unit/test_health_m0.py`
- `tests/unit/test_reliability_m0.py`
- `tests/unit/test_slo_m0.py`
- `tests/unit/test_observability_m0.py`

Docs:

- `docs/architecture/OBSERVABILITY_M0.md` (this file)
- `docs/audits/OBSERVABILITY_M0_2026-09-23.md` (audit record)

---

## 11. Next Steps (M1/M2/M3)

- Wire metrics into `InProcessJobQueue` (needs coordination with queue owner)
- Implement `/readyz` endpoint in `api/app.py` using `evaluate_readiness` (needs core-security coordination)
- Add correlation-id to job payloads in `bot/handlers.py` and `worker.py` (needs feature-wiring coordination)
- Add Postgres-backed `ClaimLease` implementation (interface already defined)
- Add outbox pattern (P3) and effect-key enforcement in job handlers
- Add latency histogram (currently only counter + timer, no histogram)
- Add dashboard for metrics snapshot with job metrics

---

## 12. Cardinality Policy Summary

**Never label with:**

- telegram_id, user_id (raw)
- URL, API key, token, secret
- raw user text, file contents
- random UUID, correlation_id

**Allowed labels (bounded):**

- job_type (bounded set, unknown→"unknown")
- error_code (bounded)
- reason (bounded)
- outcome (bounded)
- backend, scope (existing)

**Enforcement:** `MetricsRegistry.increment` raises `ValueError` for unsupported labels, high-cardinality values, or overly long values. Tests prove rejection.

---

## 13. References

- `research/v2/13-target-architecture.md` (not found in repo, but target architecture described in mission: P1 Claim+Lease, P2 Inbox/Receipt, P3 Outbox, P4 Effect-Key)
- `docs/architecture/OBSERVABILITY.md` — O1 observability (lifecycle events, redaction, metrics allow-list, healthz)
- `docs/architecture/RUNTIME_FLOWS.md` — job queue flow
- `docs/DECISION_LOG.md` — decisions
- `TASK159` closeout — ad delivery tick (context)
