# Studio Waves 4-6 — N=3 latency benchmark

- `HEAD` SHA: `ec1ad5ead2d8b0c5b0b64144ab5c5fbe662e46a6`
- Trials: **3**, iterations per trial: 5
- Measured at: 2026-11-27T14:52:29+00:00 on CPython 3.11.2

| # | Path | Trials | Median (ms) | p95 (ms) | Stddev (ms) | Min | Max |
|---|---|---:|---:|---:|---:|---:|---:|
| a | `authority_bridge_proof` | 3 | 0.437 | 1.029 | 0.344 | 0.429 | 1.029 |
| b | `revision_pinning_worker_verify` | 3 | 4.592 | 7.145 | 1.477 | 4.582 | 7.145 |
| c | `preview_readback_wysiwys` | 3 | 1.691 | 1.733 | 0.052 | 1.629 | 1.733 |
| d | `execution_outbox_full_cycle` | 3 | 20.084 | 23.079 | 2.988 | 17.102 | 23.079 |
| e | `disaster_recovery_backup_verify_restore` | 3 | 29.202 | 33.208 | 2.823 | 27.759 | 33.208 |

Each trial runs in its own fresh temporary directory, so a later trial is
never warmed by an earlier one.  `p95` is nearest-rank over the pooled
samples (with N=3 that is the maximum observation, which is the honest
answer: an interpolated p95 at N=3 is a number no measurement supports).

## Per-trial samples (ms)

- `authority_bridge_proof` — authority_bridge_proof#trial1: 1.029, authority_bridge_proof#trial2: 0.437, authority_bridge_proof#trial3: 0.429
- `revision_pinning_worker_verify` — revision_pinning_worker_verify#trial1: 4.592, revision_pinning_worker_verify#trial2: 7.145, revision_pinning_worker_verify#trial3: 4.582
- `preview_readback_wysiwys` — preview_readback_wysiwys#trial1: 1.733, preview_readback_wysiwys#trial2: 1.629, preview_readback_wysiwys#trial3: 1.691
- `execution_outbox_full_cycle` — execution_outbox_full_cycle#trial1: 17.102, execution_outbox_full_cycle#trial2: 23.079, execution_outbox_full_cycle#trial3: 20.084
- `disaster_recovery_backup_verify_restore` — disaster_recovery_backup_verify_restore#trial1: 27.759, disaster_recovery_backup_verify_restore#trial2: 33.208, disaster_recovery_backup_verify_restore#trial3: 29.202

**Overall: PASS** — every path measured in every trial.
