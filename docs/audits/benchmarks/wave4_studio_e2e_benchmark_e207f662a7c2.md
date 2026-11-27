# Studio Waves 4-6 — N=3 latency benchmark

- `HEAD` SHA: `e207f662a7c2dcf2993ba4bcfc9b30f73a9686d6-dirty` (worktree dirty — numbers describe the working tree)
- Trials: **3**, iterations per trial: 5
- Measured at: 2026-11-27T13:23:52+00:00 on CPython 3.11.2

| # | Path | Trials | Median (ms) | p95 (ms) | Stddev (ms) | Min | Max |
|---|---|---:|---:|---:|---:|---:|---:|
| a | `authority_bridge_proof` | 3 | 0.524 | 1.491 | 0.570 | 0.483 | 1.491 |
| b | `revision_pinning_worker_verify` | 3 | 5.964 | 6.038 | 0.164 | 5.724 | 6.038 |
| c | `preview_readback_wysiwys` | 3 | 1.859 | 2.000 | 0.082 | 1.855 | 2.000 |
| d | `execution_outbox_full_cycle` | 3 | 24.261 | 25.080 | 0.811 | 23.457 | 25.080 |
| e | `disaster_recovery_backup_verify_restore` | 3 | 32.039 | 34.742 | 1.562 | 32.036 | 34.742 |

Each trial runs in its own fresh temporary directory, so a later trial is
never warmed by an earlier one.  `p95` is nearest-rank over the pooled
samples (with N=3 that is the maximum observation, which is the honest
answer: an interpolated p95 at N=3 is a number no measurement supports).

## Per-trial samples (ms)

- `authority_bridge_proof` — authority_bridge_proof#trial1: 1.491, authority_bridge_proof#trial2: 0.483, authority_bridge_proof#trial3: 0.524
- `revision_pinning_worker_verify` — revision_pinning_worker_verify#trial1: 6.038, revision_pinning_worker_verify#trial2: 5.724, revision_pinning_worker_verify#trial3: 5.964
- `preview_readback_wysiwys` — preview_readback_wysiwys#trial1: 2.000, preview_readback_wysiwys#trial2: 1.859, preview_readback_wysiwys#trial3: 1.855
- `execution_outbox_full_cycle` — execution_outbox_full_cycle#trial1: 23.457, execution_outbox_full_cycle#trial2: 25.080, execution_outbox_full_cycle#trial3: 24.261
- `disaster_recovery_backup_verify_restore` — disaster_recovery_backup_verify_restore#trial1: 32.036, disaster_recovery_backup_verify_restore#trial2: 34.742, disaster_recovery_backup_verify_restore#trial3: 32.039

**Overall: PASS** — every path measured in every trial.
