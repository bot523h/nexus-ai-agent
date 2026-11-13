# Hyperion Phase 2 Speech-Cinema Doctrine

These modules are pure, deterministic building blocks for offline speech cinema. They accept PCM arrays or standard WAV input, avoid service calls and mutable global state, and use NumPy only for signal math.

* **Voice detection** reports frame masks and exact `Fraction`-based segment boundaries; thresholds are explicit in `VADConfig` and SNR is estimated against a low-percentile noise floor.
* **Alignment** is a reproducible baseline rather than a language model: tokenization, grapheme phonemization, viseme IDs, aperture weights, and monotonic banded DTW have stable tie-breaking.
* **Dubbing** guarantees the requested frame count, including empty and multichannel inputs. The baseline uses deterministic band-limited linear interpolation and exposes a result object for auditability.
* **Subtitles** are data-first: aligned words become cues via duration, character, gap, and shot-cut constraints, and codecs emit stable SRT, WebVTT, and ASS. SHA-256 fingerprints are over exact serialized bytes.

Callers should preserve sample rate metadata and treat times as seconds unless documented otherwise. Inputs are never modified in place.


## Phase 3 completion (artifact_vault, speech_cinema, color)

### Architecture boundary

* The NumPy/Pillow colour kernels live in `speech_cinema/color_imaging.py` (adapter side).
* `creative/packs/color/` is a pure data contract (`OPERATIONS`, `KELVIN_RANGE`) with no sibling imports and no NumPy or Pillow.
* `speech_cinema/color_ops.py` binds the contract to the kernels and fails closed on any missing or extra operation.
* `tests/architecture`: **367 passed, 0 failed**.

### Gates (measured on the final tree)

| Gate | Result |
|---|---|
| `mypy --strict` (artifact_vault, speech_cinema, packs/color) | **0 issues in 12 files** |
| `ruff check` / `ruff format --check` | clean |
| `pytest --cov-fail-under=95` (zone, `-m "not slow"`) | **99.65 % total**, **every file >= 99 %** |
| Falsification ledger | **40 probes**: 35 pass, 5 recorded as strict-xfail gaps |
| Mutation campaign | **30 / 30 killed** |
| Benchmarks | N = 3 per workload (table below) |

### Per-file coverage

| File | Stmts | Miss | Cover |
|---|---:|---:|---:|
| `creative/packs/color/__init__.py` | 5 | 0 | 100 % |
| `creative/speech_cinema/color_imaging.py` | 127 | 1 | 99 % |
| `creative/speech_cinema/color_ops.py` | 16 | 0 | 100 % |
| `creative/speech_cinema/forced_aligner.py` | 122 | 1 | 99 % |
| `creative/speech_cinema/isochronous_dubbing.py` | 43 | 0 | 100 % |
| `creative/speech_cinema/subtitle_compiler.py` | 81 | 0 | 100 % |
| `creative/speech_cinema/vad_detector.py` | 126 | 0 | 100 % |
| `storage/artifact_vault/cdc_chunker.py` | 66 | 0 | 100 % |
| `storage/artifact_vault/galois_erasure.py` | 133 | 1 | 99 % |
| `storage/artifact_vault/vault_manager.py` | 127 | 0 | 100 % |

The remaining misses are defensive guards that are unreachable by construction (for example, DTW connectivity once the band is widened to at least `|n-m|`).

### Falsification ledger (40 probes)

* **Probes 1-14 (artifact_vault):** GF(2^8) inverse and distributivity, RS parameter refusal, erasure recovery for parity, data, mixed and `m+1` losses, Merkle tamper detection (leaf and node), on-disk heal, CDC resynchronisation, traversal refusal, empty-payload and bounds refusal. All pass.
* **Probes 15-28 (speech_cinema):** silence, NaN/Inf, sample rate, DTW band and monotonicity, WSOLA identity and exact counts, stretch-ratio refusal, 42-character limit, SRT/VTT/ASS validity, inverted timestamps. Pass.
  * **Gaps (strict xfail):** p24 (CPS > 20 split or flag), p25 (out-point snap to `t_cut - 2 frames`), p26 (no cue may span a shot cut).
* **Probes 29-40 (color):** Bradford, filmic bound and monotonicity, Gaussian OT mean and covariance, zero-variance refusal, channel-count refusal, negative radiance refusal, alpha blend and bounds. Pass.
  * **Gaps (strict xfail):** p29 (Bradford identity at equal temperatures, measured error about 4.3e-2 against a 1e-6 target) and p30 (no general source-to-destination Bradford API, so no round trip).

### Specification-to-implementation discrepancies (recorded, not hidden)

| Handover wording | Implemented reality | Resolution |
|---|---|---|
| Kelvin range 1000-40000 K | 2000-15000 K | Probe 31 tests the implemented bound; `KELVIN_RANGE` is declared in the pack contract. |
| `auto_heal_artifact` / `WorkspaceFilesystem` | `ArtifactVault.verify_and_heal_artifact` over a root directory | Probes 11 and 13 test the real API. |
| CPS, shot-cut snap and in-point guard | Not implemented; cuts only split groups | Strict-xfail gaps p24, p25, p26. |
| Bradford `T_src -> T_dst` identity | Only `kelvin -> D65` exists | Strict-xfail gaps p29, p30. |

### Defects found by falsification and fixed in this phase

1. **Filmic tonemap exceeded 1.0.** Saturated highlights were scaled by luminance alone, so a pure red at 100 produced 4.57 and blue produced 12.88. Output is now scaled per pixel to keep every channel in [0, 1], preserving hue. Regression: p32 and mutant M25.
2. **VAD crashed on NaN and Inf input** with an `IndexError`. It now raises a typed `ValueError`. Regression: p16.
3. **VAD accepted `sample_rate = 0`.** The falsy default `sample_rate or 16000` swallowed zero. It now uses an explicit `None` check and rejects non-positive rates. Regression: p17.
4. **VAD crashed on clips shorter than the smoothing window.** `np.convolve(mode="same")` returns `max(len, width)`. The window is now clamped to the frame count. Regression: short-clip test and mutant M15.
5. **A silent clip shorter than `min_silence` was reported as speech.** The gap filler promoted leading and trailing silence. It now fills only gaps bounded by speech on both sides. Regression: short-silence test and mutant M16.
6. **Time scaling had no stretch bound.** Ratios of 400x and 0.2x were accepted. Ratios outside the open interval (0.2, 4.0) are now refused. Regression: p22 and mutant M19.
7. **Inverted or zero-length subtitle cues were accepted** (p28). They are now refused with a typed error.
8. **VTT cues without an identifier were silently dropped**, and `parse_vtt` replaced every `.` in the text, not only in timestamps (`Dr. Smith` became `Dr, Smith`). Parsing now handles optional identifiers, skips header and NOTE blocks, and preserves text.
9. **Erasure accepted an empty payload.** It is now refused with a typed error (p14).
10. **Non-finite DTW costs produced a misleading error** (`band does not connect`). They are now refused as non-finite.

### Mutation ledger (30 / 30 killed)

The campaign applies one exact textual change to a private copy of the package and runs the zone suite in a subprocess. A mutant is killed only if the suite fails. The harness first verifies that the subprocess imports the copy and that the unmutated zone is green.

* **Killed by the existing suite on the first run (26):** M01, M03-M10 (GF polynomial, empty-payload refusal, Vandermonde base, GF range check, Merkle prefixes, heal and unavailable refusals, CDC mask, gear seed is not in this group), M13-M14 (VAD gates), M16 (edge-silence regression), M17-M30 (DTW recurrence and band, stretch bound, WSOLA interpolation, 42-character boundary, zero-length cue, fraction scale, filmic bound and constants, Bradford coefficient, matrix root, OT blend, PNG quantisation).
* **Survived the first run, killed after strengthening (4, no survivors in the final run):** M02 (boundary `k+m = 255`, new KAT), M11 (gear seed, new golden master with content-defined chunk bounds), M12 (broadband noise rejected by the entropy gate, new test), M15 (loud clip shorter than the smoothing window, new regression test).

Golden masters and KATs are labelled as such in `test_hyperion_known_answers.py`. Golden masters are regression pins from the reference implementation. KATs are derived from RFC 6962, GF(2^8) with polynomial 0x11D, the ICC Bradford constants and the filmic formula.

### Benchmarks (N = 3 per workload, `time.perf_counter_ns`, `tracemalloc`)

| Workload | Runs | Median ms | Max ms | Peak heap KiB |
|---|---:|---:|---:|---:|
| `artifact_vault_cdc_rs8_4_heal_merkle` | 3 | 10485.5 | 10561.6 | 2734 |
| `speech_cinema_vad_dtw_wsola_subtitles_5s_16k` | 3 | 134.1 | 180.6 | 5243 |
| `color_imaging_bradford_filmic_ot_128x128x3` | 3 | 7.7 | 25.7 | 3846 |

**Top performance risk:** the Reed-Solomon path over GF(2^8) is pure-Python per byte. Encode, heal and read of 256 KiB take about 10.5 s. Vectorising the GF multiply with NumPy log and exp table lookups is the first optimisation to make. The benchmark asserts only a 60 s ceiling, so it will not catch this regression by itself.

### Commit ledger

| Commit | Scope |
|---|---|
| `02c983b` | Manus baseline (vault, speech_cinema, color) |
| `2ff717c` | Board claim for the Hyperion completion zone |
| `6d2cd4f` | Stage 1: boundary fix (367/367) and `mypy --strict` zero |
| this commit | Stage 2 and 3: ten defect fixes, 40-probe ledger, coverage, 30-mutant campaign, N = 3 benchmarks, doctrine |

Known blocker outside the zone: `tests/unit/test_pack_coverage_contract.py` fails at `02c983b` as well, because `creative/packs/color` has no `pack.manifest.json` and no entry in `continuum/pack_coverage.py::PACK_TEST_TARGETS`. Those files are not in the authorised zone.

### Out of scope (not changed)

Alpha (`audio_engine`, `packs/audio`, `slideshow/audio.py`), Beta (`chronos`), OpenHands (`episode_causality_lab`, `director`, `composition`, `packs/scene`, `packs/portrait`), `creative/studio/bus.py`, `docs/README.md`, `docs/OPERATION_TRUTH.json`, and the `#334` branch and claim.
