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

Final state recorded 2026-09-18 on `02f8d18` (C1…C4 + smoke fix):
`ruff` clean, `mypy` clean (140 files), `pytest -m "not slow"` =
**295 passed / 1 skipped** (296 collected).

## R — Requirements

| ID  | Requirement                                                              | Status | Test reference(s)                                                                    | Notes |
|-----|--------------------------------------------------------------------------|--------|--------------------------------------------------------------------------------------|-------|
| R1  | Saver decorator only at composition root + kill-switch                   | DONE   | `tests/integration/test_checkpoint_composition.py` (PG ⇒ bare / off ⇒ bare / on ⇒ wrap + atexit); `test_kill_switch_skips_lifecycle_writes`; `test_lifecycle_adversarial.py::test_cli_kill_switch_blocks_apply` | `get_checkpointer()` is the only seam; `NEXUS_LIFECYCLE_HOOKS_ENABLED` kill-switch; atexit `flush_sync` registered |
| R2  | Full delegate of the wrapped saver (introspective)                       | DONE   | `tests/unit/test_lifecycle_adversarial.py::test_wrapper_exposes_full_saver_surface` | every public name of the real `SqliteSaver` resolves on the wrapper; read parity for `get_tuple`/`list`/`config_specs`/`serde`; `__getattr__` covers future upstream names |
| R3  | contextvar default `system`; lost context ⇒ no touch                     | DONE   | `test_lost_context_means_no_touch` (default + set-then-reset + live-user coalescing); `test_touch_requires_user_context_and_is_coalesced` | `nexus_access_context` defaults to `system`; only a live `user` context touches |
| R4  | flush on shutdown + atexit                                               | DONE   | `test_checkpoint_composition.py` (atexit registration); `test_lifecycle_adversarial.py::test_shutdown_flush_persists_pending_touches` | `flush_sync()` runs with no running loop; pending touch persisted; second flush no-op |
| R5  | reconcile is CLI, dry-run only by default                                | DONE   | `test_cli_reconcile_dry_run_default_and_json`; `test_cli_reconcile_apply_requires_flag` | `nexus checkpoints reconcile`; mutation only with `--apply`; no cron |
| R6  | estimates carry the `_estimate` label                                    | DONE   | `tests/unit/test_inspect_contract.py` (`would_free_bytes_estimate` is a real int) | value from `SQLiteCheckpointAdapter.estimate_thread_bytes` (2.x + legacy guarded) |
| R7  | core head read from `alembic_version` without running a migration        | DONE   | `tests/unit/test_core_fingerprint.py` (`test_core_head_read_from_alembic_version`, `test_core_head_absent_returns_none`, `test_connection_error_is_not_core_drift`) | plain `SELECT` on `alembic_version`; no `Script.run_env`, no migration run |
| R8  | registry without new dependencies                                        | DONE   | `tests/unit/test_observability.py::test_metrics_allow_only_low_cardinality_labels` | in-process `MetricsRegistry`, allow-listed labels, stdlib only |
| R9  | baseline frozen or shrinking                                             | DONE   | `tests/architecture/test_import_boundaries.py::test_global_legacy_baseline_has_no_new_violations` | `tests/architecture/legacy_baseline.json`: 37 grandfathered violations, unchanged |

## S — Spec items

| ID  | Item                                                                           | Status | Test reference(s) / file evidence |
|-----|--------------------------------------------------------------------------------|--------|-----------------------------------|
| S1  | touch state machine: no immediate failure retry; drop after window expiry; 10% health gate | DONE | `tests/architecture/test_glossary_liveness.py::test_retention_contract_remains_live`; `domain/policies/retention.py` (`ALLOWED_TRANSITIONS`, backoff); `tests/unit/test_health_gate.py` (gate > 10% ⇒ disabled; immediate latch); coalescer drop-window in `lifecycle_recording.py` |
| S2  | pure `purge_allowed` predicate in `domain/policies/reconciler_policy.py`       | DONE | `tests/unit/test_lifecycle_adversarial.py::test_purge_policy_boundary_values`; `tests/unit/test_reconciler.py` (rate/absolute guards block purge) |
| S3  | inspect-v1: `missing_lifecycle` bool per thread + `unknown_fields` array + schema key | DONE | `tests/unit/test_inspect_contract.py` (`schema="inspect-v1"`, sorted `unknown_fields`, `missing_lifecycle`) |
| S4  | wiring trio of tests (composition root)                                        | DONE | `tests/integration/test_checkpoint_composition.py` — PG ⇒ bare saver; kill-switch off ⇒ bare; on ⇒ `LifecycleRecordingSaver` + atexit |
| S5  | C1 test separation (five separate test files)                                  | DONE | `tests/unit/test_lifecycle_recording.py`, `tests/unit/test_checkpoint_lifecycle.py`, `tests/unit/test_checkpoint_lifecycle_store.py`, `tests/integration/test_checkpoint_adapter.py`, `tests/unit/test_reconciler.py`, `tests/unit/test_health_gate.py` (6 files) |
| S6  | scope-qualified fingerprint: langgraph mismatch ⇒ disabled; core mismatch ⇒ warning only | DONE | `test_reconciler.py::test_schema_mismatch_disables_apply` (langgraph ⇒ disabled); `tests/unit/test_core_fingerprint.py` (core mismatch/missing/manifest-unavailable ⇒ warning only, apply still proceeds) |

## I — Invariants (behavioral contract)

| ID  | Invariant                                          | Status | Test reference(s) |
|-----|----------------------------------------------------|--------|-------------------|
| I1  | inspect ⇒ no touch (read-only)                     | DONE | `tests/unit/test_inspect_contract.py` (no lifecycle file created; no `last_accessed_at` written; contextvar=admin set) |
| I2  | system/admin context ⇒ no touch                    | DONE | `test_lost_context_means_no_touch` (default system never touches; the gate is `!= "user"`, so admin included); `test_touch_requires_user_context_and_is_coalesced` |
| I3  | user ⇒ coalesced (one pending timestamp/thread)    | DONE | `test_touch_requires_user_context_and_is_coalesced`, `test_touch_coalescer_keeps_latest_timestamp`, `test_lost_context_means_no_touch` (two reads → one touch) |
| I4  | kill-switch off ⇒ no lifecycle writes              | DONE | `test_kill_switch_skips_lifecycle_writes`; `test_checkpoint_composition.py` (off ⇒ bare saver); `test_cli_kill_switch_blocks_apply` |
| I5  | touch/upsert errors never propagate                | DONE | `test_lifecycle_errors_never_propagate` (record + touch paths: results intact, no exception) |
| I6  | rate > 10% ⇒ lifecycle disabled (health gate)     | DONE | `tests/unit/test_health_gate.py`; `test_lifecycle_errors_never_propagate` (immediate latch, process-lifetime) |
| I7  | anomaly > 1% or > 500 ⇒ no purge                   | DONE | `test_reconciler.py` (anomaly rate blocks purge); `test_purge_policy_boundary_values` (exactly 1.0% allowed, 1.01% blocked; exactly 500 allowed, 501 blocked) |
| I8  | age ≤ 24h ⇒ no purge                               | DONE | `test_reconciler.py` (young record not purged); `test_purge_policy_boundary_values` (24h blocks, 24h+1s allows) |
| I9  | orphan ⇒ `blocked_until = now + 24h`               | DONE | `test_reconciler.py::test_orphan_decision_blocks_until_plus_24h` (stateless derivation; unknown reference ⇒ never purge) |
| I10 | unknown ⇒ inspect stays live (reports unknown)    | DONE | `tests/unit/test_inspect_contract.py` (unknown values stay in output; `unknown_fields` only names them, sorted) |
| I11 | missing golden ⇒ disabled + alert, no auto-gen     | DONE | `test_reconciler.py::test_missing_golden_disables_apply_and_alerts` (structured alert, no file); `test_lifecycle_adversarial.py::test_missing_golden_never_generates_files` (real golden dir byte-identical after two refused applies) |
| I12 | connection error ≠ schema drift                    | DONE | `test_checkpoint_adapter.py::test_connection_error_is_not_reported_as_schema_drift`; `test_core_fingerprint.py::test_connection_error_is_not_core_drift` |
| I13 | secret ⇒ redacted before log/metric                | DONE | `test_observability.py::test_redaction_removes_credentials`; `test_structured_events.py` (password/bearer/URL-credentials redacted in structured events; redaction failure drops values, never raises) |
| I14 | `thread_id` / URL never a metric label             | DONE | `test_metrics_allow_only_low_cardinality_labels` (rejects `thread_id` label); `ALLOWED_LABELS` allow-list |

## C — PR2 completion targets (this session)

| ID  | Target                                                                                  | Status | Evidence |
|-----|------------------------------------------------------------------------------------------|--------|----------|
| C1  | hooks + context + reconciler + health gate + kill-switch (5 separate test files)         | DONE | `6d172f2` — 6 test files (S5); composition root + atexit; `reconcile` CLI; `purge_allowed` |
| C2  | read-only inspect with inspect-v1 contract (contextvar=admin)                            | DONE | `dba1702` — `tests/unit/test_inspect_contract.py` (5 tests) |
| C3  | O1: redaction + structured logging + in-process registry + `core_schema_fingerprint` (manifest/log only; mismatch ⇒ message, not disable) | DONE | `35e0ba6` — `infrastructure/observability/structured.py`; `storage/checkpoint_fingerprint.py`; `test_core_fingerprint.py` (9), `test_structured_events.py` (4) |
| C4  | tests-only: 8 remaining adversarial tests + golden at `storage/golden/sqlite.langgraph.json` + kill-switch & flush-shutdown tests | DONE | `fdef331` — `tests/unit/test_lifecycle_adversarial.py` (8 tests); golden present since lineage; continuum count refreshed to 296 |

Hard constraints (all of C1–C4): reconciler is CLI-only, dry-run default,
mutation only via `--apply`, no cron; **no** journal table; **no** PG adapter;
**no** migration; no PR without owner confirmation.

## DoD (final verification)

| # | Criterion                                                                       | Status | Evidence |
|---|---------------------------------------------------------------------------------|--------|----------|
| 1 | No `DELETE` against LangGraph tables anywhere in the src diff                    | DONE | grep: the only `DELETE FROM` in our storage code is `checkpoint_lifecycle_store.py` (`_TABLE` = `checkpoint_lifecycle`, our metadata table); no `DELETE FROM checkpoints`/`writes` in src |
| 2 | Lifecycle-metadata DELETE only via reconciler `--apply`                         | DONE | `store.delete_index` has exactly one caller: `checkpoint_reconciler.py` `_purge`, reachable only after `apply=True` + kill-switch + golden match + health ok + cleanup lock + golden re-assert |
| 3 | Runtime passes through `LifecycleRecordingSaver` (composition test)              | DONE | `tests/integration/test_checkpoint_composition.py` (three-way wiring contract) |
| 4 | Full delegate — introspective test over the wrapped saver                       | DONE | `test_lifecycle_adversarial.py::test_wrapper_exposes_full_saver_surface` |
| 5 | Raw-saver AST scan: only composition root + `adapters/langgraph` touch raw saver| DONE | `tests/architecture/test_saver_boundary.py`; concrete-saver names appear in src only in `storage/langgraph_checkpoint.py` (the composition root) |
