# ADR-0010 — When a dimension has no defensible threshold, say so

- Status: **Accepted** (implemented and tested at `757dafc`)
- Date: 2026-11-09
- Closes: F23, F24, F25, F27 (and the revalidation in `BENCHMARK_REVALIDATION.md`)

## Context

ADR-0003 fixed the idea that thresholds must be measured, not hand-written. Measuring
them raised a harder question: what do you do when the measured distributions do not
separate? The old code answered by placing both boundaries at the midpoint of the
overlap — a band that classifies coin flips — and then reported 12/12.

Three concrete symptoms: statistics were fitted on the character that was later
reported as held out (F23); a voice scenario compared a recording with itself (F24);
and the published reports failed their own digests because timing was inside them
(F25). The common cause is the same: a number was produced where no measurement
existed.

## Decision

A calibration fit has exactly three declared outcomes per dimension, recorded in
`calibration.json` as `band_fit.mode`:

1. **`QUANTILE`** — the p95 genuine / p05 impostor bands are ordered. Use them.
2. **`EER_FALLBACK`** — the bands cross, but the distributions are ordered on average
   and the equal-error rate is at most **0.25**. Put the boundaries symmetrically
   around the equal-error point and label the fit. (Face lands here: EER 0.150,
   boundaries 0.10 / 0.20.)
3. **`NOT_MEASURED`** — otherwise. **No threshold is emitted.** The dimension reports
   `NOT_MEASURED` with the numbers that prove the inseparability (voice: EER 1.0,
   `reason: CALIBRATION_INSEPARABLE`), and no holdout operating point is published
   because there is none.

Supporting rules:

* Fitted statistics come from the calibration ids only; the split and the pruned
  probes are recorded inside the artefact so the claim is checkable (`split`,
  `pruning`).
* Genuine pairs are always *different recordings* (`session a` vs `b`/`c`), never the
  same array passed twice.
* Timing (`processing_ms`), wall-clock `created_at` and absolute paths are outside every
  digest, and each digest publishes its exclusion list.
* A run publishes into `results/<run_id>/` through a staging directory; a second run
  with the same id is refused.

## Consequences

* Voice is `NOT_MEASURED` on this corpus and `extra_voice_identity` therefore reports
  `VERIFIED_WITH_LIMITATIONS` rather than a verified voice identity. That is a weaker
  claim than the one the old benchmark made, and it is the true one.
* Face is calibrated through a labelled fallback; the held-out FAR (0.6) is published
  next to it rather than hidden by a better-looking threshold.
* The scenario count did not change (12/12), but three of the twelve now rest on
  measurements that can be reproduced and audited instead of a single midpoint band.

## Alternatives considered

* **Keep the midpoint band.** It is what produced a passing benchmark over an
  inseparable feature; the pass was the defect.
* **Tune the threshold on the holdout.** Directly contradictory to ADR-0003 and F23.
* **Drop voice from the benchmark.** Hiding a dimension that cannot be measured is less
  informative than reporting that it cannot be measured.
