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

## D — PR3 targets (Phase 5, this session)

| ID  | Target                                                                                  | Status | Evidence |
|-----|------------------------------------------------------------------------------------------|--------|----------|
| D1  | read-only Postgres checkpoint adapter (same contract as SQLite)                          | DONE | `063e7df` — `storage/checkpoint_pg_adapter.py`; read-only enforced server-side (`default_transaction_read_only`, psycopg 25006 test) |
| D2  | one contract, two backends (fingerprint/golden/lineage/estimate)                        | DONE | `063e7df` — `tests/integration/test_checkpoint_adapter_contract.py` (24 tests; PG leg in CI `migrate-postgres` job) |
| D3  | PG composition: real `PostgresCheckpointer` (isinstance bug fixed) + lifecycle wrapper   | DONE | `aed8728` — four-way matrix in `test_checkpoint_composition.py` incl. live PG e2e (data → PG, lifecycle → sidecar) |
| D4  | lifecycle store on PG path — **superseded by option A (see E)**; sidecar kept only on the SQLite path | SUPERSEDED | `aed8728` (B) → `d214e1a`/`1133df1` (A) |
| D5  | backend-aware CLI: inspect/reconcile on PG; reconcile = no-op on PG (SQLite-only purge)  | DONE | `aed8728` — `_open_checkpoint_backend`; `purge_eligible: false` on PG with reason |
| D6  | `golden update` command (human-in-the-loop only; `--yes` required for write)             | DONE | `aed8728` — `tests/unit/test_golden_management.py` (5 tests incl. live PG fingerprint == committed golden) |
| D7  | `postgres.langgraph.json` committed golden (schema-v1-pg; LangGraph scope only)          | DONE | `063e7df` — generated under owner authority from PG 18; stability on CI's pg16 proven by contract test |
| D8  | ops runbook + DATA_LIFECYCLE schema map + ledger/roadmap/continuum refresh               | DONE | `43a1b6a` (schema map) + this commit — `docs/ops/NEON_LIFECYCLE_RUNBOOK.md` |

PR3 hard constraints (owner): no new core dependencies; kill-switch +
flush at composition root only; touch errors never propagate; connection
error ≠ drift; golden update human-triggered only, never in CI.  The
"zero migrations" constraint was set under option B and lifted by the
owner for option A **only** for the single isolated lifecycle-table
revision (strict isolation condition — see E); migrations remain
forbidden everywhere else.

Final gate (option A state): **320 passed, 20 skipped** (`-m "not slow"`,
no PG); with live PG: **345 passed, 0 failed — twice consecutively on the
same database**; ruff check + format clean; mypy clean (142 files).

## E — PR3 option A decision (this session)

| ID  | Decision / Target                                                                 | Status | Evidence |
|-----|------------------------------------------------------------------------------------|--------|----------|
| E1  | owner decision: lifecycle index moves from local SQLite sidecar (B) to the database (A) — serverless (Neon) local files are ephemeral; sidecar history would be lost on every restart | DECIDED | owner instruction 2026-09-19, with rationale |
| E2  | strict isolation condition: one isolated revision (`f4a9c2e71b08`) creating exactly one table; no other DDL; PG dialect only | DONE | `d214e1a` — migration + source-level isolation test (`test_lifecycle_migration_is_isolated_and_postgres_only`) |
| E3  | PG store: DML-only (AST test), lazy connection (boot-safe), server-enforced read-only for inspect | DONE | `d214e1a` — `checkpoint_lifecycle_pg_store.py` + `test_lifecycle_store_contract.py` |
| E4  | adopt-pg: stamped-at-head DB must contain the table; legacy DB without it = drift (fail-fast) | DONE | `d214e1a` — `TestHeadStateIncludesLifecycleTable` |
| E5  | composition: lifecycle rows land in the same database; no sidecar file on the PG path (asserted); kill-switch off = zero rows (live PG) | DONE | `1133df1` — four-way matrix + `test_postgres_kill_switch_off_writes_nothing` |
| E6  | golden regenerated via human-triggered CLI (`bac07283…` → `0842f831…`, head-only change) | DONE | `d214e1a` — one-line diff on `postgres.langgraph.json` |
| E7  | purge works uniformly on both backends (lifecycle rows only, full guard stack) | DONE | `1133df1` — reconciler store-agnostic; store-read failure contained as health-gate scan error |

Supersession note: section D rows D1–D3/D5–D7 remain valid; D4 (sidecar
on the PG path) is history — the sidecar remains on the SQLite path only
(see `docs/ops/NEON_LIFECYCLE_RUNBOOK.md`, "Lifecycle index maintenance").

## DoD (final verification)

| # | Criterion                                                                       | Status | Evidence |
|---|---------------------------------------------------------------------------------|--------|----------|
| 1 | No `DELETE` against LangGraph tables anywhere in the src diff                    | DONE | grep: the only `DELETE FROM` in our storage code is `checkpoint_lifecycle_store.py` (`_TABLE` = `checkpoint_lifecycle`, our metadata table); no `DELETE FROM checkpoints`/`writes` in src |
| 2 | Lifecycle-metadata DELETE only via reconciler `--apply`                         | DONE | `store.delete_index` has exactly one caller: `checkpoint_reconciler.py` `_purge`, reachable only after `apply=True` + kill-switch + golden match + health ok + cleanup lock + golden re-assert |
| 3 | Runtime passes through `LifecycleRecordingSaver` (composition test)              | DONE | `tests/integration/test_checkpoint_composition.py` (three-way wiring contract) |
| 4 | Full delegate — introspective test over the wrapped saver                       | DONE | `test_lifecycle_adversarial.py::test_wrapper_exposes_full_saver_surface` |
| 5 | Raw-saver AST scan: only composition root + `adapters/langgraph` touch raw saver| DONE | `tests/architecture/test_saver_boundary.py`; concrete-saver names appear in src only in `storage/langgraph_checkpoint.py` (the composition root) |

## F — PHASE 4 / PR4: Celery/Redis removal (R-001, R-026)

First corrective PR. Scope is exactly the two ledger items below; message
history, thread lifecycle, creative security and Nagar are untouched.
Order followed: failing tests proving the violation → architecture-contract
tests → implementation → `make lint` / `make types` / `make test` → hostile
`git diff` review.

| ID    | Requirement                                                                             | Status | Test reference(s) | Notes |
|-------|------------------------------------------------------------------------------------------|--------|-------------------|-------|
| R-001 | No distributed queue: Celery/Redis fully out of the runtime path (deps, worker, compose, `.delay()`, Redis rate limiter, broker settings) | DONE | `tests/architecture/test_no_distributed_queue.py` (6 contract tests, all red on `994a509`, plus a parser self-check: `worker.py` celery, `rate_limiter.py` redis, `handlers.py` `.delay`, pyproject deps, compose `{beat, bot, dashboard, redis, worker}`, 3 broker settings) | `worker.py` deleted (its `legacy_baseline.json` entry removed — baseline shrinks 37 → 36); `docker-compose.yml` = `bot` + `dashboard`; `RETIRED_BROKER_KEYS` shim keeps a v3.4.0-era `.env` bootable with a warning (`tests/unit/test_settings_retired_broker_keys.py`); verified with celery/redis/kombu/billiard **uninstalled**: 142 production modules import, full suite green, `make smoke` green |
| R-026 | `JobQueuePort` implemented in-process, SQLite-durable, statuses + result/error, PDF and story jobs preserved and invoked via `enqueue()` | DONE | `tests/integration/test_in_process_job_queue.py` (execution, non-blocking submit, idempotency incl. concurrent duplicates, failure ⇒ `failed` + error, unknown type/payload rejected before any write, restart durability + explicit resume, missing-handler-after-restart fails visibly, audit-trigger proof that every persisted transition is an `ALLOWED_TRANSITIONS` edge, JSON columns, bounded drain); `tests/unit/test_jobs.py` (real Pillow story PNG through the queue; PDF indexing via recording RAG double; binary PDF ⇒ recorded `UnicodeDecodeError`; engines run off the loop thread); `tests/unit/test_job_handlers.py` (handlers enqueue through the port with per-message idempotency keys; missing queue ⇒ visible reply; `build_application` wires one queue, no `post_init`, `post_shutdown` drains a real in-flight job) | Port file `application/ports/job_queue.py` untouched (`test_port_signatures.py` still green); adapter extras (`register`, `get_job`, `get_result`, `wait`, `resume_pending`, `close`) are adapter-level, not port additions; sidecar `<db_path>.jobs` — no migration, no app-schema change, no operation journal (Stage 3) |

Explicitly **not** done (needs an owner decision, not a silent change):
`resume_pending()` at boot (multi-instance caveat documented in the adapter);
scheduling `nightly_channel_management` (never had a beat schedule); real PDF
text extraction; completion notification for PDF/story jobs (the "I will
notify you" reply predates this PR and was never backed by code).

Gates on this PR: `ruff check` + `ruff format --check` clean (243 files);
`mypy src` clean (154 files); `pytest -m "not slow"` = **435 passed / 20
skipped** (was 386 / 20 on `994a509`; +49 = exactly the new tests, none
removed or weakened); `nexus continuum verify` count refreshed (357 → 405
collected test functions incl. slow-marked).

## G — PHASE 4 / PR4 follow-up: the four open decisions (D1–D4)

Each decision is one atomic commit whose body carries the Decision Record
(Evidence → Impact → Contract → Alternatives → Decision).

| ID | Decision                                                                                     | Status | Test reference(s) / evidence | Notes |
|----|-----------------------------------------------------------------------------------------------|--------|------------------------------|-------|
| D2 | `nightly_channel_management`: **removed dead code; channel management stays simulated until R-031.** No scheduler added. | DONE | `tests/architecture/test_no_distributed_queue.py::test_no_scheduler_in_production` (bans `apscheduler`/`schedule`/`croniter`/`rocketry` imports in `src/` and asserts `run_nightly_tasks` / `nightly_channel_management` are no longer defined; red on the previous tree) | Evidence: the Celery task had no `beat_schedule` and no caller; `ChannelManager` is never instantiated in `src/`; `post_top_users` / `post_viral_content` / `run_nightly_tasks` were reachable only through the task. The `/post`, `/schedule`, `/ban` handlers are explicitly "(simulated)". Removed the "Autonomous Channel Management (v3.4.0)" block + unused imports; channel primitives (post/pin/delete/schedule/ban/welcome) kept for R-031. `legacy_baseline.json` entry for `channel_manager.py` shrinks from `[sqlmodel, telegram]` to `[sqlmodel]` (the `telegram.error` import left with the dead code). |
| D3 | PDF: **real, bounded text extraction with pypdf under the optional `[pdf]` extra**; missing extra ⇒ explicit actionable error (no UTF-8 fallback, no silence). | DONE | `tests/unit/test_jobs.py`: `test_process_pdf_job_extracts_text_from_a_real_pdf` (real 887-byte two-page fixture `tests/fixtures/pdf/two_pages.pdf`, parses in pypdf strict mode), `test_pdf_job_without_the_extra_fails_with_an_actionable_error` (`sys.modules["pypdf"]=None` ⇒ `PdfSupportMissingError` naming `pip install 'nexus-ai-agent[pdf]'`), `test_pdf_job_rejects_password_protected_pdf`, `test_pdf_job_rejects_pdf_without_extractable_text`, `test_pdf_job_rejects_non_pdf_bytes`, `test_pdf_extraction_is_bounded_by_pages`, `test_pdf_extraction_is_bounded_by_characters` (cap counts separators; exact boundary cases), `test_pdf_bounds_are_sane_defaults`, `test_pdf_job_failure_is_recorded_not_swallowed` (queue persists `PdfExtractionError: …`) | Bounds: `PDF_MAX_PAGES = 100`, `PDF_MAX_CHARACTERS = 250_000` (module constants in `jobs.py`; `settings.py` untouched). Result reports `pages` / `total_pages` / `characters` / `truncated`. `FileNotFoundError` still propagates unchanged (infrastructure, not a user PDF problem). Packaging: `[pdf]` extra; `pypdf` also in `dev` (CI runs the fixture test); Dockerfile installs `.[pdf]` so the shipped image supports PDF uploads; README note. |
| D4 | "وقتی آماده شد به شما اطلاع می‌دهم": **implemented for real** — the in-process queue fires a fail-safe completion hook on every terminal transition; `TelegramJobNotifier` (`bot/job_notifications.py`) delivers the story PNG (`send_photo`) / reports PDF pages (+ truncation) / sends a user-facing failure reason to the originating chat. | DONE | `tests/integration/test_in_process_job_queue.py`: `test_completion_hook_receives_the_terminal_record`, `test_completion_hook_failure_is_logged_not_fatal` (broken hook ⇒ `job_completion_hook_failed`, outcome unchanged), `test_completion_hook_fires_for_resumed_and_unhandled_jobs`; `tests/unit/test_job_notifications.py` (8 tests, fake bot): PNG delivery, story failure, PDF success with/without truncation, failure mapping (`PdfExtractionError` message user-facing; `PdfSupportMissingError` ⇒ "روی این سرور فعال نیست", anything else ⇒ generic — no internal paths leak), fail-safe on Telegram errors, skipped without `chat_id`, ignores non-terminal/unknown jobs; `tests/unit/test_job_handlers.py`: payloads carry `chat_id` (+ `file_name`), `build_application` installs the notifier on `application.bot`, `test_story_job_result_is_delivered_to_the_chat` (real wiring: enqueue → render → `send_photo` with the PNG bytes) | Hook runs **after** the terminal row is durable, so a Telegram outage can never alter a job's stored outcome; it also fires for resumed and handler-less rows (`nexus jobs resume` reuses it — D1). Prerequisite fix 7514b74: the webhook runner now honours `post_stop`, and the drain moved there so the bot is still initialised while in-flight jobs finish and notify. The story reply text now says the PNG will be sent in this chat. |
| D1 | `resume_pending()`: **no auto-resume at boot**; recovery is the operator command `nexus jobs resume` (dry-run default, `--apply`, `--timeout`, `--notify/--no-notify`) guarded by an advisory store-ownership lock. | DONE | `tests/integration/test_in_process_job_queue.py`: `test_second_instance_cannot_resume_while_the_store_is_owned` (`JobStoreBusyError`; ownership picked up lazily once free), `test_enqueue_never_needs_ownership` (bot keeps serving, `job_store_owned_elsewhere` warning), `test_close_releases_ownership_even_without_tasks`, `test_list_unfinished_reports_rows_in_creation_order`; `tests/unit/test_jobs_resume.py` (11): dry run changes nothing, apply resumes + outcomes (`attempts == 2` for the interrupted row), the injected notifier is opened before and closed after the run, `bot.app.job_notifier_for_token` maps PTB errors to `NotifierUnavailableError` **before** anything is resumed / wraps an initialised client, refuses while another process owns the store (dry run still allowed), CliRunner: dry-run exit 0 with ids, `--apply` exit 1 when a job failed / "nothing to resume" exit 0, `--notify` composes the bot-layer notifier with the token, busy ⇒ exit 2 with "another process owns the job store (is the bot running?)", runbook contains the command | Lock = `flock(LOCK_EX\|LOCK_NB)` on `<sidecar>.lock`, taken lazily in `initialize()` (retried on every operation until acquired), released by `close()` even with no tasks. Only `resume_pending()` requires it (the one operation that reclassifies another process's `running` rows); enqueue/read are CAS-safe without it. No `fcntl` ⇒ assumed owner + warning once. `post_init` stays `None` (`test_application_wires_job_queue_and_handlers`). Import boundary respected: `jobs.py` / `cli.py` stay free of `telegram` (frozen legacy baseline); Telegram composition for the CLI is `bot/app.py::job_notifier_for_token`, injected into `jobs.run_resume(notifier=…)`. Docs: `docs/ops/JOBS_RUNBOOK.md`, README pointer, PORTS.md note. `settings.py` untouched. |
