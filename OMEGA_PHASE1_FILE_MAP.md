# NAGAR CHRONOS-OMEGA — STAGE-A File Map

Branch `arena/288ef95e-nexus-ai-agent`, tip `620855b1`, forked from `c3aa7c8`.
`6915d328` (the OMEGA-02 audio execution plane) remains in the ancestry.

**Deliverable 1 of the OMEGA prompt.** Every row states what a file is responsible
for and what proof exists for it. Nothing here is a display/demo operation: each
verified path is backed by an artifact that is decoded and re-measured *after* the
final encode.

Status vocabulary is limited to `VERIFIED`, `VERIFIED_WITH_LIMITATIONS`,
`HARDENED_BUT_NOT_COMPLETE`, `BLOCKED`, `DEFERRED`.

---

## 1. Normative doctrine (new in STAGE-A)

| File | Responsibility | Status |
|---|---|---|
| `docs/architecture/NAGAR_MATHEMATICAL_ENGINEERING_DOCTRINE.md` | The normative MUST/MUST-NOT layer: four layers (A–D), the eight-part scientific contract, JCGM GUM uncertainty, `Confidence ≠ Probability` (§4), verify-before-publication (§5), the evidence ladder (§6), the single-authority rule (§7), loudness targets are earned not labelled (§8), measurement debt is declared (§9). | `VERIFIED` — indexed in `docs/README.md`, enforced by tests (see §7) |

## 2. The execution plane — `src/nexus_ai_agent/creative/audio_engine/`

The engine sits **outside** `creative/packs` (which stays pure) and outside
`creative/studio` (which stays engine-free). A composition root injects it.

| File | Responsibility | STAGE-A change |
|---|---|---|
| `models.py` | `CanonicalStreamProfile`, `LoudnessProfile`, `ProcessingMode`, `RenderOutcome`, `LoudnessTargetCheck`, `MediaProbe`, `SpectralSignature`, `ResourceBudget`, `ProcessOutcome`. | §4.5: `ProcessingMode = STRICT_LINEAR | DYNAMIC_ALLOWED`; `max_crest_reduction_db` destruction bound; `-14 LUFS` renamed `NAGAR_STREAMING_PRESET`; `LoudnessTargetCheck.processing_mode`/`crest_reduction_db`; `RenderOutcome` carries the source crest factor back from a builder. §4.8: `ResourceBudget` became measured. |
| `verify.py` | `ArtifactPassport` + Ed25519, `build_passport`, `verify_passport`, `check_loudness_target`, `decode_evidence`, per-check codes. | §4.5: computes `crest_reduction_db` from the source and published crest factors; `verify_passport` re-derives the claim from a **fresh** measurement and refuses a passport whose number no longer reconstructs. Fixed a real crash: an unmeasurable true peak raised `TypeError` instead of failing the ceiling check. |
| `execution.py` | `NativeAudioEngine` (implements the port), `AudioExecutionSettings`, the source catalogs, `build_native_audio_engine`, and the `_derive` pipeline: stage → render in an isolated run dir → **verify the private bytes** → atomic publish → measure the budget. | §4.1 verify-before-publish, §4.2 thin workspace adapter, §4.5 canonical conversion **before** pass-1 loudness + the linear/dynamic split with a measured crest reduction, §4.8 measured `ResourceBudget` (`-threads`, staging quota, Peak RSS from `/proc`). Media root derives from the workspace, never `Path.cwd()`. |
| `workspace.py` | `RuntimeWorkspace`: isolated `staging/`, `inputs/`, `media/`, per-run directories, `contain()`, `stage_input()`, atomic `publish()`. | §4.2: now a **thin adapter** on `tools/filesystem_policy.WorkspaceFilesystem` — one containment authority, no second boundary rule. |
| `process.py` | `ProcessGovernor` — the only place a child process is created. Deadline, `CancelToken`, process-**group** kill/reap, bounded capture, `_peak_rss_bytes` from `/proc/<pid>/status` `VmHWM`. | §4.4 cancellation reaches FFmpeg; §4.8 Peak RSS measured or honestly `None`. |
| `ffmpeg_ops.py` | `probe` (banner), `probe_json` (fail-closed `ffprobe -of json` contract), `measure_loudness` (ebur128), `measure_loudnorm_pass`, `render`, `assemble_stream_copy`, `decode_all`, `signature`. | §4.6 typed probe contract; corruption gate (FFmpeg exits 0 on damaged input). |
| `continuity.py`, `numpy_dsp.py`, `recipe.py`, `ed25519_sign.py` | Zero-Drift continuity verdicts; bounded-memory DSP; recipe identity hash; Ed25519. | §4.7 `_interval_candidates` mean-gap fix; §4.8 recipe identity over the full tuple. |

## 3. The port and the packs

| File | Responsibility | STAGE-A change |
|---|---|---|
| `creative/studio/audio_engine.py` | `AudioEnginePort` Protocol + `ArtifactEvidence`, `BeatAnalysis`, and the request models. | `ArtifactEvidence.processing_mode`/`crest_reduction_db`; `BeatAnalysis.budget` + `confidence_kind: Literal["normalized_margin"]`. |
| `creative/studio/evidence.py` | The evidence ladder `PLANNED → IMPLEMENTED → EXECUTION_PROVEN → ARTIFACT_VERIFIED → PRODUCTION_PROVEN`, red state `CONTRADICTED`. | §4.3: unchanged *because it was already correct* — production verifies but never mints. Now pinned by a gate that static `PACK_EVIDENCE` may never contain `PRODUCTION_PROVEN`. |
| `creative/studio/lifecycle.py` | `PACK_LIFECYCLE`, `PACK_EVIDENCE`, `pack_lifecycle()`, `pack_evidence_state()`. | §4.3: the `nexus.audio.studio` reason was stale (claimed DSP paths "unmapped"); it now matches the measured state. |
| `creative/packs/audio/models.py` | Canonical constants, the locked loudness profiles, `NormalizeLoudnessInput`. | §4.5: `nagar_streaming_preset` replaces `streaming_master`. |
| `continuum/pack_coverage.py` | The canonical-test-target registry (a new pack test can never be silently left out of the evidence). | Registered `tests/unit/test_omega_stage_a_hardening.py` as canonical **audio-pack** evidence. |

## 4. Tests — every one drives real FFmpeg or the exact function

| File | Proves |
|---|---|
| `tests/unit/test_omega_stage_a_hardening.py` (63) | The four P0 defects reproduced-then-fixed: no unverified byte ever appears at a published path; a changed recipe parameter never returns the previous artifact; the caller's `CancelToken` reaches the builder; lag-k candidates are **mean gaps**, not finite differences. Plus §4.5: the linear/dynamic split discriminates on one signal (4 verdicts), the crest reduction is arithmetic on the measured file including the dual-mono up-mix term, a destructive target is refused, and `confidence` is labelled a margin. |
| `tests/unit/test_omega_engine_coverage.py` (81) | The engine's **refusal** surface: source resolution, composition and env fallbacks, beat-analysis refusals, loudness contract refusals, transcoder failures (empty concat, quoted path, failed copy leaving no partial file), every `verify_passport` refusal, the numeric layer, the workspace boundary. |
| `tests/unit/test_omega_native_engine.py` (26) | Real FFmpeg end to end: post-decode LUFS at target for both locked profiles, true peak honoured, 96/120/140 BPM detected ±3%, silence → no tempo, corrupt input refused. |
| `tests/unit/test_omega_audio_bus.py`, `test_omega_continuity_assembly.py`, `test_omega_evidence_ladder.py`, `test_audio_pack_evidence_paths.py`, `test_audio_pack.py` | The bus, the continuity verdicts, the ladder, every pack refusal branch. |
| `scripts/mutate_omega_audio.py` (75 probes) | That the suite is load-bearing: revert a fix and a **named** test must fail. |
| `scripts/omega_stage_a_benchmarks.py` | §8.3: N ≥ 3, median/min/max/sd, Peak RSS, process count, FFmpeg version; refuses `N < 3` and exits non-zero on `NOT_MEASURED`. |

## 5. What is measured on this branch

Mutation campaign — `scripts/mutate_omega_audio.py`, **75/75 KILLED**
(`ANCHOR_MISSING`, `AMBIGUOUS_ANCHOR` and `SURVIVED` all fail the gate).

Engine coverage — `coverage run --source=src/nexus_ai_agent/creative/audio_engine`
over the six engine suites, **95.1%** (1839 statements, 89 uncovered; the bar was
95%):

```
execution 97%  models 97%  process 97%  numpy_dsp 96%  continuity 95%
ffmpeg_ops 94%  verify 93%  ed25519 93%  workspace 92%  recipe 100%
```

§4.5 behaviour on a real 18.7 dB-crest programme (mono, dual-mono up-mix):

```
linear render @ -23 LUFS (ebu_r128_broadcast)  → target reached, crest removed < 1 dB
preset     @ -14 LUFS (nagar_streaming_preset) → target reached, 2.38 dB reported
27.8 dB bare click track, -23 LUFS → refused: loudness_target_unreachable
27.8 dB bare click track, -14 LUFS → refused: loudness_crest_destruction
                                     (needs 11.81 dB, profile permits 9 dB)
```

§8.3 benchmarks, N=3, ffmpeg `n9.0.2-17-g2a571b6068-20260930`, py 3.11.2:

```
benchmark   median s    min s    max s    sd s    peak RSS  procs
denoise       0.4842   0.4840   0.4997  0.0090    38.1 MiB      8
loudness      0.9934   0.9912   1.0192  0.0156   131.7 MiB     13
beats         0.1135   0.0936   0.1143  0.0117    28.3 MiB      2
```

## 6. Gates

| Gate | Result |
|---|---|
| `ruff check src/ tests/ scripts/` + `ruff format` | clean |
| `python -m nexus_ai_agent.nagar --docs` | clean |
| `pytest tests/unit tests/architecture` | **6049 passed, 14 skipped, 0 failed** |
| `scripts/mutate_omega_audio.py` | **75/75 KILLED** |
| `scripts/omega_stage_a_benchmarks.py -n 3` | exit 0, all rows `ok` |
| Engine coverage | **95.1%** (bar 95%) |
| Canonical audio-pack coverage | 99.58% (939/943), contract 95% |
| `scripts/agent_board.py check --files <exact list>` | no overlap, before every push |

## 7. STAGE-A status — honest ledger

| Item | Status | Evidence / what is missing |
|---|---|---|
| §Doctrine registered | `VERIFIED` | normative file + `docs/README.md` index row |
| §4.1 verify-before-publish + fenced order | `VERIFIED` | 4 tests + 2 mutation probes |
| §4.2 thin workspace adapter | `VERIFIED` | 7 boundary tests + 1 probe |
| §4.3 measurement ≠ provenance | `VERIFIED_WITH_LIMITATIONS` | ladder + 2 gates; limitation: the canonical audio evidence carries one signing key, so the honest ceiling is `ARTIFACT_VERIFIED` |
| §4.4 cancellation + process-group kill/reap | `VERIFIED` | cancel-mid-render, pre-cancelled, post-process tests |
| §4.5 canonical conversion before pass-1 | `VERIFIED` | pass-1 spy test + `loudness-measured-before-canonical-conversion` probe |
| §4.5 `STRICT_LINEAR` vs `DYNAMIC_ALLOWED` + crest | `VERIFIED` | 4 verdicts on one signal, arithmetic reconstruction, destruction bound |
| §4.5 `NAGAR_STREAMING_PRESET` relabel | `VERIFIED` | 31 call sites renamed; a gate asserts the old name is unreachable |
| §4.6 fail-closed `ffprobe -of json` | `VERIFIED` | every malformed document refuses; 3 probes |
| §4.7 `_interval_candidates` mean gap | `VERIFIED` | 3 tests + 1 probe |
| §4.7 `bpm_std_uncertainty` (Type A), spectral-flux octave disambiguation, full PCM/FFT/STFT/denoise audit | `HARDENED_BUT_NOT_COMPLETE` | mean-gap fix and confidence labelling are in; the GUM Type-A uncertainty report, the spectral-flux autocorrelation octave disambiguation and the full DSP audit are **not** implemented |
| §4.8 recipe identity + measured `ResourceBudget` | `VERIFIED` | 5956→ measured run shown in the earlier unit; 5 probes |
| §8.2 mutation gates (12 named) | `VERIFIED` | 39 probes across 12 gate labels |
| §8.3 benchmarks N ≥ 3 + Peak RSS + tool version | `VERIFIED` | table above; harness refuses `N < 3` |
| §8.2 **live CircleCI Pipeline 678** root-cause, 9 failures classified | `BLOCKED` | see §8 |
| STAGE-B (K1–K4 kernels) | `DEFERRED` | correctly gated: not started, by design |

## 8. The one BLOCKED item, and why

The instruction asks for a live CircleCI **Pipeline 678** root-cause with each of
the nine failures labelled `BASELINE_REPRODUCED | REGRESSION | ENVIRONMENT_BLOCKED |
NONDETERMINISTIC | UNCLASSIFIED`. That classification cannot be produced here, and
saying so is the only honest answer:

1. **`.circleci/` does not exist** — not in the working tree, and `git log --all --
   .circleci` returns nothing, so no ref in this repository has ever carried it.
   `docs/DECISION_LOG.md` records the removal: the branch `circleci-project-setup`
   was "Irrelevant — CI platform cut … `.circleci/` does not exist on `main`; the
   project standardizes on GitHub Actions."
2. **The project's CI is GitHub Actions** (`.github/workflows/ci.yml`,
   `queue-ownership-mutations.yml`, `maintenance.yml`). Every recent run reachable
   through the API is a GitHub Actions run, not a CircleCI pipeline.
3. **`circleci.com` is not reachable from this sandbox** (egress is allowlisted to
   GitHub/npm/PyPI), so no CircleCI API query can be made even in principle.

The one *currently failing* scheduled workflow on `main` — GitHub Actions run
`43207417853` (`maintenance`) — was inspected instead and **is** classified:

| Check | Classification | Root cause |
|---|---|---|
| `backup-db` → step "Preflight — required production backup configuration" | `ENVIRONMENT_BLOCKED` | the scheduled job refuses to run without production backup credentials, which a fork/sandbox CI does not have. `housekeeping` and `restore-drill` were `skipped` as a result. Nothing in the audio engine, and no code defect. |

To close the CircleCI item, either the pipeline must be identified by its real
platform and run id (a GitHub Actions run id, or a CircleCI project slug + token),
or a `.circleci/config.yml` must be reintroduced. Both are owner decisions.

## 9. Forward tasks (10)

| # | Task | Why now |
|---|---|---|
| 1 | §4.7 GUM Type-A `bpm_std_uncertainty` on the measured onset grid (JCGM GUM §5.2) | the only remaining *unmeasured* number in the beat path |
| 2 | §4.7 spectral-flux autocorrelation octave disambiguation (competing-peak test, not a constant) | 96/120/140 BPM pass today, but an octave error on real music is currently undetectable |
| 3 | Full PCM/FFT/STFT/denoise audit note (window function, overlap, band edges, `afftdn` limits) | the doctrine demands a stated measurement protocol for every DSP claim |
| 4 | STAGE-B K1: `RationalTime` + delta shot-DAG + verified `-c copy` assembly | the first kernel; `continuity.assert_lossless_assembly` is its proof seam |
| 5 | STAGE-B K2: `K[R|t]` + Brown-Conrady + thin-lens CoC + GUM Jacobian `∂H_px/∂Z_c` | depends on K1's rational time |
| 6 | STAGE-B K3: sRGB/BT.709 → scene-linear, Bradford, albedo/shading split, regularized Monge–Kantorovich relighting | needs a scene-linear substrate; measure `ρ_∇` and `ΔE2000` against a real render |
| 7 | STAGE-B K4: Allen–Berkley ISM RIR + Woodworth ITD/ILD coupled to camera distance; liftered cepstral timbre; rational-PTS jaw/viseme solver | last kernel; couples to K2's geometry |
| 8 | `DYNAMIC_ALLOWED` on a stereo source (the crest arithmetic is proven on mono→stereo; the 1:1 channel case needs the same reconstruction gate) | closes the last unmeasured branch of §4.5 |
| 9 | Identify the real CI pipeline behind "Pipeline 678" (platform + run id) and classify its 9 failures | unblocks the last `BLOCKED` STAGE-A item |
| 10 | Standing: `scripts/mutate_omega_audio.py` + the benchmark harness become a CI job, not a manual step | a gate that is only run by hand is a gate that will be skipped |
