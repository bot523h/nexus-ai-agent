# Operation Contract Matrix — Canonical 70 vs 57 Runtime

> **Generated:** 2026-09-24 | **Catalog:** 70 | **Runtime:** 57 | **Formula:** `70 - 20 - 3 + 10 = 57`

## Reconciliation Summary

- **70 catalog:** 70
- **57 runtime:** 57
- **Extra outside 70:** 10 — media.pause, media.play, slideshow.compose, slideshow.render, slideshow.scan_assets, slideshow.score_images, slideshow.suggest_tone, slideshow.upscale, system.undo, timeline.mark
- **Missing (23):** color.deband_denoise, color.hdr_tonemap, color.white_balance, portrait.background_blur, portrait.correct_gaze, portrait.detect_landmarks, portrait.enhance_eyes, portrait.mask_hair, portrait.relight_face, portrait.retouch_blemish, portrait.smooth_skin, portrait.stabilize_face, portrait.whiten_teeth, scene.auto_reframe_subject, scene.detect_shot_boundaries, scene.find_subject_moment, scene.remove_background, scene.remove_logo, scene.remove_object, scene.replace_sky, scene.segment_subject, scene.track_face, scene.track_object
  - Portrait+Scene 20: portrait.background_blur, portrait.correct_gaze, portrait.detect_landmarks, portrait.enhance_eyes, portrait.mask_hair, portrait.relight_face, portrait.retouch_blemish, portrait.smooth_skin, portrait.stabilize_face, portrait.whiten_teeth, scene.auto_reframe_subject, scene.detect_shot_boundaries, scene.find_subject_moment, scene.remove_background, scene.remove_logo, scene.remove_object, scene.replace_sky, scene.segment_subject, scene.track_face, scene.track_object
  - Color 3: color.deband_denoise, color.hdr_tonemap, color.white_balance

**Verification:** Product Catalog ≠ Runtime Registry ≠ Executable Surface — proven

## Required Columns (Gate 2)

| Column | Present |
|---|---|
| Product ID | ✅ |
| Canonical Operation Name | ✅ |
| Product Pack | ✅ |
| TDD Exists | ✅ |
| Registry Exists | ✅ |
| Registry ID | ✅ |
| Domain Model Exists | ✅ |
| Reducer Exists | ✅ |
| Executor Exists | ✅ |
| Real Encode / Real Runtime Proof | ✅ |
| Surface Mapping | ✅ |
| Typed Command Exists | ✅ |
| Input Schema | ✅ |
| Output Schema | ✅ |
| Capability ID | ✅ |
| Authorization Required | ✅ |
| Local / Cloud Policy | ✅ |
| Reversible | ✅ |
| Previewable | ✅ |
| Idempotency | ✅ |
| Current L-Level | ✅ |
| Evidence Class | ✅ |
| Evidence Location | ✅ |
| Tests | ✅ |
| Owner | ✅ |
| Notes / Gaps | ✅ |

## Multi-layer Status (separate booleans)

Each operation has independent booleans: PRODUCT_DEFINED, REGISTERED, DOMAIN_REDUCER_READY, EXECUTOR_READY, SURFACE_MAPPED, COMMAND_CONTRACTED, TESTED, PROVEN

Example: PRODUCT_DEFINED=true, REGISTERED=true, DOMAIN_REDUCER_READY=true, EXECUTOR_READY=false → registered but not executable.

## Full Matrix (80 rows = 70 + 10 extra)

| Product ID | Operation | Pack | TDD | Registry | L-Level | Evidence | Notes |
|---|---|---|---|---|---|---|---|
| T01 | timeline.split_at_playhead | nexus.edit.timeline | True | True | L4 | VERIFIED |  |
| T02 | timeline.trim | nexus.edit.timeline | True | True | L3 | VERIFIED |  |
| T03 | timeline.ripple_delete | nexus.edit.timeline | True | True | L3 | VERIFIED |  |
| T04 | timeline.insert_gap | nexus.edit.timeline | True | True | L3 | VERIFIED |  |
| T05 | timeline.speed_ramp | nexus.edit.timeline | True | True | L3 | VERIFIED |  |
| T06 | timeline.reverse_segment | nexus.edit.timeline | True | True | L3 | VERIFIED |  |
| T07 | timeline.freeze_frame | nexus.edit.timeline | True | True | L3 | VERIFIED |  |
| T08 | timeline.attach_b_roll | nexus.edit.timeline | True | True | L3 | VERIFIED |  |
| T09 | timeline.sync_multicam | nexus.edit.timeline | True | True | L3 | VERIFIED |  |
| T10 | timeline.retime_to_music | nexus.edit.timeline | True | True | L3 | VERIFIED |  |
| T11 | portrait.detect_landmarks | nexus.vision.portrait | True | False | L0 | MISSING | MISSING in runtime 57 — portrait.detect_landmarks defined in TDD but not registe |
| T12 | portrait.smooth_skin | nexus.vision.portrait | True | False | L0 | MISSING | MISSING in runtime 57 — portrait.smooth_skin defined in TDD but not registered |
| T13 | portrait.retouch_blemish | nexus.vision.portrait | True | False | L0 | MISSING | MISSING in runtime 57 — portrait.retouch_blemish defined in TDD but not register |
| T14 | portrait.relight_face | nexus.vision.portrait | True | False | L0 | MISSING | MISSING in runtime 57 — portrait.relight_face defined in TDD but not registered |
| T15 | portrait.whiten_teeth | nexus.vision.portrait | True | False | L0 | MISSING | MISSING in runtime 57 — portrait.whiten_teeth defined in TDD but not registered |
| T16 | portrait.correct_gaze | nexus.vision.portrait | True | False | L0 | MISSING | MISSING in runtime 57 — portrait.correct_gaze defined in TDD but not registered |
| T17 | portrait.enhance_eyes | nexus.vision.portrait | True | False | L0 | MISSING | MISSING in runtime 57 — portrait.enhance_eyes defined in TDD but not registered |
| T18 | portrait.mask_hair | nexus.vision.portrait | True | False | L0 | MISSING | MISSING in runtime 57 — portrait.mask_hair defined in TDD but not registered |
| T19 | portrait.background_blur | nexus.vision.portrait | True | False | L0 | MISSING | MISSING in runtime 57 — portrait.background_blur defined in TDD but not register |
| T20 | portrait.stabilize_face | nexus.vision.portrait | True | False | L0 | MISSING | MISSING in runtime 57 — portrait.stabilize_face defined in TDD but not registere |
| T21 | scene.segment_subject | nexus.vision.scene | True | False | L0 | MISSING | MISSING in runtime 57 — scene.segment_subject defined in TDD but not registered |
| T22 | scene.remove_object | nexus.vision.scene | True | False | L0 | MISSING | MISSING in runtime 57 — scene.remove_object defined in TDD but not registered |
| T23 | scene.replace_sky | nexus.vision.scene | True | False | L0 | MISSING | MISSING in runtime 57 — scene.replace_sky defined in TDD but not registered |
| T24 | scene.remove_background | nexus.vision.scene | True | False | L0 | MISSING | MISSING in runtime 57 — scene.remove_background defined in TDD but not registere |
| T25 | scene.track_object | nexus.vision.scene | True | False | L0 | MISSING | MISSING in runtime 57 — scene.track_object defined in TDD but not registered |
| T26 | scene.track_face | nexus.vision.scene | True | False | L0 | MISSING | MISSING in runtime 57 — scene.track_face defined in TDD but not registered |
| T27 | scene.detect_shot_boundaries | nexus.vision.scene | True | False | L0 | MISSING | MISSING in runtime 57 — scene.detect_shot_boundaries defined in TDD but not regi |
| T28 | scene.find_subject_moment | nexus.vision.scene | True | False | L0 | MISSING | MISSING in runtime 57 — scene.find_subject_moment defined in TDD but not registe |
| T29 | scene.remove_logo | nexus.vision.scene | True | False | L0 | MISSING | MISSING in runtime 57 — scene.remove_logo defined in TDD but not registered |
| T30 | scene.auto_reframe_subject | nexus.vision.scene | True | False | L0 | MISSING | MISSING in runtime 57 — scene.auto_reframe_subject defined in TDD but not regist |
| T31 | motion.add_transition | nexus.motion.graphics | True | True | L3 | VERIFIED |  |
| T32 | motion.keyframe_transform | nexus.motion.graphics | True | True | L3 | VERIFIED |  |
| T33 | motion.add_parallax | nexus.motion.graphics | True | True | L3 | VERIFIED |  |
| T34 | motion.apply_mask | nexus.motion.graphics | True | True | L3 | VERIFIED |  |
| T35 | motion.add_glow | nexus.motion.graphics | True | True | L3 | VERIFIED |  |
| T36 | motion.add_motion_blur | nexus.motion.graphics | True | True | L3 | VERIFIED |  |
| T37 | motion.stabilize | nexus.motion.graphics | True | True | L3 | VERIFIED |  |
| T38 | motion.warp | nexus.motion.graphics | True | True | L3 | VERIFIED |  |
| T39 | motion.add_title | nexus.motion.graphics | True | True | L3 | VERIFIED |  |
| T40 | motion.add_particles | nexus.motion.graphics | True | True | L3 | VERIFIED |  |
| T41 | audio.detect_beats | nexus.audio.studio | True | True | L3 | VERIFIED |  |
| T42 | audio.beat_sync_cut | nexus.audio.studio | True | True | L3 | VERIFIED |  |
| T43 | audio.remove_noise | nexus.audio.studio | True | True | L3 | VERIFIED |  |
| T44 | audio.remove_vocal | nexus.audio.studio | True | True | L3 | VERIFIED |  |
| T45 | audio.duck_music | nexus.audio.studio | True | True | L3 | VERIFIED |  |
| T46 | audio.normalize_loudness | nexus.audio.studio | True | True | L3 | VERIFIED |  |
| T47 | audio.eq_voice | nexus.audio.studio | True | True | L3 | VERIFIED |  |
| T48 | audio.deess | nexus.audio.studio | True | True | L3 | VERIFIED |  |
| T49 | audio.align_music | nexus.audio.studio | True | True | L3 | VERIFIED |  |
| T50 | audio.time_stretch | nexus.audio.studio | True | True | L3 | VERIFIED |  |
| T51 | caption.transcribe | nexus.language.caption | True | True | L3 | VERIFIED |  |
| T52 | caption.align_words | nexus.language.caption | True | True | L3 | VERIFIED |  |
| T53 | caption.diarize | nexus.language.caption | True | True | L3 | VERIFIED |  |
| T54 | caption.translate_local | nexus.language.caption | True | True | L3 | VERIFIED |  |
| T55 | caption.generate_srt | nexus.language.caption | True | True | L3 | VERIFIED |  |
| T56 | caption.generate_ass_rtl | nexus.language.caption | True | True | L3 | VERIFIED |  |
| T57 | caption.style_vazirmatn | nexus.language.caption | True | True | L3 | VERIFIED |  |
| T58 | caption.highlight_words | nexus.language.caption | True | True | L3 | VERIFIED |  |
| T59 | caption.search_transcript | nexus.language.caption | True | True | L3 | VERIFIED |  |
| T60 | caption.burn_in | nexus.language.caption | True | True | L3 | VERIFIED |  |
| T61 | color.auto_balance | nexus.color.delivery | True | True | L3 | VERIFIED |  |
| T62 | color.adjust_exposure | nexus.color.delivery | True | True | L3 | VERIFIED |  |
| T63 | color.white_balance | nexus.color.delivery | True | False | L0 | MISSING | MISSING in runtime 57 — color.white_balance defined in TDD but not registered |
| T64 | color.match_shot | nexus.color.delivery | True | True | L3 | VERIFIED |  |
| T65 | color.apply_lut | nexus.color.delivery | True | True | L3 | VERIFIED |  |
| T66 | color.hdr_tonemap | nexus.color.delivery | True | False | L0 | MISSING | MISSING in runtime 57 — color.hdr_tonemap defined in TDD but not registered |
| T67 | color.deband_denoise | nexus.color.delivery | True | False | L0 | MISSING | MISSING in runtime 57 — color.deband_denoise defined in TDD but not registered |
| T68 | delivery.make_proxy_480p | nexus.color.delivery | True | True | L3 | VERIFIED |  |
| T69 | delivery.render_master_4k | nexus.color.delivery | True | True | L3 | VERIFIED |  |
| T70 | delivery.export_otio | nexus.color.delivery | True | True | L3 | VERIFIED |  |
| E01 | media.play | media/system | False | True | L2 | VERIFIED | Extra outside 70 — not in TDD catalog, but in runtime |
| E02 | media.pause | media/system | False | True | L2 | VERIFIED | Extra outside 70 — not in TDD catalog, but in runtime |
| E03 | timeline.mark | media/system | False | True | L4 | VERIFIED | Extra outside 70 — not in TDD catalog, but in runtime |
| E04 | system.undo | media/system | False | True | L2 | VERIFIED | Extra outside 70 — not in TDD catalog, but in runtime |
| E05 | slideshow.scan_assets | nexus.slideshow.compose | False | True | L3 | VERIFIED | Extra outside 70 — not in TDD catalog, but in runtime |
| E06 | slideshow.suggest_tone | nexus.slideshow.compose | False | True | L3 | VERIFIED | Extra outside 70 — not in TDD catalog, but in runtime |
| E07 | slideshow.score_images | nexus.slideshow.compose | False | True | L3 | VERIFIED | Extra outside 70 — not in TDD catalog, but in runtime |
| E08 | slideshow.compose | nexus.slideshow.compose | False | True | L4 | VERIFIED | Extra outside 70 — not in TDD catalog, but in runtime |
| E09 | slideshow.render | nexus.slideshow.compose | False | True | L4 | VERIFIED | Extra outside 70 — not in TDD catalog, but in runtime |
| E10 | slideshow.upscale | nexus.slideshow.compose | False | True | L3 | VERIFIED | Extra outside 70 — not in TDD catalog, but in runtime |

## Detailed JSON

See [OPERATION_MATRIX.json](OPERATION_MATRIX.json) for full columns.

## Evidence Model

Each row carries Evidence Class: VERIFIED, OBSERVED, SUPPORTED, INFERRED, HYPOTHESIS, NOT_VERIFIED, DOCUMENTED_ONLY, MISSING, BLOCKED
And Evidence Location + Tests + Owner.
