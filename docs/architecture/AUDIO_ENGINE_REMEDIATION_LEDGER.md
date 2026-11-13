# OMEGA audio-engine remediation ledger

This is measured execution evidence, not a claim that the entire NEXUS Spine
or Stage-B is closed. The bootstrap and standing prohibitions remain in
[OMEGA_SESSION_HANDOFF.md](OMEGA_SESSION_HANDOFF.md).

## Baseline and scope (2026-11-13)

- Session branch: `arena/5a8952b7-nexus-ai-agent` (unchanged).
- Imported the exact Stage-A commit `a24a1bc6e3c530064ca980701f58ad317e164f25`
  by fast-forward from `c3aa7c85e4b64400cb8d51b0880ef5e6cafd4e1b` after reading
  the full handoff. HEAD and a clean tree were checked before implementation.
- Live GitHub run `43413790414` on that baseline was `success`. PR #337
  remains open and unmerged. That CI evidence belongs to Stage-A, not Stage-B.
- No new edits to other agents' zones or the coordination board. The read-only
  multisource referee checked 56 readable sources and returned exit 0.
- Python 3.11.2, NumPy 2.4.6, Ruff **0.16.8**, bundled FFmpeg
  `n9.0.2-17-g2a571b6068-20260930`. Python 3.10 is **not locally verified**.
  Poetry is not installed; gates use the equivalent editable-install `.venv`
  Python commands. No external AI service/model is used by the audio work.

## Wave B1 — audit debt and fenced lineage adapter

| Item | Executable result / limit |
|---|---|
| Shadowed content-limit handler | Specific subclass caught first; both refusal paths tested. `workspace.py` 100% lines. |
| Duration extraction | Optional primary `ffprobe` JSON; rational ticks, decimal stream seconds, key-value and case-insensitive clock fallback. An installed probe returning invalid evidence refuses rather than hiding it with a banner. |
| R128 extraction | Legacy scoped sections plus alternate unit-bearing labels, scanner JSON and `lavfi.r128.*` metadata. Linear metadata peak converted to dB. Last programme wins; LRA gate cannot overwrite I gate; missing I/TP refuses. |
| Slideshow decoder | Coverage exposed a real 24-bit sign-extension/scale defect; fixed against actual integer PCM endpoints, not an encode/decode round-trip sharing the same bug. |
| Slideshow tempo | A coarse autocorrelation bin could lie outside the requested search window; final interpolated period now respects that window. |
| Spine adapter | `spine_lineage.py`: immutable revision/attempt/token/recipe/input/output/passport/measurement record; signed canonical passport stored verbatim; held host fence across check, atomic publication and lineage append. |
| Adversarial publication | Stale token, staged-byte tampering after verification, failed canonical `verify_passport`, invalid renderer paths, cancellation and writer failure all refuse. No final file is created for verification/fence/tamper refusals. |
| Authority boundary | No second database/queue/passport implementation. Host supplies the canonical held fence and append-only AssetVersion writer. This is **engine-side adapter verified**, not production DB/CommandBus wiring closure. Crash recovery remains the canonical queue's responsibility. |

Commands use `.venv/bin/python`:

```sh
-m coverage run --data-file=ci-artifacts/omega-b1.coverage \
  --source=nexus_ai_agent.creative.audio_engine,nexus_ai_agent.creative.slideshow.audio \
  -m pytest tests/unit/test_omega*.py tests/unit/test_audio_engine_*.py \
  tests/unit/test_slideshow*.py tests/unit/test_audio_pack*.py \
  tests/architecture/test_audio_pack_boundary.py -q
-m mypy src
-m mypy --strict src/nexus_ai_agent/creative/audio_engine
-m ruff check src tests scripts
-m ruff format --check src tests scripts
scripts/mutate_omega_audio.py
```

Final expanded focused run: **839 passed, 1 skipped** (99.20 seconds).
`slideshow/audio.py`: **391/399 lines = 97.99%**; `spine_lineage.py`:
**126/126 = 100%**; `workspace.py`: **141/141 = 100%**; `ffmpeg_ops.py`:
**436/451 = 96.67%**. Every engine module is above the unrounded 95% bar.
Stage-A's displayed `95%` for continuity was actually **94.9686%**; the
basename regression makes it **152/159 = 95.60%** without weakening a threshold.
Docs/truth regression: **165 passed**. Both mypy modes and repository Ruff
checks/format checks pass. The clean-tree pack gate runs after the checkpoint
commit, as required; its result is recorded in the next checkpoint.

The first post-format mutation run found **76/77**, not green: the optional
stream-duration refusal masked the removed required-shape check. Required
codec/sample-format access now precedes duration alternatives; the original
malformed-shape test kills that mutation again. Final post-format campaign:
**77/77 KILLED, 0 SURVIVED**. No mutation anchor or acceptance gate was weakened.

B1 checkpoint: `76574db1c9540d63105cf2fb7c3d3de896930956`, pushed on the
session branch. Its **clean-tree** `scripts/pack_coverage.py` verdict was
**ACCEPTED** (audio 939/943 = 99.58%; all packs 5623/5766 = 97.52%).
That verdict is commit-specific, not evidence for later uncommitted work.

## Wave B2 — K1 spatial acoustics and K2 classical voice restoration

| Kernel | Implemented contract / independently falsifiable evidence |
|---|---|
| K1 encoding | Real ACN/SN3D order 0–3; right-handed X-front/Y-left/Z-up; FOA `[W,Y,Z,X]`. Per-degree spherical quadrature and orthogonal SO(3) action; directional, inverse and group-law tests, not just an energy normalization. |
| K1 binaural | Virtual-speaker spherical-head ITD, causal Lagrange fractional delay, frequency-dependent head shadow and elevation pinna notch; `(2,N)` PCM. Analytic approximation, **not measured/personalized HRTFs**. |
| K1 FDN | Eight distinct prime delays, Householder unitary mixing, passive bilinear low/mid/high damping; room volume/surface and Eyring absorption. Causal octave-band Schroeder RT60 measurements **0.7640719 / 0.5787025 / 0.4083715 s** for targets **0.8 / 0.6 / 0.4 s**, each within 8%. |
| K2 WOLA/LSA | Exact weighted overlap-add, including endpoints; decision-directed prior SNR, quiet-bin noise PSD tracking, Ephraim–Malah log-spectral amplitude and gain floor. Independent E1 tables agree within `3e-13` relative; the estimator is tested to differ from Wiener. |
| K2 declipping | Bandlimited trigonometric POCS; reliable samples remain bit-exact and positive/negative clipped samples retain their signed threshold inequalities. No output normalization can undo the constraints. |
| K2 WPE | Delayed multichannel, multi-tap weighted prediction with regularized solves; delay at least 35 ms and first 35 ms of PCM preserved. Short actual recordings bypass training; reflection padding is not additional evidence. A stationary-noise 48/96 ms late-echo fixture gains **4.4681 dB** SNR; this is not a natural-speech performance guarantee. |
| B1 tier correction | A target-free canonical passport does not run `verify_passport`'s target meter comparison. The publication adapter now independently decodes/meters private bytes even without a loudness target before earning `SIGNED_CROSS_VERIFIED_PRIVATE_BYTES`. Missing/disagreeing PCM refuses with no public bytes. |

### Bounded synthetic restoration benchmark

Three-second 16 kHz AM harmonic voiced surrogate (180/540/900 Hz), additive
noise at **5.0 dB before clipping**, exactly **15%** samples hard clipped.
The declipper uses an explicit 1800 Hz bandlimit prior; the denoiser receives a
noise-only 0.5 s reference, **never the clean signal**. Clean PCM is used only
by the separate evaluator. Seeds 42/43/44 gain **9.9391 / 9.9044 / 9.8989 dB**
SNR over the noisy clipped input and recover a crest closer to the reference.
Seed 42 reference/input/output crest: **8.9846 / 4.6812 / 9.9482 dB**.

Monotonic evidence is the **LSA strength sweep after declipping**, not a claim
that each algorithmic stage helps every metric. Seed 42 input envelope
correlation is **0.9917474**; strengths 0.25/0.5/0.75/1.0 produce
**0.9925264 / 0.9940996 / 0.9958081 / 0.9968098**. Declipping alone slightly
reduces this metric (**0.9910693**); it is not concealed as an improvement.
No natural-speech corpus, intelligibility score or listening validation is claimed.

`tests/integration/test_audio_engine_stage_b_benchmarks.py` reruns fixed input
**N=3**, emits JSON and asserts the numerical bounds. Observed standalone
medians/spreads: FDN **1.1263 / 0.1018 s**, restoration **0.2712 / 0.0606 s**.
Peak RSS **75,464,704 bytes** is Linux `ru_maxrss` for the **whole benchmark
process including imports/all kernels**, not per-kernel or isolated-worker RSS.
Rotation norm error **5.68e-14**. Timings are observations, not a real-time SLA.

Reproduce (each command uses `.venv/bin/python`):

```sh
-m coverage run --data-file=ci-artifacts/omega-b2-combined.coverage \
  --source=src/nexus_ai_agent/creative/audio_engine,nexus_ai_agent.creative.slideshow.audio \
  -m pytest tests/unit/test_omega*.py tests/unit/test_audio_engine_*.py \
  tests/unit/test_slideshow*.py tests/unit/test_audio_pack*.py \
  tests/integration/test_audio_engine_stage_b_benchmarks.py \
  tests/architecture/test_audio_pack_boundary.py -q
-m coverage json --data-file=ci-artifacts/omega-b2-combined.coverage \
  -o ci-artifacts/omega-b2-coverage.json
 tests/integration/test_audio_engine_stage_b_benchmarks.py
 scripts/mutate_omega_audio.py
-m mypy src
-m mypy --strict src/nexus_ai_agent/creative/audio_engine
-m ruff check src tests scripts
-m ruff format --check src tests scripts
```

Final combined run: **892 passed, 1 skipped** (106.76 s). K1 **234/234**, K2
**243/243**, lineage **130/130** lines covered; every engine module and slideshow
`audio.py` pass the **unrounded per-module 95%** check. Slideshow **391/399**.
Global/strict mypy: **382 / 18 files**, repository Ruff format **826 files**.
Post-source-format mutation campaign **77/77 KILLED**. Read-only multisource
ownership check: **56 readable sources, no overlap**. Raw logs, coverage JSON
and benchmark JSON are ignored under `ci-artifacts/omega-b2-*`.

B2 checkpoint: `e6880216069f4c873489cd7276315342011a66c4`, pushed on the
session branch. Its clean-tree pack gate was **ACCEPTED** (audio 99.58%, total
97.52%, 33 canonical test modules). B3's new pack surface requires a new gate;
that B2 result is not reused as proof for the new operations.

## Wave B3 — K3 synchronization/Foley, K4 Bark mastering and pack reachability

| Surface | Executed contract / limits |
|---|---|
| GCC-PHAT | Full linear FFT padding, spectral-zero refusal, 32x correlation grid and parabolic sub-bin interpolation; signed audio-minus-picture lag. Endpoint baseline removal avoids a shared, mean-subtraction-induced rectangular edge falsely dominating sparse feature correlation. |
| Visual alignment | Timestamped cubic-Hermite frame-to-envelope interpolation, tested at 24, 30000/1001 and 60 fps; fractional zero-extended sinc correction, followed by a new residual measurement on corrected PCM in the default RMS path. No claim to reconstruct events above visual Nyquist. |
| ITU window | Explicit asymmetric **[-15,+5] ms** decision, inclusive boundaries. Correction targets zero, even for an offset already inside the window; residuals must be <=0.5 ms. |
| Modal Foley | Wood, metal, glass, stone, leather-footstep and cloth-rustle modal presets, positive damping, force/contact excitation, impulse linearity, deterministic friction seed. Exact integer collision samples; overlap-add and tail truncation only, no peak normalization. Relative acoustic units, not calibrated SPL. |
| Bark model | 24 Zwicker critical bands partition every FFT bin; asymmetric **+25 dB/Bark** lower-side and level-dependent **-27..-10 dB/Bark** upper-side spreading. Explicit assumed full-scale SPL calibration (100 dB default), not an inferred hearing threshold. |
| Speech priority | +6 dB speech/masker target; only conflicting music/SFX **800..4500 Hz** bins can be carved. Dialogue unchanged; linked stereo gains. **Coherent** music+SFX power is used: summing their separate powers understated correlated maskers and failed an independent waveform test. |
| Protected bands | Bass <250 Hz and air >6 kHz preserved at the carving stage. Global final loudness gain/limiting naturally affects the complete master. A protected out-of-band masker can make +6 dB infeasible; the result explicitly reports `margin_reached=False`. |
| Three-stem delivery | Dialogue/music/SFX-Foley master to **-24 LKFS +/-0.1 LU**, true peak **<=-1 dBTP**. Channel-linked lookahead limiter only when needed; iterative actual BS.1770 measurements and refusal on silence/nonconvergence. No target number is copied into a measurement. |
| Reachability | Four typed operations: `audio.spatialize`, `audio.restore_voice`, `audio.sync_foley`, `audio.bark_master`; registered in engine health, package exports, pure pack registry and data-only manifest. Real CommandBus dispatches prove all four produce measured artifacts. No DSP imports/I/O in the pack. |
| Private verification | Canonicalize/hash all primary and auxiliary inputs privately; all hashes bind recipe reuse. Signed canonical `verify_passport` and independent NumPy metering before publication. K4's signed target check explicitly uses **0.1 LU / 0 dB peak tolerances**, not the legacy broader defaults. |
| Bounded execution | Whole-signal inputs limited to **min(10 seconds, configured analysis bound)**; oversized/unknown-duration inputs refuse rather than truncate. A deployment signing key/id is required. Cancellation is cooperative at kernel boundaries, not mid-NumPy preemption. Replay verifies existing bytes but does not invent unperformed kernel remeasurements. |

### Measured B3 benchmark and regression

The same standalone N=3 benchmark now includes K3/K4. At 24/29.970/60 fps,
absolute offset errors were **0.000195 / 0.000150 / 0.000187 ms**; freshly
measured corrected-PCM residuals **-0.07177 / -0.07175 / +0.03624 ms**.
All six Foley first-active indices were exactly the requested **sample 123**.
The synthetic master measured **-24.00000 LKFS**, **-15.92052 dBTP**; minimum
active speech/masker margin **5.99964 dB**. Protected 160/7000 Hz music tone
changes before mastering were **-0.0000677 / +0.00000762 dB**.

N=3 timing medians/spreads (seconds): FDN **1.13020 / 0.07264**; restoration
**0.27347 / 0.02827**; sync **0.01035 / 0.00125**; six-material Foley
**0.00695 / 0.00080**; Bark master **0.03580 / 0.00383**. Whole benchmark-process
peak RSS **77,180,928 bytes**, including imports and every kernel, not isolated
per-kernel memory. These are bounded synthetic/offline observations, not a
real-time SLA or natural-speech/picture-understanding/listening guarantee.

Reproduce B3 with the B2 combined coverage command, adding
`tests/unit/test_opgap_wave5.py` and replacing the single integration target
with `tests/integration/test_audio_engine_*.py`. Final: **1014 passed, 1 skipped**
(128.26 s). K1 **234/234**, K2 **243/243**, K3 **168/168**, K4 **142/142**;
execution **448/458 = 97.82%**; slideshow **391/399**; every engine module passes
95% **unrounded**. Global/strict mypy **384 / 20 files**; Ruff **831 files**.
Docs/truth/activation regression **155 passed**; canonical truth generator and
`--check` green. Logs/JSON: `ci-artifacts/omega-b3-*`.

The user's engineering-choice authorization permits the minimal compatibility
extension: the exact ten-operation inventory assertion now expects fourteen;
the new pack-importing integration suite is in canonical `PACK_TEST_TARGETS`.
Only that audio target list changed in the coverage harness; no provenance or
threshold policy was weakened. Operation-truth artifacts were refreshed solely
by `python -m nexus_ai_agent.nagar --docs`; no proof registry entry was invented.

Mutation audit found that the inherited loudnorm-rate mutant contained an
unclosed parenthesis. Historical 77/77 logs used the old runner's "any nonzero"
classification and thus included that collection error. The replacement is now
syntactically valid (`return ()`); the unchanged rate-guard test kills the
semantic defect. The post-format inherited campaign again reports **77/77**.
B4 must additionally establish clean controls and distinguish real test-call
failures from collection/setup errors; the historical runner is not sufficient
for final acceptance.

## Remaining work

- B4: 33 additional deterministic mutation probes (110 total), a controlled
  mutation runner, full-repository regression and final clean-tree evidence.
- Production persistence/CommandBus convergence is still owned by the canonical
  Spine/AssetVersion host; an engine-side protocol is not its implementation.
