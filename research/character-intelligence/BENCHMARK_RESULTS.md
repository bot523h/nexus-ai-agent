# Benchmark Results — run `final-757dafc`

Command executed (commit `757dafc`, 2 vCPU, Python 3.11.2):

```bash
PYTHONPATH=. python -m experiments.character_capsule.benchmark \
    --work-dir experiments/character_capsule/.work --run-id final-757dafc
```

A run publishes into its own directory, staged and swapped in with a single rename:

```
experiments/character_capsule/.work/results/final-757dafc/
├── BENCHMARK.md              # table version of this document (generated)
├── benchmark_results.json    # machine-readable results + environment + digests
├── calibration.json          # thresholds, statistics, split, per-dimension metrics
├── continuity_reports.json   # one full report per scenario (each with its digest)
├── corpus_manifest.json      # 63 frame descriptors incl. per-frame sha256
└── evidence_bundle.json      # content-addressed evidence (UNSIGNED here)
```

The directory is git-ignored; regenerate it with the command above. Reusing an
existing `--run-id` raises `FileExistsError` rather than overwriting evidence.

| digest | value |
|---|---|
| corpus | `sha256:70e45e5ea91f4bd4fd706b9c32fe462b210752b1180931800e2a7cceaf13c8e4` |
| calibration | `sha256:6a761bee0594760d9a27ba5489b9d62f97bc498dd0ec062ce9d636a6bb30ff36` |
| results | `sha256:4a2ce9400b755c0672c01a3aeacd6b76fea60c6d13a90865ada724bcca24a91f` |
| evidence bundle | `sha256:e47339a51229ba4151502c05adf7ef0fa2c90a6598dd8a04387129b7f4f9f788` |

`results_digest` is reproducible: it excludes `created_at`, `environment`, `run`,
`evidence`, `scenarios[].processing_ms` and `corpus.root`, and publishes that
exclusion list in `results_digest_scope`.

## Scenarios: 12 / 12 as declared

| # | scenario | actual verdict |
|---|---|---|
| 1 | same character, same camera | `VERIFIED_WITH_LIMITATIONS` |
| 2 | same character, 32° yaw | `VERIFIED_WITH_LIMITATIONS` |
| 3 | same character, 3400 K key @1.18 | `INSUFFICIENT_EVIDENCE` (quality-gated) |
| 4 | same character, smiling | `VERIFIED_WITH_LIMITATIONS` |
| 5 | same character, second registered outfit | `VERIFIED_WITH_LIMITATIONS` |
| 6 | same character, different set + canon scar | `REVIEW_REQUIRED` (canon marker) |
| 7 | same character, full-body pose | `REVIEW_REQUIRED` (canon effect not asserted) |
| 8 | same character, gait sequence | `REVIEW_REQUIRED` (canon effect not asserted) |
| 9 | **different character, look-alike** | `IDENTITY_DRIFT` |
| 10 | **canon conflict** (`scar=none` after EVT-001) | `CANON_CONFLICT` |
| 11 | extra: unplanned outfit at shot 0 | `WARDROBE_DRIFT` |
| 12 | extra: voice identity, **session b vs session a** | `VERIFIED_WITH_LIMITATIONS` (voice `NOT_MEASURED`, see below) |

Total wall time 5.05 s; mean scenario 196 ms; peak RSS 55.6 MB; GPU `NOT_MEASURED`.

Scenario 12 is the one that changed meaning: it used to compare a recording with
itself (`distance 0.0`) and pass. It now compares two sessions, and the voice
dimension reports `NOT_MEASURED` because this corpus cannot separate the look-alike
pair by voice. `BENCHMARK_REVALIDATION.md` explains that in full.

## Fitted bands (calibration characters only) and held-out behaviour

Split: `calibration = [NAGAR-CHAR-000001, NAGAR-CHAR-000002]`,
`holdout = [NAGAR-CHAR-000003]`, overlap `[]`. The split and the pruned probes are
recorded inside `calibration.json`.

| dimension | fit mode | accept ≤ | reject ≥ | calibration n (gen/impostor) | calibration EER | held-out EER | FAR @ accept | FRR @ accept |
|---|---|---|---|---|---|---|---|---|
| body | QUANTILE | 0.1000 | 0.6574 | 6 / 6 | 0.000 | 0.000 | 0.000 | 0.000 |
| face | **EER_FALLBACK** | 0.1000 | 0.2000 | 7 / 19 | 0.150 | **0.325** | **0.600** | **0.250** |
| motion | QUANTILE | 1.8026 | 2.1078 | 6 / 1 | 0.000 | 0.000 | 0.000 | 0.000 |
| scene | QUANTILE | 0.0604 | 0.1604 | 1314 / 10 | 0.000 | `NOT_MEASURED` (one class held out) | — | — |
| wardrobe | QUANTILE | 0.9209 | 2.3016 | 78 / 54 | 0.000 | 0.000 | 0.000 | 0.051 |
| voice | `NOT_MEASURED` | — | — | 6 / 1 | **1.000** | no operating point | — | — |
| hair | `NOT_MEASURED` | — | — | — | advisory | — | — | — |

The face row is the important one: the p95/p05 bands crossed, the equal-error point
was used instead (declared as `EER_FALLBACK`), and the held-out character then shows
FAR 0.6. The benchmark reports both numbers together.

`voice` has no threshold because the calibration split cannot separate genuine from
impostor pairs at all (EER 1.0: the look-alike pair is 3 Hz apart, a session shift is
~2 Hz). The code no longer invents a midpoint band for that case.

## Reading this honestly

* 12/12 means "the twelve scenarios reach the verdicts written down for them".
  The declared verdicts are part of the artefact (`scenarios[].expected`), so a
  change of expectation is visible in the diff, not hidden in prose.
* `INSUFFICIENT_EVIDENCE`, `REVIEW_REQUIRED` and `NOT_MEASURED` are results, not
  failures. A verifier that never says "I cannot tell" is not a verifier.
* Face FAR 0.6 / FRR 0.25 on the held-out character is a limitation of the classical
  descriptor on a deliberately hard look-alike corpus. It is reported, not tuned
  away; tuning on the holdout is the defect F23 was about.
* The corpus is procedural and the subjects are synthetic. These thresholds do not
  transfer to photographs; `not_measured["photographic_transfer"]` says so.
