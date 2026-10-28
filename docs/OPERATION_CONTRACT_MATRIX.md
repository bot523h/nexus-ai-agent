# Operation Contract Matrix & Truth Table

**Mission:** Agent B — Operation Truth & Wave Planning Mission  
**Status:** Canonical Living Truth Matrix  
**Anchor Commit:** `035a896` · **Date:** 2026-09-24  

---

## 1. Overview and Invariants

This matrix provides an immutable, reproducible truth table for all 80 operations tracked across the Nagar creative studio architecture.
It separates the system into three distinct architectural layers:
1. **Product Catalog (70 Operations)**: Specified in `docs/NAGAR_70_OPERATIONS_TDD.md`.
2. **Runtime Registry (57 Operations)**: Loaded dynamically in `nexus_ai_agent.creative.packs.runtime.build_runtime_registry()`.
3. **Executable Surface (9 Operations)**: Reachable via Telegram commands and job queues (`/edit`, `/caption`, `/grade`, `/slideshow`).

### Legend
- **P-Def**: Product Catalog Defined (Yes/No)
- **Reg**: Registered in CapabilityRegistry (Yes/No)
- **Dom**: Domain Reducer Implemented (Yes/No)
- **Exec**: Pure Executor Function Ready (Yes/No)
- **Surf**: Surface Mapped to End-User (Yes/No)
- **Cmd**: Typed Command Envelope Contracted (Yes/No)
- **Tst**: Tested in Suite (Yes/No)
- **Prv**: Runtime Proven (Yes/No)
- **Class**: Evidence Class (`VERIFIED` / `MISSING` / `INFERRED` / `BLOCKED`)
- **Mat**: Maturity Level (`L0` to `L4`)

---

## 2. Complete Operation Truth Table (80 Operations)

| Operation ID | Cat | P-Def | Reg | Dom | Exec | Surf | Cmd | Tst | Prv | Class | Mat | Owner | Next Action |
|---|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|---|
| `audio.align_music` | audio | Y | Y | Y | Y | N | Y | Y | Y | VERIFIED | L3 | Agent B (Creative) | Expose via Telegram/UI surface when scheduled |
| `audio.beat_sync_cut` | audio | Y | Y | Y | Y | N | Y | Y | Y | VERIFIED | L3 | Agent B (Creative) | Expose via Telegram/UI surface when scheduled |
| `audio.deess` | audio | Y | Y | Y | Y | N | Y | Y | Y | VERIFIED | L3 | Agent B (Creative) | Expose via Telegram/UI surface when scheduled |
| `audio.detect_beats` | audio | Y | Y | Y | Y | N | Y | Y | Y | VERIFIED | L3 | Agent B (Creative) | Expose via Telegram/UI surface when scheduled |
| `audio.duck_music` | audio | Y | Y | Y | Y | N | Y | Y | Y | VERIFIED | L3 | Agent B (Creative) | Expose via Telegram/UI surface when scheduled |
| `audio.eq_voice` | audio | Y | Y | Y | Y | N | Y | Y | Y | VERIFIED | L3 | Agent B (Creative) | Expose via Telegram/UI surface when scheduled |
| `audio.normalize_loudness` | audio | Y | Y | Y | Y | N | Y | Y | Y | VERIFIED | L3 | Agent B (Creative) | Expose via Telegram/UI surface when scheduled |
| `audio.remove_noise` | audio | Y | Y | Y | Y | N | Y | Y | Y | VERIFIED | L3 | Agent B (Creative) | Expose via Telegram/UI surface when scheduled |
| `audio.remove_vocal` | audio | Y | Y | Y | Y | N | Y | Y | Y | VERIFIED | L3 | Agent B (Creative) | Expose via Telegram/UI surface when scheduled |
| `audio.time_stretch` | audio | Y | Y | Y | Y | N | Y | Y | Y | VERIFIED | L3 | Agent B (Creative) | Expose via Telegram/UI surface when scheduled |
| `caption.align_words` | caption | Y | Y | Y | Y | N | Y | Y | Y | VERIFIED | L3 | Agent B (Creative) | Expose via Telegram/UI surface when scheduled |
| `caption.burn_in` | caption | Y | Y | Y | Y | N | Y | Y | Y | VERIFIED | L3 | Agent B (Creative) | Expose via Telegram/UI surface when scheduled |
| `caption.diarize` | caption | Y | Y | Y | Y | N | Y | Y | Y | VERIFIED | L3 | Agent B (Creative) | Expose via Telegram/UI surface when scheduled |
| `caption.generate_ass_rtl` | caption | Y | Y | Y | Y | N | Y | Y | Y | VERIFIED | L3 | Agent B (Creative) | Expose via Telegram/UI surface when scheduled |
| `caption.generate_srt` | caption | Y | Y | Y | Y | N | Y | Y | Y | VERIFIED | L3 | Agent B (Creative) | Expose via Telegram/UI surface when scheduled |
| `caption.highlight_words` | caption | Y | Y | Y | Y | N | Y | Y | Y | VERIFIED | L3 | Agent B (Creative) | Expose via Telegram/UI surface when scheduled |
| `caption.search_transcript` | caption | Y | Y | Y | Y | N | Y | Y | Y | VERIFIED | L3 | Agent B (Creative) | Expose via Telegram/UI surface when scheduled |
| `caption.style_vazirmatn` | caption | Y | Y | Y | Y | N | Y | Y | Y | VERIFIED | L3 | Agent B (Creative) | Expose via Telegram/UI surface when scheduled |
| `caption.transcribe` | caption | Y | Y | Y | Y | Y | Y | Y | Y | VERIFIED | L4 | Agent B (Creative) | Production monitoring & regression guard |
| `caption.translate_local` | caption | Y | Y | Y | Y | N | Y | Y | Y | VERIFIED | L3 | Agent B (Creative) | Expose via Telegram/UI surface when scheduled |
| `color.adjust_exposure` | color | Y | Y | Y | Y | Y | Y | Y | Y | VERIFIED | L4 | Agent B (Creative) | Production monitoring & regression guard |
| `color.apply_lut` | color | Y | Y | Y | Y | N | Y | Y | Y | VERIFIED | L3 | Agent B (Creative) | Expose via Telegram/UI surface when scheduled |
| `color.auto_balance` | color | Y | Y | Y | Y | N | Y | Y | Y | VERIFIED | L3 | Agent B (Creative) | Expose via Telegram/UI surface when scheduled |
| `color.deband_denoise` | color | Y | N | N | N | N | N | N | N | MISSING | L0 | Agent 1 (Runtime) | Implement models + reducer in pack |
| `color.hdr_tonemap` | color | Y | N | N | N | N | N | N | N | MISSING | L0 | Agent 1 (Runtime) | Implement models + reducer in pack |
| `color.match_shot` | color | Y | Y | Y | Y | N | Y | Y | Y | VERIFIED | L3 | Agent B (Creative) | Expose via Telegram/UI surface when scheduled |
| `color.white_balance` | color | Y | N | N | N | N | N | N | N | MISSING | L0 | Agent 1 (Runtime) | Implement models + reducer in pack |
| `delivery.export_otio` | delivery | Y | Y | Y | Y | Y | Y | Y | Y | VERIFIED | L4 | Agent B (Creative) | Production monitoring & regression guard |
| `delivery.make_proxy_480p` | delivery | Y | Y | Y | Y | Y | Y | Y | Y | VERIFIED | L4 | Agent B (Creative) | Production monitoring & regression guard |
| `delivery.render_master_4k` | delivery | Y | Y | Y | Y | N | Y | Y | Y | VERIFIED | L3 | Agent B (Creative) | Expose via Telegram/UI surface when scheduled |
| `media.pause` | media | N | Y | Y | Y | N | Y | Y | Y | VERIFIED | L3 | Agent B (Creative) | Expose via Telegram/UI surface when scheduled |
| `media.play` | media | N | Y | Y | Y | N | Y | Y | Y | VERIFIED | L3 | Agent B (Creative) | Expose via Telegram/UI surface when scheduled |
| `motion.add_glow` | motion | Y | Y | Y | Y | N | Y | Y | Y | VERIFIED | L3 | Agent B (Creative) | Expose via Telegram/UI surface when scheduled |
| `motion.add_motion_blur` | motion | Y | Y | Y | Y | N | Y | Y | Y | VERIFIED | L3 | Agent B (Creative) | Expose via Telegram/UI surface when scheduled |
| `motion.add_parallax` | motion | Y | Y | Y | Y | N | Y | Y | Y | VERIFIED | L3 | Agent B (Creative) | Expose via Telegram/UI surface when scheduled |
| `motion.add_particles` | motion | Y | Y | Y | Y | N | Y | Y | Y | VERIFIED | L3 | Agent B (Creative) | Expose via Telegram/UI surface when scheduled |
| `motion.add_title` | motion | Y | Y | Y | Y | N | Y | Y | Y | VERIFIED | L3 | Agent B (Creative) | Expose via Telegram/UI surface when scheduled |
| `motion.add_transition` | motion | Y | Y | Y | Y | N | Y | Y | Y | VERIFIED | L3 | Agent B (Creative) | Expose via Telegram/UI surface when scheduled |
| `motion.apply_mask` | motion | Y | Y | Y | Y | N | Y | Y | Y | VERIFIED | L3 | Agent B (Creative) | Expose via Telegram/UI surface when scheduled |
| `motion.keyframe_transform` | motion | Y | Y | Y | Y | N | Y | Y | Y | VERIFIED | L3 | Agent B (Creative) | Expose via Telegram/UI surface when scheduled |
| `motion.stabilize` | motion | Y | Y | Y | Y | N | Y | Y | Y | VERIFIED | L3 | Agent B (Creative) | Expose via Telegram/UI surface when scheduled |
| `motion.warp` | motion | Y | Y | Y | Y | N | Y | Y | Y | VERIFIED | L3 | Agent B (Creative) | Expose via Telegram/UI surface when scheduled |
| `portrait.background_blur` | portrait | Y | N | N | N | N | N | N | N | MISSING | L0 | Agent 1 (Runtime) | Implement models + reducer in pack |
| `portrait.correct_gaze` | portrait | Y | N | N | N | N | N | N | N | MISSING | L0 | Agent 1 (Runtime) | Implement models + reducer in pack |
| `portrait.detect_landmarks` | portrait | Y | N | N | N | N | N | N | N | MISSING | L0 | Agent 1 (Runtime) | Implement models + reducer in pack |
| `portrait.enhance_eyes` | portrait | Y | N | N | N | N | N | N | N | MISSING | L0 | Agent 1 (Runtime) | Implement models + reducer in pack |
| `portrait.mask_hair` | portrait | Y | N | N | N | N | N | N | N | MISSING | L0 | Agent 1 (Runtime) | Implement models + reducer in pack |
| `portrait.relight_face` | portrait | Y | N | N | N | N | N | N | N | MISSING | L0 | Agent 1 (Runtime) | Implement models + reducer in pack |
| `portrait.retouch_blemish` | portrait | Y | N | N | N | N | N | N | N | MISSING | L0 | Agent 1 (Runtime) | Implement models + reducer in pack |
| `portrait.smooth_skin` | portrait | Y | N | N | N | N | N | N | N | MISSING | L0 | Agent 1 (Runtime) | Implement models + reducer in pack |
| `portrait.stabilize_face` | portrait | Y | N | N | N | N | N | N | N | MISSING | L0 | Agent 1 (Runtime) | Implement models + reducer in pack |
| `portrait.whiten_teeth` | portrait | Y | N | N | N | N | N | N | N | MISSING | L0 | Agent 1 (Runtime) | Implement models + reducer in pack |
| `scene.auto_reframe_subject` | scene | Y | N | N | N | N | N | N | N | MISSING | L0 | Agent 1 (Runtime) | Implement models + reducer in pack |
| `scene.detect_shot_boundaries` | scene | Y | N | N | N | N | N | N | N | MISSING | L0 | Agent 1 (Runtime) | Implement models + reducer in pack |
| `scene.find_subject_moment` | scene | Y | N | N | N | N | N | N | N | MISSING | L0 | Agent 1 (Runtime) | Implement models + reducer in pack |
| `scene.remove_background` | scene | Y | N | N | N | N | N | N | N | MISSING | L0 | Agent 1 (Runtime) | Implement models + reducer in pack |
| `scene.remove_logo` | scene | Y | N | N | N | N | N | N | N | MISSING | L0 | Agent 1 (Runtime) | Implement models + reducer in pack |
| `scene.remove_object` | scene | Y | N | N | N | N | N | N | N | MISSING | L0 | Agent 1 (Runtime) | Implement models + reducer in pack |
| `scene.replace_sky` | scene | Y | N | N | N | N | N | N | N | MISSING | L0 | Agent 1 (Runtime) | Implement models + reducer in pack |
| `scene.segment_subject` | scene | Y | N | N | N | N | N | N | N | MISSING | L0 | Agent 1 (Runtime) | Implement models + reducer in pack |
| `scene.track_face` | scene | Y | N | N | N | N | N | N | N | MISSING | L0 | Agent 1 (Runtime) | Implement models + reducer in pack |
| `scene.track_object` | scene | Y | N | N | N | N | N | N | N | MISSING | L0 | Agent 1 (Runtime) | Implement models + reducer in pack |
| `slideshow.compose` | slideshow | N | Y | Y | Y | Y | Y | Y | Y | VERIFIED | L4 | Agent B (Creative) | Production monitoring & regression guard |
| `slideshow.render` | slideshow | N | Y | Y | Y | Y | Y | Y | Y | VERIFIED | L4 | Agent B (Creative) | Production monitoring & regression guard |
| `slideshow.scan_assets` | slideshow | N | Y | Y | Y | N | Y | Y | Y | VERIFIED | L3 | Agent B (Creative) | Expose via Telegram/UI surface when scheduled |
| `slideshow.score_images` | slideshow | N | Y | Y | Y | N | Y | Y | Y | VERIFIED | L3 | Agent B (Creative) | Expose via Telegram/UI surface when scheduled |
| `slideshow.suggest_tone` | slideshow | N | Y | Y | Y | N | Y | Y | Y | VERIFIED | L3 | Agent B (Creative) | Expose via Telegram/UI surface when scheduled |
| `slideshow.upscale` | slideshow | N | Y | Y | Y | N | Y | Y | Y | VERIFIED | L3 | Agent B (Creative) | Expose via Telegram/UI surface when scheduled |
| `system.undo` | system | N | Y | Y | Y | N | Y | Y | Y | VERIFIED | L3 | Agent B (Creative) | Expose via Telegram/UI surface when scheduled |
| `timeline.attach_b_roll` | timeline | Y | Y | Y | Y | N | Y | Y | Y | VERIFIED | L3 | Agent B (Creative) | Expose via Telegram/UI surface when scheduled |
| `timeline.freeze_frame` | timeline | Y | Y | Y | Y | N | Y | Y | Y | VERIFIED | L3 | Agent B (Creative) | Expose via Telegram/UI surface when scheduled |
| `timeline.insert_gap` | timeline | Y | Y | Y | Y | N | Y | Y | Y | VERIFIED | L3 | Agent B (Creative) | Expose via Telegram/UI surface when scheduled |
| `timeline.mark` | timeline | N | Y | Y | Y | N | Y | Y | Y | VERIFIED | L3 | Agent B (Creative) | Expose via Telegram/UI surface when scheduled |
| `timeline.retime_to_music` | timeline | Y | Y | Y | Y | N | Y | Y | Y | VERIFIED | L3 | Agent B (Creative) | Expose via Telegram/UI surface when scheduled |
| `timeline.reverse_segment` | timeline | Y | Y | Y | Y | Y | Y | Y | Y | VERIFIED | L4 | Agent B (Creative) | Production monitoring & regression guard |
| `timeline.ripple_delete` | timeline | Y | Y | Y | Y | N | Y | Y | Y | VERIFIED | L3 | Agent B (Creative) | Expose via Telegram/UI surface when scheduled |
| `timeline.speed_ramp` | timeline | Y | Y | Y | Y | Y | Y | Y | Y | VERIFIED | L4 | Agent B (Creative) | Production monitoring & regression guard |
| `timeline.split_at_playhead` | timeline | Y | Y | Y | Y | N | Y | Y | Y | VERIFIED | L3 | Agent B (Creative) | Expose via Telegram/UI surface when scheduled |
| `timeline.sync_multicam` | timeline | Y | Y | Y | Y | N | Y | Y | Y | VERIFIED | L3 | Agent B (Creative) | Expose via Telegram/UI surface when scheduled |
| `timeline.trim` | timeline | Y | Y | Y | Y | Y | Y | Y | Y | VERIFIED | L4 | Agent B (Creative) | Production monitoring & regression guard |

---

## 3. Wave Planning and Roadmap Recommendations

Based on the objective evidence matrix, capabilities are staged across four waves prioritizing runtime readiness and structural risk over raw feature count.

### Wave 1: Green Cockpit Stabilization (Maturity L4)
- **Scope:** Core Command Registry, CommandBus, student mode, lightweight creative tools, `/slideshow`, `/edit`, `/caption`, `/grade`.
- **Value:** High. Delivers immediate end-to-end user workflows with verifiable rendering.
- **Dependency:** None (already operational in main).
- **Risk:** Low.
- **Runtime Readiness:** 100% (All 9 surface operations verified in CI).
- **Testability:** Fully automated via `test_creative_surface.py`, `test_creative_render_jobs.py`, and `test_slideshow_render.py`.

### Wave 2: Timeline & Color Rendering Maturity (Maturity L3 → L4)
- **Scope:** Expand `timeline.*` (multicam, attach b-roll, ripple delete) and `color.*` (apply LUT, auto balance, match shot). Wire the 3 missing color operations (`color.white_balance`, `color.hdr_tonemap`, `color.deband_denoise`).
- **Value:** High. Provides professional grade NLE capability without UI dependencies.
- **Dependency:** `creative/rendering/ir.py`, FFmpeg filters.
- **Risk:** Medium (FFmpeg binary availability, color matrix math).
- **Runtime Readiness:** High (9 timeline and 4 color ops already L3 in registry).
- **Testability:** Pure mathematical and filtergraph IR tests.

### Wave 3: Audio Studio & Local Speech / Diarization (Maturity L3 → L4)
- **Scope:** Surface exposure of `audio.*` (beat sync, ducking, normalization, vocal separation) and `caption.*` (ASR forced alignment, pyannote diarization, Vazirmatn ASS/RTL styling).
- **Value:** Very High for localization and podcast / dialogue editing.
- **Dependency:** `faster-whisper`, `pyannote-audio`, local DSP SIMD.
- **Risk:** High (Model memory footprint, token gated models).
- **Runtime Readiness:** Medium (Pure reducers L3 proven; requires hardware extra verification in CI).
- **Testability:** Audio wav/pcm test fixtures and deterministic timing comparisons.

### Wave 4: Vision Packs — Portrait & Scene Understanding (Maturity L0 → L3)
- **Scope:** Address the 20 missing vision gaps (10 `portrait.*`, 10 `scene.*`). Implement MediaPipe/ONNX FaceMesh, SAM segmentation, and spatio-temporal inpainting.
- **Value:** High aesthetic value, but computationally intensive.
- **Dependency:** `nexus.vision.portrait`, `nexus.vision.scene`, ONNX Runtime / WebGPU.
- **Risk:** Very High (Memory limits, model licensing, deepfake / privacy considerations requiring Level C gates).
- **Runtime Readiness:** 0% (Currently preserved as `EvidenceClass.MISSING`, `L0`).
- **Testability:** Requires synthetic video fixtures with ground truth IoU and coordinate baselines.
