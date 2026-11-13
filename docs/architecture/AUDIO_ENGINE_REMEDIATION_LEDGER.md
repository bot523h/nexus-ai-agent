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

## Remaining work

- B2: spatial rendering/reverberation and speech-restoration kernels.
- B3: sub-frame synchronization, Foley, Bark mastering and pure pack/engine wiring.
- B4: 33 additional deterministic mutation probes, unrounded per-module 95%
  gate, full-repository regression run and final evidence update.
- Production persistence/CommandBus convergence is still owned by the canonical
  Spine/AssetVersion host; an engine-side protocol is not its implementation.
