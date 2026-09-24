# 70 ↔ 57 Reconciliation — Explicit Tables

> Formula: `70 - 20 - 3 + 10 = 57`

## A. Which 70 operations are in TDD?

Count: 70

- audio.align_music
- audio.beat_sync_cut
- audio.deess
- audio.detect_beats
- audio.duck_music
- audio.eq_voice
- audio.normalize_loudness
- audio.remove_noise
- audio.remove_vocal
- audio.time_stretch
- caption.align_words
- caption.burn_in
- caption.diarize
- caption.generate_ass_rtl
- caption.generate_srt
- caption.highlight_words
- caption.search_transcript
- caption.style_vazirmatn
- caption.transcribe
- caption.translate_local
- color.adjust_exposure
- color.apply_lut
- color.auto_balance
- color.deband_denoise
- color.hdr_tonemap
- color.match_shot
- color.white_balance
- delivery.export_otio
- delivery.make_proxy_480p
- delivery.render_master_4k
- motion.add_glow
- motion.add_motion_blur
- motion.add_parallax
- motion.add_particles
- motion.add_title
- motion.add_transition
- motion.apply_mask
- motion.keyframe_transform
- motion.stabilize
- motion.warp
- portrait.background_blur
- portrait.correct_gaze
- portrait.detect_landmarks
- portrait.enhance_eyes
- portrait.mask_hair
- portrait.relight_face
- portrait.retouch_blemish
- portrait.smooth_skin
- portrait.stabilize_face
- portrait.whiten_teeth
- scene.auto_reframe_subject
- scene.detect_shot_boundaries
- scene.find_subject_moment
- scene.remove_background
- scene.remove_logo
- scene.remove_object
- scene.replace_sky
- scene.segment_subject
- scene.track_face
- scene.track_object
- timeline.attach_b_roll
- timeline.freeze_frame
- timeline.insert_gap
- timeline.retime_to_music
- timeline.reverse_segment
- timeline.ripple_delete
- timeline.speed_ramp
- timeline.split_at_playhead
- timeline.sync_multicam
- timeline.trim

## B. Which 70 are in registry?

Count: 47 of 70 are in registry

- audio.align_music ✅ REGISTERED
- audio.beat_sync_cut ✅ REGISTERED
- audio.deess ✅ REGISTERED
- audio.detect_beats ✅ REGISTERED
- audio.duck_music ✅ REGISTERED
- audio.eq_voice ✅ REGISTERED
- audio.normalize_loudness ✅ REGISTERED
- audio.remove_noise ✅ REGISTERED
- audio.remove_vocal ✅ REGISTERED
- audio.time_stretch ✅ REGISTERED
- caption.align_words ✅ REGISTERED
- caption.burn_in ✅ REGISTERED
- caption.diarize ✅ REGISTERED
- caption.generate_ass_rtl ✅ REGISTERED
- caption.generate_srt ✅ REGISTERED
- caption.highlight_words ✅ REGISTERED
- caption.search_transcript ✅ REGISTERED
- caption.style_vazirmatn ✅ REGISTERED
- caption.transcribe ✅ REGISTERED
- caption.translate_local ✅ REGISTERED
- color.adjust_exposure ✅ REGISTERED
- color.apply_lut ✅ REGISTERED
- color.auto_balance ✅ REGISTERED
- color.match_shot ✅ REGISTERED
- delivery.export_otio ✅ REGISTERED
- delivery.make_proxy_480p ✅ REGISTERED
- delivery.render_master_4k ✅ REGISTERED
- motion.add_glow ✅ REGISTERED
- motion.add_motion_blur ✅ REGISTERED
- motion.add_parallax ✅ REGISTERED
- motion.add_particles ✅ REGISTERED
- motion.add_title ✅ REGISTERED
- motion.add_transition ✅ REGISTERED
- motion.apply_mask ✅ REGISTERED
- motion.keyframe_transform ✅ REGISTERED
- motion.stabilize ✅ REGISTERED
- motion.warp ✅ REGISTERED
- timeline.attach_b_roll ✅ REGISTERED
- timeline.freeze_frame ✅ REGISTERED
- timeline.insert_gap ✅ REGISTERED
- timeline.retime_to_music ✅ REGISTERED
- timeline.reverse_segment ✅ REGISTERED
- timeline.ripple_delete ✅ REGISTERED
- timeline.speed_ramp ✅ REGISTERED
- timeline.split_at_playhead ✅ REGISTERED
- timeline.sync_multicam ✅ REGISTERED
- timeline.trim ✅ REGISTERED

## C. Which have domain reducer?

All 57 runtime ops have pure reducer (evidence: operations.py pure handlers).

## D. Which have execution proof?

Real encode / runtime proof (render lane, audio analysis, caption):
- delivery.make_proxy_480p
- delivery.render_master_4k
- delivery.export_otio
- slideshow.render
- slideshow.compose
- caption.burn_in
- caption.transcribe
- audio.detect_beats
- timeline.split_at_playhead

## E. Which are only concept/contract?

Missing 23: ['color.deband_denoise', 'color.hdr_tonemap', 'color.white_balance', 'portrait.background_blur', 'portrait.correct_gaze', 'portrait.detect_landmarks', 'portrait.enhance_eyes', 'portrait.mask_hair', 'portrait.relight_face', 'portrait.retouch_blemish', 'portrait.smooth_skin', 'portrait.stabilize_face', 'portrait.whiten_teeth', 'scene.auto_reframe_subject', 'scene.detect_shot_boundaries', 'scene.find_subject_moment', 'scene.remove_background', 'scene.remove_logo', 'scene.remove_object', 'scene.replace_sky', 'scene.segment_subject', 'scene.track_face', 'scene.track_object']

## F. Which extra outside 70?

- media.pause
- media.play
- slideshow.compose
- slideshow.render
- slideshow.scan_assets
- slideshow.score_images
- slideshow.suggest_tone
- slideshow.upscale
- system.undo
- timeline.mark

## G. Which IDs collision?

None — no duplicate operation IDs in registry (verified by CapabilityRegistry duplicate check).

## H. Which orphan?

None — every runtime operation has manifest entry (composition_issues() == ()).

## I. Which registry entries legacy/duplicate/alias?

None — all 57 are canonical, no alias.

## Final Proof

**Product Catalog ≠ Runtime Registry ≠ Executable Surface — proven**

- Product Catalog (70): TDD defines 7 packs ×10
- Runtime Registry (57): live build_runtime_registry() = 5 wave1 + 52 pack ops
- Executable Surface: subset with surface mapping + real encode proof
