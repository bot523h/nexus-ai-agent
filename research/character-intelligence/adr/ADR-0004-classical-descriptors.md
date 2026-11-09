# ADR-0004 — Classical descriptors in the prototype, embeddings behind a seam

- Status: **Accepted** (prototype); embedding upgrade **DEFERRED**
- Date: 2026-11-09

## Context

The best identity verifiers are learned embeddings (ArcFace/SFace and successors).
The best-known weights (InsightFace `buffalo_*`, `antelopev2`, InstantID checkpoints)
are **non-commercial research only**, which disqualifies them for commercial output.
This environment additionally has no torch/onnxruntime and no network access to model
weights.

## Decision

Build the verifier from deterministic, inspectable measurements:

* white balance (grey-world, bounded) → skin mask → **eye-anchored** square crop;
* face groups: high-pass chroma, per-cell normalised gradients, geometry, skin/eye
  tone; body: silhouette + profile; wardrobe: colour/bands/pattern; scene: backdrop;
* per-group z-scored RMS, averaged with fixed weights (geometry ×2, skin tone ×0.5),
  so dimensionality cannot silently outvote specificity.

Keep the seam narrow: `group_stats()` + `distance()`. An embedding strategy would
replace exactly those two functions.

## Consequences

* No licence risk, no model download, no GPU, ~4 ms per frame, fully reproducible.
* Accuracy is modest and *measured*: face FAR 0.600 on the held-out split. The
  document says so; it is not dressed up as production-grade.
* The eye-anchored crop was not a stylistic choice: bounding-box alignment made
  every feature drift with the key light, which the measurements showed.
* A future embedding upgrade must re-calibrate; the calibration artifact makes that
  a data change, not a code change.

## Alternatives rejected

| alternative | why rejected |
|---|---|
| InsightFace embeddings | licence excludes commercial output |
| SFace now | Apache-2.0, but training-data provenance undocumented; adopted as *candidate*, not baseline |
| CLIP/DINO similarity | documented to correlate poorly with human judgement; no weights offline |
| perceptual hashing | insensitive to the facial detail that identity depends on |

## Evidence

`experiments/character_capsule/vision.py`, `test_vision_measurement.py` (9 tests),
`VISUAL_CONTINUITY_EVALUATION_DESIGN.md` §8.
