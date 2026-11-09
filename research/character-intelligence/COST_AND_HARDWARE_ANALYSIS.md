# Cost and Hardware Analysis

Status: **VERIFIED for CPU measurements** (this workspace, 2 vCPU / ~3 GB);
**NOT_MEASURED for anything GPU** (no GPU present).

## Measured on this machine

| operation | wall time | memory |
|---|---|---|
| generate corpus (63 frames, 384×768) + write PNGs | ~1.2 s | < 100 MB |
| `observe()` one frame (full descriptor) | ~3.5–4.5 ms | negligible |
| one continuity evaluation (7 references + 1 candidate) | **249 ms mean** (min 79 ms, max 324 ms) | 57.3 MB peak RSS |
| full benchmark run (corpus + calibration + 12 scenarios + evidence) | **6.84 s** | 57.3 MB |
| full test suite `tests/character_capsule` (58 tests) | 28.8 s | < 120 MB |

Per-shot cost is dominated by re-observing the gallery: seven references × ~4 ms,
plus the candidate, plus the pure-numpy group distances. Caching observations per
gallery would cut the mean to well under 20 ms; that optimisation is not
implemented because the prototype optimises for inspectability, not throughput.

## Scaling estimate (arithmetic on measured numbers, not a benchmark)

| load | estimated CPU time | basis |
|---|---|---|
| 1 000 shots, 7-reference gallery, no cache | ~250 s single-core | 249 ms × 1 000 |
| 1 000 shots with observation cache | ~15–25 s | 6 × 4 ms per shot + overhead |
| 10 000 shots with cache | ~3–4 min single-core | linear |

These are **extrapolations from a measured per-shot cost**, not measured at scale;
the document says so instead of presenting a throughput figure as a benchmark.

## What a GPU would change (and what it would not)

GPU matters for **generation** (InstantID/PuLID/LoRA inference: seconds per image on
a 24 GB card, vendor claim) and for **learned embeddings** (ArcFace/SFace: 10–30 ms
per face on CPU, much less on GPU). It does **not** matter much for this verifier,
because the descriptor is numpy geometry and chroma statistics.

Consequence: **the verifier fits the cheap tier**; the expensive tier is generation,
which this mission deliberately does not build.

## Cost of the licence-clean path

| component | licence | cost |
|---|---|---|
| YuNet + SFace (OpenCV Zoo) | MIT / Apache-2.0 | free, CPU-friendly |
| embedding upgrade | — | requires GPU only for training, not inference |
| C2PA signing | Apache-2.0/MIT | free; needs a certificate chain (cost is process, not software) |
| per-character LoRA | base-model dependent | training time; **not** licence-free with Flux dev |

## Storage

| object | size measured here |
|---|---|
| one frame (384×768 PNG) | ~15–25 KB (typical synthetic frame) |
| corpus (63 frames) | 1.3 MB including results and bundle |
| capsule JSON | ~6 KB |
| calibration artifact | ~15 KB |
| continuity report | ~4 KB |
| evidence bundle | ~4 KB |

A season of 5 000 shots at ~4 KB of report each is ~20 MB — the reports are small;
the frames dominate.

## Environment limits that shaped the design

* **No ffmpeg**: video is represented as frame sequences + a feature vector, and the
  prototype writes/reads PNG and WAV with its own codecs.
* **No torch/onnxruntime**: no learned model was used, so no accuracy claim about
  learned models can be made here (`NOT_MEASURED`).
* **2 vCPU**: the benchmark was designed to finish in seconds so it can run on every
  PR; a GPU-class corpus would not fit this budget.

## Recommendation for a real deployment

1. Keep the verifier on CPU; cache gallery observations (biggest win, ~10×).
2. Put generation behind a GPU service; never let the verifier depend on it.
3. Budget the evidence chain (4 KB per shot) rather than the JSON reports.
4. Measure on photographic data before trusting any threshold from this corpus.
