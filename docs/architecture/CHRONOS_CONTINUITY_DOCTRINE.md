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
