# NAGAR CHRONOS-OMEGA — Phase 1 File Map

Branch `arena/288ef95e-nexus-ai-agent` (from `c3aa7c8`).
Every row states **what the file is responsible for** and **what proof exists** for it.
No operation in this map is a display/demo operation: each `READY` path is backed by an
artifact that is re-decoded and re-measured *after* the final encode.

---

## 1. New execution plane — `src/nexus_ai_agent/creative/audio_engine/`

The engine is deliberately **outside** `creative/packs` (stays pure) and outside
`creative/studio` (stays engine-free). A composition root injects it.

| File | Lines | Responsibility | Proof |
|---|---|---|---|
| `__init__.py` | 100 | Package contract + public surface (`NativeAudioEngine`, `build_native_audio_engine`, profiles, passports). Documents which module owns which concern. | Imported by tests + `render_jobs`; `test_no_orphan_modules` green. |
| `models.py` | 559 | `CanonicalStreamProfile` (48 kHz, channel layout, codec, sample format, `format_arguments()`), `LoudnessProfile` (`EBU_R128_BROADCAST` −23/−1/11, `STREAMING_MASTER` −14/−1/11), `ContinuityLockSpec`, `IdentityLockSpec`, `MediaProbe`, `SpectralSignature`, `LoudnessMeasurement` (silent = all-`None`), `LoudnessTargetCheck`, `DecodeEvidence`, `ResourceBudget`, `ProcessOutcome`. | Pure pydantic; validators refuse unsupported rates, silent-with-numbers, etc. |
| `process.py` | 344 | **The only place a child process is created.** `ProcessGovernor`: mandatory deadline, cancellation token, process-*group* kill, poll loop, bounded stdout/stderr `_BoundedSink`, stdin closed, drain threads joined. `ProcessTimeout` / `ProcessCancelled` carry the `ProcessOutcome`. | 3 adversarial tests: deadline kill (<20 s), cancel mid-run, captured bytes bounded. |
| `workspace.py` | 237 | `RuntimeWorkspace`: isolated `staging/`, `inputs/`, `media/`, per-run directories, `contain()` boundary, `stage_input()` (hash-addressed copy), `publish()` = atomic `os.replace` **only**, refuses existing destination, refuses a run's own input, `hash_file()`. | Tests: atomic publish, overwrite refused, run-input protection, out-of-root refused, no staging residue after refusal. |
| `ffmpeg_ops.py` | 605 | `FfmpegEngine` (probe by parsing `ffmpeg -i`; `measure_loudness` via `ebur128`; `measure_loudnorm_pass`), `FfmpegTranscoder` (`render`, `assemble_stream_copy`, `decode_all`, `signature`, `signature_resampled`), `resolve_ffmpeg_bin`, `corruption_marker()` + `AudioDecodeError` / `AudioEngineUnavailableError` / `AudioEncodingError` / `AudioMeasurementError`. | **FFmpeg exits 0 on damaged input while printing `corrupt input packet`** — the corruption gate turns that into a typed refusal (pinned by test). |
| `numpy_dsp.py` | 244 | Bounded-memory analysis: frame streaming, RMS/peak/crest/dynamic range (P95−P10), zero-crossing rate, spectral centroid + 8-band energy ratios via `sliding_window_view`, onset envelope, tempo candidates with confidence. | `analyse_samples` refuses non-mono input (caught a real bug in `signature`). |
| `verify.py` | 617 | `ArtifactPassport` (schema-versioned, canonical payload, Ed25519 signature), `build_passport`, `verify_passport`, `PassportKeyring`, `check_loudness_target`, `decode_`/`signature_` gates, `VerificationReport` with per-check codes. Verifies the **file on disk after the final encode** — never a pre-encoder array. | Tests: verified / tampered-byte / forged-payload / unknown-key / JSON round-trip. |
| `ed25519_sign.py` | 133 | `derive_public_key`, `sign_ed25519`, `key_id_for`, `Ed25519SigningError`. Reuses the group law from `creative.packs.ed25519` and **verifies every signature it produces before returning it**. | RFC 8032 vectors via the shared verifier; round-trip tests. |
| `continuity.py` | 466 | Zero-Drift: `compare_continuity` (per-axis verdicts `ok`/`drift`/`unknown`, tolerances documented in `TOLERANCES`), `detect_lock_drift` (cheap pre-production lock comparison), `assert_lossless_assembly` → `AssemblyProof` (`stream_copy` vs `re_encode_required` with the reason), `ContinuityError`. | Tests: stitchable pair, wrong-rate blocks copy, mono-blocks-stereo, missing evidence = `unknown` **not** `ok`. |
| `execution.py` | 850 | `NativeAudioEngine` (implements the port), `AudioExecutionSettings` (budgets + signing key), `AudioSourceCatalog` / `CallbackSourceCatalog` / `LocalDirectorySourceCatalog`, `build_native_audio_engine()` (the one place that decides where bytes live), `prove_lossless_assembly()`, and the shared `_derive` pipeline: **stage → run in isolated dir → atomic publish → verify the published bytes → quarantine `<name>.rejected` on refusal → reuse a still-verified artifact on replay**. Also refuses an *unreachable* loudness request up front: if raising the programme to the target would push its peaks past the true-peak ceiling, no limiter or dynamic pass can satisfy both numbers, so it refuses with the arithmetic (`loudness_target_unreachable`) instead of publishing an undershooting file. | 24 real-FFmpeg tests + 8 end-to-end bus tests + 11 continuity tickets. |

## 2. The port (contract home)

| File | Lines | Responsibility | Why here |
|---|---|---|---|
| `creative/studio/audio_engine.py` | 257 | `AudioEnginePort` Protocol, `ArtifactEvidence`, `BeatAnalysis`, `SourcePrecheck`, `SourceRequest`, `LoudnessRequest`, `DenoiseRequest`, `BeatRequest`, `EngineCapability`, `AudioEngineHealth`, `EngineUnavailable` / `EngineInputError` / `EngineRejected`. | `creative/packs/audio/**` may import **only** stdlib + pydantic + `creative.studio` (`test_audio_pack_boundary`). Declaring the contract here is what lets the pack type its seam without crossing a layer. |
| `application/ports/audio_engine.py` | 51 | Compatibility re-export of the above (defines nothing of its own). | Callers already using the older path keep working. |

## 3. Studio lifecycle — evidence-based states

| File | Lines | Responsibility |
|---|---|---|
| `creative/studio/evidence.py` | 230 | `OperationEvidenceState` ladder `PLANNED → IMPLEMENTED → EXECUTION_PROVEN → ARTIFACT_VERIFIED → PRODUCTION_PROVEN` + red state `CONTRADICTED`. Promotion requires a **signed** verifier corroboration naming the same artifact hash; a refusal is `CONTRADICTED`; `PRODUCTION_PROVEN` needs **two independent key ids**. `assess_state`, `assert_verified`, `states_for`. |
| `creative/studio/lifecycle.py` | +64 | Existing maturity axis untouched. Adds `PackLifecycle.evidence_state`, `PACK_EVIDENCE` (measured ceilings), `pack_evidence_state()`. Rationale documented: canonical audio evidence carries **one** signing key ⇒ honest ceiling is `ARTIFACT_VERIFIED`; nothing claims `PRODUCTION_PROVEN`. |
| `creative/studio/capabilities.py`, `creative/studio/bus.py` | +23 | The `audio_engine` composition seam threaded `CommandBus → OperationContext`. Duck-typed `Any` so studio stays free of the engine. |

## 4. The audio pack — real execution, no simulation

| File | Lines | Change |
|---|---|---|
| `creative/packs/audio/models.py` | +158 | Canonical profile constants, the two locked loudness profiles, `NormalizeLoudnessInput` validator (a locked profile **locks** its numbers; `custom` requires all three; single-pass is refused), `EXECUTION_STATUS_READY`/`UNAVAILABLE`, `CODE_REQUIRES_LOCAL_MODEL`/`^AUDIO_ENGINE`/`^MEDIA_BYTES`, `DetectedBeatGrid` + in-process beat memory, `BeatGridRef` reconciling `total_beats` with its markers. |
| `creative/packs/audio/operations.py` | +639 | `audio.normalize_loudness` → real two-pass `loudnorm`, verified artifact, measured evidence. `audio.detect_beats` → real measurement (no `120` constant). `audio.beat_sync_cut` → snaps to the **measured** grid, otherwise the declared anchor explicitly labelled `tempo_source="default_120"`. `audio.remove_noise` → real `highpass`+`afftdn`, signature evidence, no separation claim. `audio.remove_vocal` → **honest `UNAVAILABLE / REQUIRES_LOCAL_MODEL`, zero assets**. State-only ops labelled `derivation="state_only"`, `artifact_verified=False`. |
| `creative/slideshow/audio.py` | +145 | Upgraded the existing analyzer (not replaced): `TempoEvidence`, `collect_tempo_candidates`, `tempo_evidence_for_file`. |

## 5. Composition root

| File | Change |
|---|---|
| `creative/render_jobs.py` | `AUDIO_ENGINE_OPERATIONS` + `_audio_engine_for(operation)`; `build_job_bus(..., audio_engine=...)`. The render lane now *gives* the engine-backed operations a real engine and `None` to everything else — no handler can be handed a process-spawning engine it does not need. |

## 6. Tests (all real, all in CI)

| File | Lines | What it proves |
|---|---|---|
| `tests/unit/omega_media.py` | 178 | Synthetic adversarial media generated at test time (no binary fixtures): quiet sine, clipped/hot, 96 BPM clicks, noisy voice, silence, corrupt RIFF, noise floor, multichannel. |
| `tests/unit/test_omega_native_engine.py` | 700 | Real FFmpeg. Source byte-identical after every run; post-decode LUFS at target for both locked profiles; true peak honoured on clipped input; the target check is **not** a rubber stamp; 96/120/140 BPM each detected (±3%), never the old constant, and a window excluding the truth reports **no** tempo; silence → no tempo; corrupt file refused; decode-corruption gate; denoise lowers the floor; strength 0 is an honest passthrough; passport verified/tampered/forged/unknown-key/JSON round-trip; deadline kill; cancellation; bounded capture; atomic publish + boundaries; no staging residue; **an unreachable target is refused with the arithmetic**, while a sustained programme reaches both locked targets — proving the gate discriminates rather than blocking loudness work wholesale. |
| `tests/unit/test_omega_audio_bus.py` | 286 | End-to-end `CommandBus`: the recorded `content_sha256` equals the file's hash read independently; replay reuses the same passport; a source whose bytes drifted from the registry is refused; silent source fails with nothing written; state-only ops labelled. |
| `tests/unit/test_omega_evidence_ladder.py` | 240 | Ladder against a real run: one key ⇒ `ARTIFACT_VERIFIED`; two keys ⇒ `PRODUCTION_PROVEN`; same key twice ≠ two verifiers; refusal ⇒ `CONTRADICTED`; wrong-hash corroboration promotes nothing; `PACK_EVIDENCE` cannot exceed the proof. |
| `tests/unit/test_omega_continuity_assembly.py` | 292 | Stitchability proved on rendered segments, blocked on rate/layout mismatch; two episodes under one lock are continuous; a different voice drifts; missing evidence is `unknown`, never `ok`; tolerances pinned. |
| `tests/unit/test_audio_pack_evidence_paths.py` | 609 | Every refusal/degenerate handler branch with a declared double: unreadable bytes, no tempo, engine refusal, missed target, unverified artifact, replay, clamping arithmetic, model-level refusals. |
| `tests/unit/test_audio_pack.py`, `test_opgap_wave5.py`, `test_opgap_audio_motion.py`, `test_capability_lifecycle.py` | +337 | Stale fake-behaviour expectations rewritten to the new truth (`remove_vocal` UNAVAILABLE, denoise needs an engine, no `120 BPM` constant). |

## 7. What the engine proves on a real run

One `python` invocation against synthetic media (FFmpeg n9.0.2), verifier included:

```
normalize (ebu_r128_broadcast): -23.0 LUFS / TP -10.6 / verified=True / 0.04 s
                                / 48000 Hz flac 2ch
beats (sustained programme):    no onsets → status="insufficient_onsets", tempo=None
                                (reported as a reason, never as 120)
denoise:                        rms -19.68 dBFS, centroid 7916 Hz, verified=True
assembly:                       stream_copy — all segments share one stream shape
sources after the run:          both SHA-256 values unchanged
staging leftover:               []
```

And the case that motivated the up-front gate (a 96 BPM click track at −31.43 LUFS /
−3.61 dBTP), refused before any file is published:

```
loudness_target_unreachable: this programme cannot reach -23 LUFS without exceeding
the -1 dBTP ceiling. It measures -31.43 LUFS at -3.61 dBTP (crest 27.82 dB) and needs
+8.43 dB, which would peak at +4.82 dBTP. This profile allows at most 22 dB of crest
factor, so no render — not a limiter, not a dynamic pass — can satisfy both numbers at
once. Publishing a file that undershoots the target (or clips to reach it) would be a
false success; lower the target, raise the ceiling, or supply a programme with less
dynamic range.
```

## 8. Gates

| Gate | Status |
|---|---|
| `ruff check src/ tests/` | clean |
| `mypy` (engine, port, evidence, lifecycle, audio pack) | clean, 17 files |
| Canonical audio pack coverage | **99.58%** (939/943 lines; was 93.6% mid-work), contract ≥ 95% |
| `test_audio_pack_boundary`, `test_nagar_studio_isolation` | green (this is why the port moved into `creative.studio`) |
| `test_no_orphan_modules`, `test_lifecycle_gate_boundary`, `test_pack_manifest_is_data_only`, `test_delivery_pack_boundary`, `test_pack_trust_boundary` | green |
| `python -m nexus_ai_agent.nagar --docs` + truth gate + mutation probes | regenerated, 41 tests green |
| `tests/unit` + `tests/architecture` | **5889 passed**, 9 failures — all pre-existing local-env failures (B2/Neon SDK versions, pg engine, PDF, knowledge CLI), identical on a pristine `c3aa7c8` worktree |
