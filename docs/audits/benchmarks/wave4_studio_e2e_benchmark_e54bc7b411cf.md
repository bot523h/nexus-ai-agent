# Studio Waves 4-6 — N=3 latency benchmark

- `HEAD` SHA: `e54bc7b411cf1ffc11c19925611201a7838861cc`
- Trials: **3**, iterations per trial: 5
- Measured at: 2026-11-27T13:55:40+00:00 on CPython 3.11.2

| # | Path | Trials | Median (ms) | p95 (ms) | Stddev (ms) | Min | Max |
|---|---|---:|---:|---:|---:|---:|---:|
| a | `authority_bridge_proof` | 3 | 0.662 | 1.317 | 0.437 | 0.488 | 1.317 |
| b | `revision_pinning_worker_verify` | 3 | 4.793 | 5.478 | 0.449 | 4.634 | 5.478 |
| c | `preview_readback_wysiwys` | 3 | 1.904 | 2.121 | 0.144 | 1.849 | 2.121 |
| d | `execution_outbox_full_cycle` | 3 | 19.382 | 20.855 | 0.976 | 19.009 | 20.855 |
| e | `disaster_recovery_backup_verify_restore` | 3 | 30.697 | 33.933 | 2.178 | 29.791 | 33.933 |

Each trial runs in its own fresh temporary directory, so a later trial is
never warmed by an earlier one.  `p95` is nearest-rank over the pooled
samples (with N=3 that is the maximum observation, which is the honest
answer: an interpolated p95 at N=3 is a number no measurement supports).

## Per-trial samples (ms)

- `authority_bridge_proof` — authority_bridge_proof#trial1: 1.317, authority_bridge_proof#trial2: 0.488, authority_bridge_proof#trial3: 0.662
- `revision_pinning_worker_verify` — revision_pinning_worker_verify#trial1: 4.793, revision_pinning_worker_verify#trial2: 4.634, revision_pinning_worker_verify#trial3: 5.478
- `preview_readback_wysiwys` — preview_readback_wysiwys#trial1: 1.904, preview_readback_wysiwys#trial2: 1.849, preview_readback_wysiwys#trial3: 2.121
- `execution_outbox_full_cycle` — execution_outbox_full_cycle#trial1: 19.009, execution_outbox_full_cycle#trial2: 20.855, execution_outbox_full_cycle#trial3: 19.382
- `disaster_recovery_backup_verify_restore` — disaster_recovery_backup_verify_restore#trial1: 29.791, disaster_recovery_backup_verify_restore#trial2: 33.933, disaster_recovery_backup_verify_restore#trial3: 30.697

**Overall: PASS** — every path measured in every trial.
