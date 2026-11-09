# Benchmark revalidation — what the old numbers actually meant

This is the honest version of the story behind the benchmark numbers on PR #326.
The pre-fix run reported **12/12 scenarios passed**. The post-fix run also reports
**12/12**. Those two sentences describe different amounts of evidence, and this
document is here to make the difference impossible to miss.

| | pre-fix (`5fa6470`) | post-fix (`757dafc`) |
|---|---|---|
| scenario accuracy | 12/12 | 12/12 |
| scenarios that were self-comparisons | 1 (`extra_voice_identity`: distance 0.0) | 0 |
| fitted on data that was later reported as "held out" | **yes** (all three characters) | no (calibration ids only, recorded in `calibration.json`) |
| `voice` threshold | 1.3496 / 1.4496, fitted from **one take per speaker** (std ≈ 0) | **no threshold** — calibration split is inseparable, `EER = 1.0` |
| `face` threshold | 0.1149 / 0.2149 (p95/p05, contaminated normalisation) | 0.10 / 0.20 via declared `EER_FALLBACK` (EER 0.150), holdout FAR **0.6** / FRR **0.25** |
| published report digests that recompute | 0/12 (`processing_ms` was inside the digest) | 12/12 |
| corpus digest covers pixels | no (metadata only) | yes (per-frame `sha256` + `size_bytes`) |
| run artefacts | fixed paths, rewritten in place, non-atomic | `results/<run_id>/`, staged and published by `os.replace` |
| reproducibility of `results_digest` | not reproducible (paths + timings inside) | reproducible (declared scope; two runs, same digest) |

## 1. The scenario that measured nothing (F24)

`extra_voice_identity` used to pass `voice_samples(...)` to both the candidate and
the reference. Same array, distance 0.0, comfortably inside any band, `VERIFIED`.
The scenario was not evidence of voice verification; it was evidence that
`distance(x, x) == 0`.

It now compares **session `b`** of the speaker against **session `a`**. The corpus
gives each character a per-session variation (f0 ±1.2 %, rate, breathiness). That is
a real comparison — and it immediately exposed the next problem.

## 2. The voice threshold was a coin flip (F23 + the calibration policy)

The corpus's look-alike pair is 3 Hz apart (186 Hz vs 189 Hz) and a session shift
moves f0 by roughly 2 Hz. Measured directly:

| pair | distance (calibration statistics) |
|---|---|
| genuine (char 1, session a → b / c) | 1.40 / 1.42 |
| imposter (char 1 vs char 2) | **0.67** |
| imposter (char 1 vs char 3) | 17.27 |

Genuine distances are *larger* than the look-alike impostor distance. No threshold
separates them: the equal-error rate is **1.0**. The pre-fix code hid this by (a)
fitting statistics that included the character it then reported as held out and (b)
placing a band at the midpoint of the overlap, which classifies coin flips and makes
every downstream verdict arbitrary.

The fix is a declared policy (`ADR-0010`), applied uniformly to every dimension:

1. If the declared p95/p05 bands are ordered, use them — `mode: QUANTILE`.
2. If they cross but the distributions are still ordered on average and
   `EER ≤ 0.25`, place the boundaries at the equal-error point — `mode: EER_FALLBACK`
   (face lands here: EER 0.150).
3. Otherwise the measurement cannot separate the classes: **no threshold**, the
   dimension is `NOT_MEASURED`, and the numbers that prove it are published
   (voice: `EER 1.0`, `reason: CALIBRATION_INSEPARABLE`).

Voice therefore has no band, no holdout FAR/FRR (there is no operating point to
report), and `extra_voice_identity` reports `VERIFIED_WITH_LIMITATIONS` with voice
`NOT_MEASURED`. That is a weaker result than the old one and it is the true one.

## 3. What the held-out character says

Measured with statistics fitted without it (`NAGAR-CHAR-000003`):

| dimension | FAR @ accept | FRR @ accept | EER | note |
|---|---|---|---|---|
| body | 0.000 | 0.000 | 0.000 | 3 genuine / 3 impostor pairs |
| face | **0.600** | **0.250** | 0.325 | classical descriptor; look-alikes dominate the impostor set |
| motion | 0.000 | 0.000 | 0.000 | 3 / 2 pairs |
| wardrobe | 0.000 | 0.051 | 0.000 | 39 / 27 pairs |
| scene | `NOT_MEASURED` | — | — | held-out split has one class |
| voice | `NOT_MEASURED` | — | — | no threshold (see §2) |
| hair | `NOT_MEASURED` | — | — | advisory dimension, no band by design |

Face FAR 0.6 means: with this descriptor, on this corpus, six out of ten
impostor pairs would be accepted. The look-alike pair is the hard case and it is the
one the benchmark is *for*. The scenario suite still reaches `IDENTITY_DRIFT` for
the look-alike because other dimensions (body, wardrobe, canon) disagree, not
because the face band is good. Both statements are in `benchmark_results.json`; the
benchmark does not get to advertise the first without the second.

This is not tuned away. Tuning a threshold on the holdout would be the exact defect
F23 was about.

## 4. Reproducibility

* `results_digest` excludes `created_at`, `environment`, `run`, `evidence`,
  `results_digest*`, `scenarios[].processing_ms` and `corpus.root` — and the
  exclusion list is published in `results_digest_scope`. Two runs with the same
  `--run-id` in different directories produce the same digest (test:
  `test_reproducible_run_has_the_same_corpus_and_results_digest`).
* `CalibrationArtifact.digest` excludes `created_at` and publishes
  `digest_scope`; the split and the pruned probes are recorded inside the artefact.
* Reports bind the calibration digest, so a `calibration.json` change invalidates
  every report digest — verified by `test_f25b`.
* A run publishes into `results/<run_id>/` via a staging directory; a second run
  with an existing id raises `FileExistsError` instead of overwriting evidence.

## 5. Reproduce it

```bash
PYTHONPATH=. python -m experiments.character_capsule.benchmark \
    --work-dir experiments/character_capsule/.work --run-id final-757dafc
```

The numbers above are from that run: corpus `sha256:70e45e5e…`, calibration
`sha256:6a761bee…`, results `sha256:4a2ce940…`, evidence bundle
`sha256:e47339a5…` (`UNSIGNED`), 5.05 s wall, peak RSS 55.6 MB, 12/12 scenarios,
2 vCPU.
