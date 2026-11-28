# Studio Waves 4-6 — N=3 latency benchmark

- `HEAD` SHA: `5dc56a7882b3d5c648645fecf9560005b0fc91a1`
- Trials: **3**, iterations per trial: 5
- Measured at: 2026-11-28T04:41:05+00:00 on CPython 3.11.2

| # | Path | Trials | Median (ms) | p95 (ms) | Stddev (ms) | Min | Max |
|---|---|---:|---:|---:|---:|---:|---:|
| a | `authority_bridge_proof` | 3 | 1.026 | 2.157 | 0.781 | 0.659 | 2.157 |
| b | `revision_pinning_worker_verify` | 3 | 6.630 | 6.872 | 0.166 | 6.554 | 6.872 |
| c | `preview_readback_wysiwys` | 3 | 2.704 | 2.831 | 0.091 | 2.655 | 2.831 |
| d | `execution_outbox_full_cycle` | 3 | 25.851 | 32.872 | 4.115 | 25.644 | 32.872 |
| e | `disaster_recovery_backup_verify_restore` | 3 | 47.343 | 52.327 | 5.097 | 42.134 | 52.327 |
| f | `runtime_build_and_close` | 3 | 29.382 | 55.969 | 15.826 | 27.803 | 55.969 |

Each trial runs in its own fresh temporary directory, so a later trial is
never warmed by an earlier one.  `p95` is nearest-rank over the pooled
samples (with N=3 that is the maximum observation, which is the honest
answer: an interpolated p95 at N=3 is a number no measurement supports).

**Read the spread, not the point estimate.**  N=3: `max` is the largest of 3 samples and `p95` is by construction the same number, so neither is a tail estimate -- they are a sample bound.  The three numbers that carry information at this N are `min` (best case the path can reach), `median` and `stddev` (how reproducible it is).  A larger N would be needed to make any p95 claim, and this report does not make one.

`runtime_build_and_close` (path f) is deliberately separate from (a)-(e): those measure the modules *inside* the runtime, while (f) measures what every Studio HTTP route pays per request to assemble and tear the runtime down.  It is a fixed overhead on top of whichever path a route then uses, so it is not additive with the others and must not be summed with them.

## Per-trial samples (ms)

- `authority_bridge_proof` — authority_bridge_proof#trial1: 2.157, authority_bridge_proof#trial2: 1.026, authority_bridge_proof#trial3: 0.659
- `revision_pinning_worker_verify` — revision_pinning_worker_verify#trial1: 6.630, revision_pinning_worker_verify#trial2: 6.554, revision_pinning_worker_verify#trial3: 6.872
- `preview_readback_wysiwys` — preview_readback_wysiwys#trial1: 2.831, preview_readback_wysiwys#trial2: 2.704, preview_readback_wysiwys#trial3: 2.655
- `execution_outbox_full_cycle` — execution_outbox_full_cycle#trial1: 25.851, execution_outbox_full_cycle#trial2: 32.872, execution_outbox_full_cycle#trial3: 25.644
- `disaster_recovery_backup_verify_restore` — disaster_recovery_backup_verify_restore#trial1: 47.343, disaster_recovery_backup_verify_restore#trial2: 52.327, disaster_recovery_backup_verify_restore#trial3: 42.134
- `runtime_build_and_close` — runtime_build_and_close#trial1: 27.803, runtime_build_and_close#trial2: 29.382, runtime_build_and_close#trial3: 55.969

**Overall: PASS** — every path measured in every trial.
