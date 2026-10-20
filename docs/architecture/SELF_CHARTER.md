# Self-Charter and Self-Ledger — Living Core Phase 1

**Status:** Implemented (Phase 1 of [LIVING_INTELLIGENCE_ROADMAP.md](LIVING_INTELLIGENCE_ROADMAP.md)); the roadmap itself still awaits owner acceptance.
**Code:** `src/nexus_ai_agent/selfhood/` · **Guards:** `tests/architecture/test_selfhood_boundary.py`, `tests/unit/test_selfhood_ledger.py` · **Law:** R21 in [MODULE_MAP.md](MODULE_MAP.md)

## What this is — and is not

The constitutional zone of the Living Core: an identity contract the system cannot edit (the **charter**) and an append-only, hash-chained **Self-Ledger** that anchors it and records commitments. It makes identity and promises *durable and tamper-evident*. It makes **no claim of consciousness or feeling**; that is itself charter principle one.

## Charter (v1)

| Id | Principle |
|---|---|
| `honest-claims` | Never claim consciousness or feelings; report only what is measured. |
| `claim-vs-measured` | Every claim of success is checked against an independent measurement. |
| `charter-immutable` | Learners and agents cannot write the charter; only a named human can. |
| `fail-closed` | On an integrity doubt about self-state, stop rather than guess. |
| `human-gate` | Irreversible or externally visible actions require human approval. |

Setpoints (`claim_vs_measured_gap_max`, `ledger_verify_interval_s`) are data for the later vital-signs phase; Phase 2a reads them (see below); a setpoint named `<vital>_max` / `<vital>_min` bounds the vital from above / below.

## Mechanics

1. **Ledger.** Each record commits to `prev_hash` and to its exact stored bytes under the domain `nexus.self-ledger.record.v1` (same canonical-JSON and genesis conventions as the causal journal, a separate sidecar and a separate domain). There is no update or delete API.
2. **Boot (`boot_self`).** Verify the whole chain first; any break raises `SelfIntegrityError` with the first broken `seq`. Then compare the running charter digest with the last anchored one: first boot anchors it; a difference raises `CharterDriftError`.
3. **Amendment (`selfhood.amend.adopt_amendment`).** Requires a named human approver and a reason, refuses an unverifiable ledger, and appends a new `charter_adopted` record. Nothing under `src/` may import it.
4. **Commitments.** Opened and closed through ledger entries; the open set is a projection, so a restart restores it exactly.

## Honest limits

- A bare hash chain cannot see loss of the **newest** records. `verify(expected_head=...)` and `boot_self(expected_head=...)` accept an externally held head hash; wiring a periodic off-host anchor is a later phase.
- Whoever can rewrite the whole SQLite file *and* recompute every hash can forge history; the defence is the external anchor plus the human-only amendment door, not the file.
- Phase 1 is not yet wired into the bot's startup; it is a verified building block.

## Acceptance evidence

| Criterion (roadmap Phase 1) | Test |
|---|---|
| Tampering and deletion are detected at the exact `seq` | `test_content_tampering_is_pinpointed`, `test_deleting_a_record_is_detected` |
| Tail loss is detected given an anchor | `test_anchored_head_detects_tail_truncation` |
| Tampered ledger fails closed at boot | `test_tampered_ledger_fails_closed_at_boot` |
| Unannounced charter change fails closed; human amendment works | `test_charter_drift_fails_closed_without_human_amendment`, `test_amendment_requires_a_named_human_and_then_boots` |
| After a hard kill, all charter entries and open commitments return | `test_crash_restores_every_charter_entry_and_open_commitment` |
| No code path in `src/` writes the charter | `test_nothing_in_src_can_reach_the_amendment_door`, `test_ledger_has_no_mutating_sql_besides_insert` |
| Check-then-append commitments cannot race (threads or processes) | `test_concurrent_open_commitment_admits_exactly_one_writer`, `test_two_processes_cannot_both_open_the_same_commitment` |
| `created_at` is covered by verification | `test_created_at_tampering_is_detected` |

## Phase 2a — Vital signs (`selfhood/vitals.py`)

- **Setpoints come only from the charter.** `VitalSigns` refuses a probe that has no charter setpoint, so a learner cannot loosen the threshold it is judged by.
- **Each sample appends a `vital_reading`; an out-of-range one also appends a `vital_deviation`.** A probe that raises or returns a non-finite number is recorded as a deviation (value `null`) — an unreadable vital is never treated as healthy.
- **Restart-safe.** `latest_readings(ledger)` rebuilds the newest reading per vital from the chain.
- **Scheduler.** `VitalsScheduler(period_s)` samples off the event loop thread and has a kill switch (`stop`). Single event loop only.
- **`ProviderHealthTracker`** is a sliding window; its `fallback_ratio` is the first real probe.

Not yet done (Phase 2 remainder, deliberately separate PRs): wiring the scheduler and probes into bot startup, post-update verification and rollback for the updater, and deciding `auto_apply_at` (implement with a test or remove).

| Criterion | Test |
|---|---|
| Vitals survive restart | `test_vitals_survive_restart` |
| Simulated provider outage reaches the ledger within one period | `test_simulated_provider_outage_lands_in_the_ledger_within_one_period` |
| Broken or NaN probe is a deviation | `test_a_failing_probe_is_itself_a_deviation_never_a_crash`, `test_nan_reading_is_a_deviation_not_a_pass` |
| Setpoints are charter-only | `test_setpoints_come_from_the_charter_not_from_the_caller`, `test_probe_without_a_charter_setpoint_is_refused` |
| Kill switch | `test_kill_switch_stops_sampling` |
