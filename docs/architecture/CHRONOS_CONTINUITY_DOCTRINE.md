# Chronos Real-Media Continuity Doctrine (Waves 11–12)

## Scope and evidence boundary

This doctrine specifies the continuity proof performed by `real_media_continuity.py` and records the measured engineering evidence from the Wave 11–12 test run. The proof processes NumPy RGB frame arrays, metric 3D landmark arrays, and mono PCM arrays; writes PNG/WAV (and optional FFmpeg mux) files under a contained workspace; hashes the files; and verifies portable RFC 6962 inclusion bundles.

The acceptance run uses reproducibly generated test buffers and a separate test with caller-supplied buffers. It is evidence for these measured buffers and persisted artifacts only. It is **not** validation of production footage, human identity, a production codec pipeline, or perceptual quality in uncontrolled media.

## Continuity invariants

### Color and identity

- CIE Lab conversion is normalized to CIE D65 XYZ `(0.95047, 1.0, 1.08883)`.
- The Bradford cone-response matrix is `[[0.8951, 0.2664, -0.1614], [-0.7502, 1.7135, 0.0367], [0.0389, -0.0685, 1.0296]]`. Adapting an identical source and destination white takes the exact identity path; the test bounds the matrix error at `1e-15`.
- Injected illuminant/exposure drift is applied to every pixel in the `F×H×W×3` linear-RGB buffers. Restoration inverse-maps the known drift, then closes the calibrated pixel patch to its Show-Bible Bradford cone response. Per-frame measurements use the worst pixel's CIEDE2000 difference in that patch, not a spatial mean.
- Cranial landmarks are fit in metric 3D with a weighted similarity transform. The restoration receipt includes the fitted rotation/scale, before/after baseline drift, and gross outlier count; identified gross outliers are restored to the canonical landmark.

### Rational PCM alignment

The canonical clock is the exact rational rate `24000/1001` frames/s at `48,000` samples/s. The horizon is computed using integer arithmetic, and Bresenham remainder carry assigns exact integer sample counts across shot boundaries. The deliberately unanchored comparison uses a nominal `23.98` fps float clock to expose cumulative sample-count drift. WAV receipts are verified on disk for channel count, sample rate, and exact frame count.

### Durable receipts and Merkle verification

Every shot receipt records the SHA-256 digest of its persisted PNG frames and WAV, plus the mux digest when FFmpeg is available. The canonical receipt bytes are UTF-8 JSON with sorted keys, compact separators, and non-finite values rejected. The Merkle rules are:

- leaf: `SHA256(0x00 || canonical_receipt_bytes)`
- internal node: `SHA256(0x01 || left_digest || right_digest)`

Bundles carry the receipt, root, leaf digest, tree size/index, and directional sibling hashes. Verification rejects non-canonical path directions, wrong path lengths, malformed framing/digests, and incorrect roots. It does not trust a caller-supplied `sibling_is_left` direction without checking it against the RFC 6962 tree shape.

When FFmpeg is present, Chronos writes an FFV1/PCM NUT mux using bitexact flags and stripped metadata; NUT avoids the randomized Matroska UIDs observed during implementation and keeps same-environment output hashes reproducible. FFmpeg remains optional; PNG/WAV receipts and Merkle proofs do not depend on it.

## 4×4 season evidence

Command-equivalent test workload: 4 episodes × 4 shots, 12 frames per shot, 16×16 pixels per frame, 48 kHz mono PCM, and 24,000/1001 fps.

| Evidence | Measured result | Acceptance |
| --- | ---: | ---: |
| Episodes / shots | 4 / 16 | 4 / 16 |
| RGB frames persisted | 192 | non-empty, PNG-decodable files with matching SHA-256 receipts |
| Mono PCM horizon | 384,384 samples | exact rational horizon, 0 anchored sample drift |
| Naive 23.98 fps count drift | 64 samples | at least 8 samples |
| Minimum unanchored per-frame worst-pixel ΔE00 over all shots | 2.7594628 | greater than 2.5 on every shot |
| Maximum restored per-frame worst-pixel ΔE00 over all shots | 7.43×10⁻¹⁴ | less than 0.25 on every shot |
| Maximum injected cranial baseline scale drift | 0.024 | corruption is present |
| Maximum restored cranial baseline scale drift | 6.94×10⁻¹⁶ | less than 1×10⁻⁹ |
| Shot inclusion bundles | 16 / 16 verified | every proof resolves to the manifest root |
| Optional FFmpeg outputs | 16 NUT files in the measured environment | each mux digest is in its shot receipt |
| Season Merkle root (measured environment) | `d02106536689a01c02ecaad4af4e268b4eaed7476b23336159f0ce8d3963912f` | reproducible across the N=3 same-environment benchmark |

The artifacts are written under each run's `media/episode-NNNN/shot-NNNN/` directory, with `media/season-continuity-manifest.json` holding the season receipts and portable inclusion bundles. A separate caller-buffer test exercises PCM pad reporting against a 3,990-sample input and verifies the stored 4,004-sample result.

## N=3 runtime benchmark

The benchmark runs the complete 4×4 acceptance workload three times, including on-disk PNG/WAV writes, optional NUT muxing, SHA-256 receipts, and all 16 Merkle proofs per run. Measured in the Arena Linux x86_64 sandbox (`Linux 6.1.158+`, Python 3.11.2, NumPy 2.4.6, bundled FFmpeg `n9.0.2-17-g2a571b6068-20260930`):

| N | Median | Minimum | Maximum | Muxes per run |
| ---: | ---: | ---: | ---: | ---: |
| 3 | 407.125 ms | 392.440 ms | 421.614 ms | 16 |

These are single-sandbox measurements, not a service-level latency guarantee. With no FFmpeg executable or `imageio-ffmpeg` package, mux count is zero and the same PNG/WAV/Merkle checks continue to run.

## Regression gates

The test suite includes a 55-case AST mutation campaign. Each case first checks its unmodified oracle, installs one function mutant, asserts the mutant is killed, and restores the original function in `finally`. Coverage includes real pixel/landmark/PCM paths, D65/Bradford identity and direction, RFC 6962 leaf and internal-node domains, and rational PCM slicing/remainder carry.

The scoped Wave 11–12 gates are Ruff check and formatting, strict mypy over all Chronos sources/tests, the 367-test architecture suite, and the complete Chronos suite with aggregate coverage at least 96%, each module at least 95%, and all 55 mutants killed. Exact command outcomes are recorded in the task completion summary after the final gate run.

## Phase 4 — Wave 13 calibrated multicamera continuity

`multicam_epipolar_matching.py` derives the essential matrix `E=[t]×R` and fundamental matrix `F=K_B⁻ᵀ E K_A⁻¹` from a validated calibrated rig. It records the rank-2/equal-nonzero-singular-value audit and uses squared-pixel Sampson distance for correspondence receipts. Projection iterates first-order corrections until every correspondence falls below the declared tolerance; it refuses degenerate gradients and non-convergence.

The optical continuity path uses a thin-lens image-distance ratio around the principal point for focus breathing and reports physical image-plane circle-of-confusion diameter in millimetres. The color path ridge-fits an affine RGB sensor transform (regularized 3×3 matrix with an independently fitted offset), then uses the shared ASC-CDL primitive for channel trims. It reports maximum CIEDE2000 before and after from measured chart patches; the metric is not a substitute for uncontrolled-scene perceptual evaluation.

Wave 13 regression evidence is in `test_multicam_epipolar_matching.py`; its measured module coverage was 96% in the scoped run. The tests exercise calibrated F/E, Sampson projection, lens and CoC domains, ridge sensor fit, ASC-CDL, and typed refusal cases.

The Wave 13 N=3 microbenchmark processes 256 noisy correspondences, a 128×128 focus-breathing image, a 64-patch RGB sensor chart, and a 128×128×3 frame. One warm-up precedes the three measured calls; all three calibrated chart ΔE00 and output-frame checksums were identical.

| N | Median | Minimum | Maximum | Maximum calibrated-chart ΔE00 |
| ---: | ---: | ---: | ---: | ---: |
| 3 | 5.263 ms | 5.086 ms | 7.088 ms | 9.318×10⁻⁸ |

The consolidated refusal and mutation ledgers are recorded after the Phase 4 final gate run.

## Phase 4 — Wave 14 acoustic-space continuity

`acoustic_space_continuity.py` computes area-weighted Sabine–Eyring room invariants, including fixed-room RT60 and critical distance. Its deterministic 3D shoebox image-source model evaluates a 27-cell neighborhood for `max_order=2`, uses six independent wall absorptions and air attenuation, distributes path delays fractionally, and applies Woodworth ITD plus bounded head-shadow ILD. Cross-shot receipts require invariant same-room RT60 and non-increasing DRR from distant to near camera positions. Mono dialogue is FFT-convolved into stereo and explicitly cropped to the original PCM sample horizon; `sample_drift` is zero by construction after input validation.

`test_acoustic_space_continuity.py` verifies the room formula, 27-path RIR, binaural channel difference, exact-length convolution, distance-ordered cross-shot behavior, and typed refusals. Scoped coverage was 99% for this module.

The Wave 14 N=3 benchmark includes three camera RIRs at 48 kHz, the continuity audit, an additional binaural RIR, and one second (48,000 samples) of FFT-convolved dialogue. The same RT60, zero RT60 spread, DRR ordering, and audio checksum were observed in all three runs.

| N | Median | Minimum | Maximum | Output samples | RT60 spread | Sample drift |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 3 | 9.728 ms | 9.653 ms | 10.105 ms | 48,000 | 0.0 s | 0 |

## Phase 4 — Wave 15 kinetic match-cut continuity

`kinetic_match_cut_engine.py` exposes the existing Hann-windowed FFT phase-correlation kernel and scanline rolling-shutter inverse warp through a unit-explicit boundary. It converts camera velocity from pixels/frame to pixels/second and applies a normalized sensor readout fraction. Its match-on-action optimizer evaluates the complete outgoing×incoming frame-pair matrix with weighted 3D landmark-pose, 2D screen-velocity, and speed costs; finite-overflow costs are refused. The globally selected handoff carries a cubic-Hermite 3D landmark trajectory and analytic velocity samples with exact endpoint poses.

`test_kinetic_match_cut_engine.py` covers subpixel Fourier shifts, rolling-shutter units and readout, global cut selection, endpoint/tangent fallbacks, Hermite receipts, and malformed/overflow refusal paths. Scoped module coverage was 100%.

The Wave 15 N=3 benchmark uses 256×320 phase-correlation frames, 256×320×3 rolling-shutter frames, and a global 32×32 cut search over 24 tracked 3D landmarks, producing a 12-sample Hermite bridge. Estimated translation, warp bound, selected cut indices, and bridge checksum matched across all runs.

| N | Median | Minimum | Maximum | Cut matrix | Hermite samples | Selected pair |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 3 | 24.910 ms | 24.723 ms | 24.957 ms | 32×32 | 12 | (21, 1) |
