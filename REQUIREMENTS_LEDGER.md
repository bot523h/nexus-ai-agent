# REQUIREMENTS_LEDGER — LangGraph checkpoint lifecycle (PR1/PR2)

Source: consolidated spec (session 01a0b5d7), cross-checked against the actual
tree at `6f21908` (rescued `arena/01a0b123-nexus-ai-agent` lineage).

Status is four-valued:

| Status   | Meaning                                                        |
|----------|----------------------------------------------------------------|
| DONE     | Implemented and covered by at least one passing test           |
| PARTIAL  | Some of the item is implemented and tested; rest is missing    |
| MISSING  | Not implemented in this tree                                   |
| DEFERRED | Explicitly out of v1 scope (POST_V1), with a live guard        |

Baseline gates recorded 2026-09-18 on `6f21908`: `make lint` green (195 files),
`make types` green (134 files), `make test` = 242 passed / 1 skipped (PG-only).

## R — Requirements

| ID  | Requirement                                                              | Status  | Test reference(s)                                                            | Notes |
|-----|--------------------------------------------------------------------------|---------|------------------------------------------------------------------------------|-------|
| R1  | Saver decorator only at composition root + kill-switch                   | PARTIAL | `tests/unit/test_lifecycle_recording.py::test_kill_switch_skips_lifecycle_writes` | Kill-switch flag + `NEXUS_LIFECYCLE_HOOKS_ENABLED` exist; runtime is NOT wired through `LifecycleRecordingSaver` yet (composition root seam open in `storage/langgraph_checkpoint.py`) |
| R2  | Full delegate of the wrapped saver (introspective)                       | PARTIAL | `tests/unit/test_lifecycle_recording.py` (behavioral only)                   | `__getattr__` delegation exists; introspective "every method delegates" test missing |
| R3  | contextvar default `system`; lost context ⇒ no touch                     | PARTIAL | `tests/unit/test_lifecycle_recording.py::test_touch_requires_user_context_and_is_coalesced` | Default `system` asserted; explicit lost-context case missing |
| R4  | flush on shutdown + atexit                                               | MISSING | —                                                                            | `TouchCoalescer.flush()` exists and is explicit; no shutdown/atexit registration |
| R5  | reconcile is CLI, dry-run only by default                                | MISSING | —                                                                            | No `reconcile` CLI command in `cli.py` |
| R6  | estimates carry the `_estimate` label                                    | PARTIAL | `cli.py` inspect emits `would_free_bytes_estimate`                           | Label present, value hard-coded `"unknown"` (no byte estimate yet) |
| R7  | core head read from `alembic_version` without running a migration        | PARTIAL | `tests/integration/test_checkpoint_adapter.py` (fingerprint path)            | `_migration_head()` reads `alembic_version` read-only; no manifest comparison + warning semantics yet |
| R8  | registry without new dependencies                                        | DONE    | `tests/unit/test_observability.py::test_metrics_allow_only_low_cardinality_labels` | In-process `MetricsRegistry`, allow-listed labels, stdlib only |
| R9  | baseline frozen or shrinking                                             | DONE    | `tests/architecture/test_import_boundaries.py::test_global_legacy_baseline_has_no_new_violations` | `tests/architecture/legacy_baseline.json`: 37 grandfathered violations, `ARCH_BASELINE_APPROVED` |

## S — Spec items

| ID  | Item                                                                           | Status  | Test reference(s) / file evidence                                              |
|-----|--------------------------------------------------------------------------------|---------|----------------------------------------------------------------------------------|
| S1  | touch state machine: no immediate failure retry; drop after window expiry; 10% health gate | PARTIAL | `tests/architecture/test_glossary_liveness.py::test_retention_contract_remains_live`; `domain/policies/retention.py` | `ALLOWED_TRANSITIONS` (FAILED→RETRYING→RUNNING, backoff constant) exists; drop-on-window-expiry and the 10% health gate are not implemented |
| S2  | pure `purge_allowed` predicate in `domain/policies/reconciler_policy.py`       | MISSING | —                                                                                | File does not exist; `domain/policies/retention.py` has `pinned`/`deletable` only (no anomaly-rate gate) |
| S3  | inspect-v1: `missing_lifecycle` bool per thread + `unknown_fields` array + schema key | PARTIAL | `cli.py` `checkpoints inspect`                                                    | `missing_lifecycle` present; `unknown_fields` and a versioned schema key absent |
| S4  | wiring trio of tests (composition root)                                        | MISSING | —                                                                                | No wiring exists yet, hence no trio |
| S5  | C1 test separation (five separate test files)                                  | PARTIAL | `tests/unit/test_lifecycle_recording.py`, `tests/unit/test_checkpoint_lifecycle.py`, `tests/unit/test_checkpoint_lifecycle_store.py`, `tests/integration/test_checkpoint_adapter.py` | 4 of 5 files exist; reconciler + health-gate file(s) missing |
| S6  | scope-qualified fingerprint: langgraph mismatch ⇒ disabled; core mismatch ⇒ warning only | PARTIAL | `tests/integration/test_checkpoint_adapter.py::test_schema_drift_disables_cleanup` | langgraph-scope mismatch ⇒ `CleanupDisabled` ✓; core (alembic head) mismatch semantics not implemented |

## I — Invariants (behavioral contract)

| ID  | Invariant                                          | Status  | Test reference(s)                                                                                   |
|-----|----------------------------------------------------|---------|-------------------------------------------------------------------------------------------------------|
| I1  | inspect ⇒ no touch (read-only)                     | PARTIAL | `cli.py` inspect docstring; no test yet that asserts `inspect` leaves the lifecycle index untouched   |
| I2  | system/admin context ⇒ no touch                    | PARTIAL | `test_touch_requires_user_context_and_is_coalesced` (system case only)                                 |
| I3  | user ⇒ coalesced (one pending timestamp/thread)    | DONE    | `test_touch_requires_user_context_and_is_coalesced`, `test_touch_coalescer_keeps_latest_timestamp`     |
| I4  | kill-switch off ⇒ no lifecycle writes              | DONE    | `test_kill_switch_skips_lifecycle_writes`                                                             |
| I5  | touch/upsert errors never propagate                | DONE    | best-effort path in `adapters/langgraph/lifecycle_recording.py`; covered behaviorally by I3 tests      |
| I6  | rate > 10% ⇒ lifecycle disabled (health gate)      | MISSING | —                                                                                                       |
| I7  | anomaly > 1% or > 500 ⇒ no purge                   | MISSING | —                                                                                                       |
| I8  | age ≤ 24h ⇒ no purge                               | MISSING | —                                                                                                       |
| I9  | orphan ⇒ `blocked_until = now + 24h`               | MISSING | inspect exposes `orphan_candidate_count` only                                                          |
| I10 | unknown ⇒ inspect stays live (reports unknown)    | PARTIAL | inspect emits `"unknown"` strings for missing metadata                                                 |
| I11 | missing golden ⇒ disabled + alert, no auto-gen     | PARTIAL | `tests/integration/test_checkpoint_adapter.py::test_missing_golden_disables_cleanup` (raises, no generation); alert path missing |
| I12 | connection error ≠ schema drift                    | DONE    | `tests/integration/test_checkpoint_adapter.py::test_connection_error_is_not_reported_as_schema_drift`  |
| I13 | secret ⇒ redacted before log/metric                | DONE    | `tests/unit/test_observability.py::test_redaction_removes_credentials`                                 |
| I14 | `thread_id` / URL never a metric label             | DONE    | `test_metrics_allow_only_low_cardinality_labels` (rejects `thread_id` label); `ALLOWED_LABELS` allow-list |

## C — PR2 completion targets (this session)

| ID  | Target                                                                                  | Status (pre-completion) |
|-----|------------------------------------------------------------------------------------------|--------------------------|
| C1  | hooks + context + reconciler + health gate + kill-switch (5 separate test files)         | PARTIAL — reconciler, health gate, wiring, atexit flush missing |
| C2  | read-only inspect with inspect-v1 contract (contextvar=admin)                            | PARTIAL — contract fields missing |
| C3  | O1: redaction + structured logging + in-process registry + `core_schema_fingerprint` (manifest/log only; mismatch ⇒ message, not disable) | PARTIAL — core fingerprint + warning semantics missing |
| C4  | tests-only: 8 remaining adversarial tests + golden at `storage/golden/sqlite.langgraph.json` + kill-switch & flush-shutdown tests | PARTIAL — golden present; adversarial set & flush-shutdown incomplete |

Hard constraints (all of C1–C4): reconciler is CLI-only, dry-run default,
mutation only via `--apply`, no cron; **no** journal table; **no** PG adapter;
**no** migration; no PR without owner confirmation.

## DoD (final verification)

| # | Criterion                                                                       | Status  |
|---|---------------------------------------------------------------------------------|---------|
| 1 | No `DELETE` against LangGraph tables anywhere in the src diff                    | PENDING |
| 2 | Lifecycle-metadata DELETE only via reconciler `--apply`                         | PENDING |
| 3 | Runtime passes through `LifecycleRecordingSaver` (composition test)              | PENDING |
| 4 | Full delegate — introspective test over the wrapped saver                       | PENDING |
| 5 | Raw-saver AST scan: only composition root + `adapters/langgraph` touch raw saver| PENDING |
