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

## Remaining work

- B3: sub-frame synchronization, Foley, Bark mastering and pure pack/engine wiring.
- B4: 33 additional deterministic mutation probes, unrounded per-module 95%
  gate, full-repository regression run and final evidence update.
- Production persistence/CommandBus convergence is still owned by the canonical
  Spine/AssetVersion host; an engine-side protocol is not its implementation.
