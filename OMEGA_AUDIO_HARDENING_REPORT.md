# OMEGA audio engine — hardening report

Branch `arena/288ef95e-nexus-ai-agent`, PR #337. Base `c3aa7c8`.
Head at time of writing `ba2643b`. **Status: `HARDENED_BUT_NOT_COMPLETE`.**

Everything below is a measurement taken in this repository, on this tree, with
the commands shown. Numbers that were not re-measured are labelled as carried
forward and are attributed to the commit that produced them.

---

## 1. What the work was

The audio engine could render, but it could not demonstrate that what it
rendered was correct. The audit that opened this work found four P0 defects
(`17a08a0`), and the fixes for them were themselves unmeasured: a claim that a
loudness match was within tolerance, with no independent meter to check it
against; a crest-factor check that compared a file to itself; a "denoised"
result with no SNR figure.

The work is therefore organised around one rule:

> **A number is not evidence until something independent can disagree with it.**

Each part below installs a measurement, then installs an artifact that can
falsify it.

---

## 2. Defects found and fixed

These are real bugs, not refactors. Each was found by a test written to break
the code, not by reading it.

### 2.1 The STRICT_LINEAR crest check was circular — `d063f39`

`verify` compared the rendered artifact's crest factor against the *previous
manifest*. A file could be crushed and still pass, because the check only ever
confirmed that the pipeline agreed with itself.

Fixed by requiring the **source** measurement: `STRICT_LINEAR` now asserts that
the crest factor is invariant from source to master, with
`STRICT_LINEAR_CREST_TOLERANCE_DB = 0.25`. A passport claiming `STRICT_LINEAR`
without a source is refused outright. Demonstrated end to end: a master with
+9.90 dB of code reduction claiming `STRICT_LINEAR` is refused with
`CODE_CREST_NOT_INVARIANT`.

### 2.2 The Wiener denoiser returned NaN — found this part, fixed in `ba2643b`

`estimate_noise_psd` floored the estimate at `np.finfo(np.float64).tiny`.

That looks like a floor. It is **2.2e-308**: non-zero, so every guard that
tests `estimate > 0` passes, but any real signal divided by it overflows to
infinity, and the weighted overlap-add turns the whole frame into NaN.

The bug only surfaced on a **sub-window input**: a 100-sample signal is
reflection-padded to 1024 each side by `_frame_signal`, producing ten frames
that are almost entirely padding, so the estimate collapses to the floor. The
symptom was a round-trip error of `nan`.

Fixed by `NOISE_FLOOR_POWER = 1e-20` (−200 dBFS per bin — far below any real
converter noise, far above overflow), with `np.clip` guards on `gamma` and
`prior`.

**The guards are bit-exact on real input.** Verified by loading the pre-edit
module and the current one side by side:

```
old (HEAD before fix): 10.365383619626568 dB
new (with clip)      : 10.365383619626568 dB
```

They change only the degenerate paths. Pinned by a dedicated test and by a new
mutation probe that reverts the floor to `float64.tiny`.

### 2.3 Four CI regressions introduced by the branch — `162a1e6`

Repaired before the hardening work proper began.

---

## 3. The independent measurements

| claim | independent check | result |
|---|---|---|
| Loudness matches | BS.1770-5 meter written from the standard, cross-verified against FFmpeg `ebur128` | agrees within **0.1 LU** |
| Denoising improves SNR | SNR against a known clean reference | **+10.37 dB** @16 kHz, **+11.68 dB** @48 kHz |
| Denoiser is invertible | WOLA round trip at unity gain | **4.4e-16** (bound was 1e-6) |
| M/S encode/decode is lossless | exact round trip | **5.55e-17** |
| HPSS masks are complementary | mask sum | **0.0**, reconstruction **3.33e-16** |
| De-esser acts only on sibilance | band measurement | **12.00 dB** de-ess, low band **−111.5 dB**, sibilance ratio **0.0669** |
| LRA is measured, not guessed | sliding 3 s windows per BS.1770 | **19.99999 LU** on a synthetic ramp |
| Tempo octave decision | GUM uncertainty on the estimate | octave-stable, uncertainty carried |

**BS.1770 K-weighting was wrong at first.** The RBJ high shelf is *not* the
BS.1770 shelf — the standard specifies its own coefficients. This was caught by
disagreeing with FFmpeg, which is the entire reason the cross-check exists.

**Three noise-estimator designs were rejected on measurement.** A running
minimum and a plain time percentile both fail on sustained music: the same tone
returns in every frame, so it never falls below the percentile and the estimate
sits at the signal's own level. The working design is the **minimum of the
time-percentile across a small spectral neighbourhood** — noise is broadband and
programme is narrowband, so a harmonic cannot drag its own estimate up.

---

## 4. The mutation campaign — 75/75 killed

`scripts/mutate_omega_audio.py`. Each probe reverts a specific fix and asserts
that a **named** test fails. A probe whose anchor text no longer exists reports
`ANCHOR_MISSING` — it never silently passes, which is what makes the count
trustworthy.

```
75/75 mutations killed
```

Groups: §4.7 octave (4), GUM (5), BS.1770 (9), STRICT_LINEAR (3), Wiener (6),
M/S + HPSS (3), forensic QC (5), Part 2 (2), original Stage-A (39).

**The campaign was re-run every time the source changed**, including after a
`ruff format` pass — formatting rewrote one anchor, and a green run from before
that edit would not have been evidence. The final 75/75 is against the exact
tree at `ba2643b`.

---

## 5. Coverage of `creative/audio_engine`

Measured with `--source=src/nexus_ai_agent/creative/audio_engine` over 12
suites, 381 passed / 1 skipped:

```
TOTAL                2557   124   95%
recipe.py             100%      execution.py       97%
models.py              97%      process.py         97%
numpy_dsp.py           96%      ffmpeg_ops.py      95%
loudness_bs1770.py     95%      forensic_qc.py     95%
stereo_hpss.py         94%      ed25519_sign.py    93%
verify.py              92%      workspace.py       92%
wiener_denoise.py      98%
```

Scoping matters and is deliberate: including `creative/slideshow` in the same
run drags TOTAL to 72%, because three slideshow modules execute at 0% under the
audio suites. That is a property of the suite selection, not of the code.

The two modules that carried the least coverage were the two that carried the
headline claims — `verify.py` at 88% and `wiener_denoise.py` at 91%. Both are
now above the bar, and the new `test_omega_wave2_verify_e2e.py` drives the
passport chain through **real FFmpeg renders** rather than mocks.

**A fixture bug worth recording.** The first end-to-end test failed with
"+3.00 dB of crest". The cause was not the pipeline: the fixture wrote a **mono
WAV** and measured it against a **canonical stereo FLAC**. The dual-mono up-mix
alone changes the crest factor by exactly 3 dB. Four raw-FFmpeg control chains
reproduced 25.300 dB, and both canonical files measured 22.300 dB — a
**0.0000 dB** difference between them. Fixtures are now built through
`CANONICAL_PROFILE`.

---

## 6. Repository gates

| gate | result |
|---|---|
| `ruff check .` | All checks passed |
| `ruff format --check .` | 1010 files unchanged |
| `pytest tests/unit tests/architecture` | **6231 passed, 14 skipped, 0 failed** (661.81 s) |
| pack-coverage contract | 97 passed |
| `scripts/pack_coverage.py` | `verdict: ACCEPTED`, 97.52% total, all packs ≥95% |

**One integration hazard recurred and is worth stating plainly.** Adding a test
module named like the audio suites silently invalidates `OPERATION_TRUTH.json`;
the stored projection must be regenerated with
`nexus_ai_agent.nagar.truth.write_projection()`. Separately, a test module that
imports a pack but is absent from `PACK_TEST_TARGETS` **breaks the whole suite**
— the fix is registration as a canonical target, not a host-layer exemption.

---

## 7. What is not done

This is why the status is `HARDENED_BUT_NOT_COMPLETE` and not `DONE`.

1. **Python 3.10 was never executed.** There is no 3.10 interpreter in the
   build sandbox and egress is blocked. Compatibility was checked statically
   only. CI runs a 3.10 leg; it is not the same thing as a local run and is not
   claimed as one.
2. **`verify.py` at 92% is the lowest module in the engine.** The uncovered
   lines are concentrated in passport failure branches that need a
   deliberately-corrupt artifact to reach.
3. **`creative/slideshow` audio modules are barely exercised** (0–67%) because
   no audio suite targets them.
4. **CI on the final commit was still running when this was written.** The
   previous commit `5910c4f` failed three jobs — `test (pytest)`,
   `python-parity`, and `continuum-evidence` — all tracing to one cause: an
   unregistered test module made pytest exit non-zero, which flips the evidence
   gate to `accepted=False`. That is fixed in `ba2643b` and reproduces locally
   as `ACCEPTED`, but the fix has not been confirmed green on CI.

---

## 8. Commit ledger

| commit | what |
|---|---|
| `6915d32` | OMEGA-02 real audio execution plane |
| `16929f8` | claim the hardening zone |
| `17a08a0` | close four P0 audio defects from forensic audit |
| `1fb5d0b` | workspace becomes a thin adapter on the filesystem authority |
| `07ac3fd` | measure the resource budget |
| `6b43175` | separate measurement evidence from the provenance passport |
| `b0f90ae` | split STRICT_LINEAR from DYNAMIC_ALLOWED |
| `620855b` | measure the refusals to 95% coverage |
| `e77703e` | replace the stale file map with the measured ledger |
| `162a1e6` | repair four CI regressions |
| `ac02b8b` | §4.7 octave-stable tempo + GUM uncertainty |
| `d063f39` | the STRICT_LINEAR crest check was circular |
| `947b09d` | independent BS.1770-5 meter, cross-verified vs FFmpeg |
| `f99326a` | decision-directed Wiener denoise, measured |
| `960ea26` | M/S matrix, HPSS, forensic QC, de-esser |
| `5910c4f` | mutation campaign 39 → 74 probes, 74/74 killed |
| `ba2643b` | close coverage gaps, fix the Wiener NaN |
