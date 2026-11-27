# Studio Waves 4-6 — N=3 latency benchmark

- `HEAD` SHA: `7134db5b0293f9c6a8d68e45e36a3d7a46e7642f`
- Trials: **3**, iterations per trial: 5
- Measured at: 2026-11-27T19:22:23+00:00 on CPython 3.11.2

| # | Path | Trials | Median (ms) | p95 (ms) | Stddev (ms) | Min | Max |
|---|---|---:|---:|---:|---:|---:|---:|
| a | `authority_bridge_proof` | 3 | 0.523 | 1.258 | 0.425 | 0.519 | 1.258 |
| b | `revision_pinning_worker_verify` | 3 | 5.735 | 5.740 | 0.132 | 5.510 | 5.740 |
| c | `preview_readback_wysiwys` | 3 | 1.907 | 2.433 | 0.308 | 1.891 | 2.433 |
| d | `execution_outbox_full_cycle` | 3 | 25.953 | 26.815 | 3.302 | 20.713 | 26.815 |
| e | `disaster_recovery_backup_verify_restore` | 3 | 36.139 | 37.649 | 0.976 | 35.822 | 37.649 |

Each trial runs in its own fresh temporary directory, so a later trial is
never warmed by an earlier one.  `p95` is nearest-rank over the pooled
samples (with N=3 that is the maximum observation, which is the honest
answer: an interpolated p95 at N=3 is a number no measurement supports).

## Per-trial samples (ms)

- `authority_bridge_proof` — authority_bridge_proof#trial1: 1.258, authority_bridge_proof#trial2: 0.523, authority_bridge_proof#trial3: 0.519
- `revision_pinning_worker_verify` — revision_pinning_worker_verify#trial1: 5.735, revision_pinning_worker_verify#trial2: 5.740, revision_pinning_worker_verify#trial3: 5.510
- `preview_readback_wysiwys` — preview_readback_wysiwys#trial1: 1.891, preview_readback_wysiwys#trial2: 1.907, preview_readback_wysiwys#trial3: 2.433
- `execution_outbox_full_cycle` — execution_outbox_full_cycle#trial1: 20.713, execution_outbox_full_cycle#trial2: 26.815, execution_outbox_full_cycle#trial3: 25.953
- `disaster_recovery_backup_verify_restore` — disaster_recovery_backup_verify_restore#trial1: 37.649, disaster_recovery_backup_verify_restore#trial2: 35.822, disaster_recovery_backup_verify_restore#trial3: 36.139

**Overall: PASS** — every path measured in every trial.
