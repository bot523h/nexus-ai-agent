# ADR-0003 — Thresholds are fitted per corpus, never hardcoded

- Status: **Accepted** (implemented; the resulting numbers are modest and reported)
- Date: 2026-11-09

## Context

A threshold is a property of a descriptor *and* a dataset. Copying one from another
system is how a verifier ends up confidently wrong: the same 0.4 boundary can be
generous on one corpus and impossible on another. This mission also has no licensed
embedding model, so the descriptor's behaviour is unknown until measured.

## Decision

1. Fit thresholds from labelled distances with a three-band rule per dimension:
   `accept` = p95 of genuine, `reject` = p05 of impostor.
2. Overlap is **reported**, not hidden: the boundaries collapse to the midpoint and
   the raw values are recorded in the calibration artifact.
3. A **declared** acceptance floor (`MIN_ACCEPT = 0.10`) and minimum band
   (`MIN_BAND = 0.10`) prevent degenerate zero-width bands when repeats measure
   exactly zero — a declared measurement resolution, not a tuned fudge.
4. Calibrate on two characters and **evaluate on a third**, never used for fitting.
5. Quality pruned frames and canon-marker frames are excluded from identity
   calibration, and the exclusion is listed in the artifact.

## Consequences

* The artifact is data, not code: it can be diffed, reproduced and (later) signed.
* The numbers are unflattering where the descriptor is weak — e.g. face FAR 0.600 /
  FRR 0.250 on the held-out character. That is the point.
* Genuine distances that measure exactly zero make the band collapse to the declared
  floor; on this synthetic corpus that is honest, but it means the band is *not* a
  transferable tolerance.

## Alternatives rejected

| alternative | why rejected |
|---|---|
| one global threshold for all dimensions | face and wardrobe have different scales and different failure costs |
| fixed thresholds from literature | literature thresholds belong to other descriptors and corpora |
| tuning until the benchmark passes | would make the benchmark decorative |

## Evidence

`calibration.json` of the executed run; `BENCHMARK_RESULTS.md`;
`continuity.build_calibration`; `benchmark._pair_scores_all` (gallery-min geometry,
which is what the evaluator actually does).
