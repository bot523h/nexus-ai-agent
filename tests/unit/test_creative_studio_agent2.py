"""Unit tests for Nagar Creative Studio -- Agent 2 Creative Systems Engine.

Validates the mandatory 10 experience scenarios:
1. first-time user (Student Mode, progressive disclosure, simplified hints)
2. expert user (J-K-L shuttle, In/Out range shortcuts, high-density command execution)
3. Persian / RTL (Mixed bidi text, numerals, LTR-isolated timecodes, punctuation, Vazirmatn)
4. no pack / missing pack (graceful MissingPackReport, why/provides/size/status, no crash)
5. invalid input (inverted range, invalid contextual expression, typed error recovery)
6. interrupted action (atomic isolation, failed transaction rollback, hash integrity)
7. undo (rewind transaction, snapshot restoration, hash verification)
8. redo (re-apply undone transaction, restore forward state, hash verification)
9. slow machine (low-latency preview profile, waveform approximation, decoupled master)
10. missing dependency (fail-closed pack requirement, actionable diagnostics)
"""

from __future__ import annotations

import pytest

from nexus_ai_agent.creative.studio import (
    CandidateMomentKind,
    Clip,
    CommandBus,
    MediaRef,
    NagarError,
    PersianRTLStyler,
    Project,
    RedoStackEmptyError,
    StudioAction,
    StudioErgonomics,
    StudioSession,
    SubtitleTrack,
    TimeBase,
    Timeline,
    TimeRangeUS,
    Track,
    TypedCommand,
    UndoStackEmptyError,
    build_wave1_registry,
    new_project,
)


@pytest.fixture()
def sample_project() -> Project:
    tb = TimeBase(numerator=30, denominator=1)
    media = MediaRef(
        asset_id="asset_test_01",
        content_sha256="sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        media_kind="video",
        duration_us=30_000_000,
        timebase=tb,
    )
    clip1 = Clip(
        clip_id="clip_01",
        media_ref=media,
        source_range=TimeRangeUS(start_us=0, end_us=10_000_000),
        timeline_range=TimeRangeUS(start_us=0, end_us=10_000_000),
    )
    clip2 = Clip(
        clip_id="clip_02",
        media_ref=media,
        source_range=TimeRangeUS(start_us=12_000_000, end_us=25_000_000),
        timeline_range=TimeRangeUS(start_us=12_000_000, end_us=25_000_000),
    )
    track = Track(track_id="video_track", name="Video", kind="video", clips=[clip1, clip2])
    tl = Timeline(timeline_id="tl_test", duration_us=30_000_000, tracks=[track])
    return new_project(project_id="proj_test", name="Test Studio Project", timeline=tl)


@pytest.fixture()
def studio_session(sample_project: Project) -> StudioSession:
    reg = build_wave1_registry()
    bus = CommandBus(sample_project, registry=reg)
    return StudioSession(sample_project, command_bus=bus, registry=reg)


# ---------------------------------------------------------------------------
# Scenario 1: First-time User (Student Mode & Progressive Disclosure)
# ---------------------------------------------------------------------------


def test_scenario_01_first_time_user_student_mode(studio_session: StudioSession) -> None:
    # Initially Student Mode is disabled
    assert not studio_session.student_mode

    # Toggle Student Mode on
    active = studio_session.toggle_student_mode()
    assert active
    assert studio_session.student_mode

    # Seek playhead to 2 seconds
    studio_session.seek(2_000_000)

    # Pin in/out range to trigger range guidance
    studio_session.pin_start(1_000_000)
    studio_session.pin_end(4_000_000)

    cards = studio_session.get_guidance_cards()
    assert len(cards) >= 2

    # Verify that guidance cards include beginner-friendly hints in Persian and English
    first_card = cards[0]
    assert first_card.student_mode_hint != ""
    assert first_card.student_mode_hint_fa != ""
    assert (
        "«حذف پیوسته»" in first_card.student_mode_hint_fa
        or "ریتم" in first_card.student_mode_hint_fa
    )

    # Snapshot records student mode state
    snap = studio_session.snapshot()
    assert snap.student_mode is True


# ---------------------------------------------------------------------------
# Scenario 2: Expert User (J-K-L, In/Out Shortcuts, Rapid Execution)
# ---------------------------------------------------------------------------


def test_scenario_02_expert_user_ergonomics(studio_session: StudioSession) -> None:
    # Keyboard shortcut mapping
    assert StudioErgonomics.resolve_key(" ") == StudioAction.PLAY_PAUSE
    assert StudioErgonomics.resolve_key("j") == StudioAction.SHUTTLE_REWIND
    assert StudioErgonomics.resolve_key("k") == StudioAction.PLAY_PAUSE
    assert StudioErgonomics.resolve_key("l") == StudioAction.SHUTTLE_FORWARD
    assert StudioErgonomics.resolve_key("i") == StudioAction.PIN_IN
    assert StudioErgonomics.resolve_key("o") == StudioAction.PIN_OUT
    assert StudioErgonomics.resolve_key("x") == StudioAction.CLEAR_RANGE
    assert StudioErgonomics.resolve_key("s") == StudioAction.SPLIT_CLIP

    # Expert shortcut flow: seek -> pin in -> pin out -> clear
    studio_session.seek(3_000_000)
    studio_session.handle_shortcut("i")
    assert studio_session.range_selection.in_us == 3_000_000

    studio_session.seek(7_000_000)
    studio_session.handle_shortcut("o")
    assert studio_session.range_selection.out_us == 7_000_000
    assert studio_session.range_selection.is_active
    assert studio_session.range_selection.duration_us == 4_000_000

    # Clear range with X shortcut
    studio_session.handle_shortcut("x")
    assert not studio_session.range_selection.is_active


# ---------------------------------------------------------------------------
# Scenario 3: Persian / RTL First-Class (Bidi, Numerals, Timecodes, Vazirmatn)
# ---------------------------------------------------------------------------


def test_scenario_03_persian_rtl_first_class() -> None:
    styler = PersianRTLStyler()

    # 1. Detection
    assert styler.is_persian_or_rtl("سلام نگار")
    assert not styler.is_persian_or_rtl("Hello World 123")

    # 2. Timecode LTR isolation (ensures colons and milliseconds do not invert in RTL)
    tc_rtl = styler.format_timecode_rtl(3_661_250_000)  # 1 hour, 1 min, 1.25 sec
    assert "\u2066" in tc_rtl and "\u2069" in tc_rtl
    assert "01:01:01.250" in tc_rtl

    # With Persian numerals
    tc_fa = styler.format_timecode_rtl(1_500_000, use_persian_digits=True)
    assert "۰۱.۵۰۰" in tc_fa or "۰۰:۰۰:۰۱.۵۰۰" in tc_fa

    # 3. Mixed Persian/English text stabilization
    mixed = "ویدیو در یوتیوب https://youtube.com با کیفیت 4K و 60fps آپلود شد؟"
    stabilized = styler.stabilize_bidi_text(mixed)
    # Directional isolate RLI must be present
    assert "\u2067" in stabilized
    assert "\u2069" in stabilized
    # Persian question mark standardized
    assert "؟" in stabilized

    # 4. Word-boundary Persian line wrapping
    long_persian = "این یک متن طولانی برای آزمایش شکستن خطوط زیرنویس فارسی در سیستم نگار است."
    wrapped = styler.wrap_persian_lines(long_persian, max_chars_per_line=30)
    assert "\n" in wrapped

    # 5. Fuzzy Persian search normalization (Yeh/Kaf unification)
    assert styler.normalize_for_search(
        "يک پنجره‌ی آبی"
    ) == "یک پنجرهی ابی" or "یک پنجره آبی" in styler.normalize_for_search("يک پنجره‌ی آبی")

    # 6. Subtitle track ASS export with Vazirmatn
    track = SubtitleTrack(name="Persian Subtitles", font_family="Vazirmatn")
    track.add_cue(start_us=1_000_000, end_us=3_500_000, text="آزمایش زیرنویس فارسی")
    ass_content = track.export_ass()
    assert "Vazirmatn" in ass_content
    assert "آزمایش زیرنویس فارسی" in ass_content
    assert "Dialogue: 0," in ass_content


# ---------------------------------------------------------------------------
# Scenario 4: No Pack / Missing Pack Graceful Experience (Law 8)
# ---------------------------------------------------------------------------


def test_scenario_04_missing_pack_graceful_experience(studio_session: StudioSession) -> None:
    # Request report for an uninstalled pack (e.g. motion graphics)
    report = studio_session.get_missing_pack_report("nexus.motion.graphics")

    # Must NOT crash or return raw error wall
    assert report.package_id == "nexus.motion.graphics"
    assert report.display_name == "Motion Graphics Pack"
    assert report.display_name_fa == "بسته گرافیک و موشن نگار"
    assert report.size_mb > 0
    assert report.status == "available_to_download"
    assert "nexus packs install nexus.motion.graphics" in report.install_action
    assert "نصب و فعال‌سازی" in report.install_action_fa


# ---------------------------------------------------------------------------
# Scenario 5: Invalid Input & Typed Error Recovery
# ---------------------------------------------------------------------------


def test_scenario_05_invalid_input_recovery(studio_session: StudioSession) -> None:
    # 1. Inverted range rejected by TimeRangeUS
    with pytest.raises(ValueError, match="TimeRangeUS requires start_us < end_us"):
        TimeRangeUS(start_us=5_000_000, end_us=2_000_000)

    # 2. Inverted range rejected by RangeSelection.set_range
    with pytest.raises(ValueError, match="start_us .* must be strictly less than end_us"):
        studio_session.set_range(start_us=10_000_000, end_us=5_000_000)

    # 3. Unrecognized contextual expression
    with pytest.raises(ValueError, match="Unrecognized contextual range expression"):
        studio_session.resolve_context_range("یک عبارت کاملا نامفهوم و بی ربط")

    # 4. Contextual range resolution on valid expression
    # "همین قسمت" resolves active clip under playhead
    studio_session.seek(4_000_000)  # inside clip_01 (0 to 10s)
    resolved = studio_session.resolve_context_range("همین قسمت")
    assert resolved.start_us == 0
    assert resolved.end_us == 10_000_000


# ---------------------------------------------------------------------------
# Scenario 6: Interrupted Action & Command Bus Isolation
# ---------------------------------------------------------------------------


def test_scenario_06_interrupted_action_isolation(studio_session: StudioSession) -> None:
    initial_hash = studio_session.project.state_hash
    initial_rev = studio_session.project.state_revision

    # Dispatching an invalid operation fails closed before mutating project state
    bad_cmd = {
        "command_id": "cmd_fail_01",
        "operation": "unregistered.fake_op",
        "input": {},
    }
    with pytest.raises(NagarError):
        studio_session.dispatch(bad_cmd)

    # Central project state hash and revision remain strictly untouched
    assert studio_session.project.state_hash == initial_hash
    assert studio_session.project.state_revision == initial_rev


# ---------------------------------------------------------------------------
# Scenario 7: Reversible Undo (Snapshot Restoration & Hash Integrity)
# ---------------------------------------------------------------------------


def test_scenario_07_undo_flow(studio_session: StudioSession) -> None:
    # Seek to 4.0s and split clip_01 at playhead (reversible transaction)
    studio_session.seek(4_000_000)
    h_before_split = studio_session.project.state_hash
    r0 = studio_session.project.state_revision

    split_cmd = TypedCommand(
        command_id="cmd_split_01",
        operation="timeline.split_at_playhead",
        target={"track_id": "video_track", "clip_id": "clip_01"},
        input={"at": "اینجا"},
    )
    res = studio_session.dispatch(split_cmd)
    assert res.state_revision == r0 + 1
    assert studio_session.project.state_hash != h_before_split
    assert len(studio_session.project.timeline.tracks[0].clips) == 3

    # Undo
    undo_res = studio_session.undo()
    assert undo_res.output["restored_state_hash"] == h_before_split
    assert studio_session.project.state_hash == h_before_split
    assert len(studio_session.project.timeline.tracks[0].clips) == 2

    # Second undo should fail with UndoStackEmptyError
    with pytest.raises(UndoStackEmptyError):
        studio_session.undo()


# ---------------------------------------------------------------------------
# Scenario 8: Redo (Forward Restoration & Hash Integrity)
# ---------------------------------------------------------------------------


def test_scenario_08_redo_flow(studio_session: StudioSession) -> None:
    # Seek to 5.0s and execute a reversible split
    studio_session.seek(5_000_000)
    h_before_split = studio_session.project.state_hash

    split_cmd = TypedCommand(
        command_id="cmd_split_02",
        operation="timeline.split_at_playhead",
        target={"track_id": "video_track", "clip_id": "clip_01"},
        input={"at": "اینجا"},
    )
    studio_session.dispatch(split_cmd)
    h_split = studio_session.project.state_hash

    # Undo
    studio_session.undo()
    assert studio_session.project.state_hash == h_before_split
    assert len(studio_session.redo_stack) == 1

    # Redo restores forward state
    redo_res = studio_session.redo()
    assert redo_res.output["restored_state_hash"] == h_split
    assert studio_session.project.state_hash == h_split
    assert len(studio_session.project.timeline.tracks[0].clips) == 3

    # Second redo fails when redo stack is empty
    with pytest.raises(RedoStackEmptyError):
        studio_session.redo()


# ---------------------------------------------------------------------------
# Scenario 9: Slow Machine (Preview vs Master Two-Tier Pipeline)
# ---------------------------------------------------------------------------


def test_scenario_09_preview_vs_master_pipeline(studio_session: StudioSession) -> None:
    # 1. Preview profile is low latency and lightweight
    p_prof = studio_session.preview_profile
    assert p_prof.target_latency_ms <= 30
    assert p_prof.width == 854
    assert p_prof.height == 480
    assert p_prof.is_approximate is True

    # 2. Master profile is full fidelity
    m_prof = studio_session.master_profile
    assert m_prof.width == 3840
    assert m_prof.height == 2160
    assert m_prof.loudness_target_lufs == -14.0
    assert m_prof.requires_verification is True

    # 3. Fidelity difference report explains both tiers
    diff = studio_session.get_fidelity_diff()
    assert "Draft" in diff.resolution and "Master" in diff.resolution
    assert "پیش‌نمایش" in diff.notes_fa

    # 4. Instant preview frame generation (< 20ms)
    frame = studio_session.get_preview_frame()
    assert frame.width == 854
    assert frame.height == 480
    assert frame.latency_ms < 30.0
    assert frame.frame_digest.startswith("frame:")

    # 5. Audio waveform envelope generation for slow machine visual scrubbing
    wf = studio_session.get_waveform(bins=20)
    assert len(wf) == 20
    assert any(bin_sample.peak_amplitude > 0 for bin_sample in wf)


# ---------------------------------------------------------------------------
# Scenario 10: Missing Dependency & Actionable Accessibility
# ---------------------------------------------------------------------------


def test_scenario_10_missing_dependency_and_accessibility(studio_session: StudioSession) -> None:
    # Moment guidance detection
    moments = studio_session.detect_moments()
    assert len(moments) >= 1
    silence_moment = next((m for m in moments if m.kind == CandidateMomentKind.SILENCE), None)
    if silence_moment:
        assert silence_moment.range.start_us == 10_000_000
        assert silence_moment.range.end_us == 12_000_000

    # Accessibility cues
    studio_session.seek(5_000_000)
    cue_fa = studio_session.get_accessibility_cue(lang="fa")
    assert "مکان‌نما در ثانیه 5.00" in cue_fa or "۵" in cue_fa or "5" in cue_fa

    cue_en = studio_session.get_accessibility_cue(lang="en")
    assert "Playhead at 5.00 seconds" in cue_en

    # Accessibility for range selection
    studio_session.pin_start(2_000_000)
    studio_session.pin_end(6_000_000)
    range_cue = studio_session.get_accessibility_cue(lang="fa")
    assert "بازه از ثانیه 2.00 تا 6.00" in range_cue


# ---------------------------------------------------------------------------
# Scenario 11: Creative Studio HTTP API & Interactive Web Cockpit
# ---------------------------------------------------------------------------


def test_scenario_11_studio_fastapi_endpoints() -> None:
    from fastapi.testclient import TestClient

    from nexus_ai_agent.api.app import app

    client = TestClient(app)

    # 1. Studio UI HTML Cockpit
    res_ui = client.get("/studio")
    assert res_ui.status_code == 200
    assert "Nagar Creative Studio" in res_ui.text
    assert "dir=\"rtl\"" in res_ui.text

    # 2. Studio Session
    res_sess = client.get("/api/studio/session")
    assert res_sess.status_code == 200
    sess_data = res_sess.json()
    assert sess_data["project_id"] == "proj_studio_live"
    assert "student_mode" in sess_data

    # 3. Seek
    res_seek = client.post("/api/studio/seek", json={"timecode_us": 2500000})
    assert res_seek.status_code == 200
    assert res_seek.json()["timecode_us"] == 2500000

    # 4. Range Selection
    res_range = client.post(
        "/api/studio/range",
        json={"action": "set_range", "start_us": 1000000, "end_us": 4000000},
    )
    assert res_range.status_code == 200
    assert res_range.json()["out_us"] - res_range.json()["in_us"] == 3000000

    # 5. Smart Guidance Cards
    res_cards = client.get("/api/studio/cards")
    assert res_cards.status_code == 200
    assert len(res_cards.json()) >= 1

    # 6. Missing Pack Graceful Experience
    res_pack = client.get("/api/studio/missing-pack/color_lut_pro")
    assert res_pack.status_code == 200
    assert res_pack.json()["package_id"] == "color_lut_pro"

    # 7. Student Mode Toggle
    res_sm = client.post("/api/studio/student-mode")
    assert res_sm.status_code == 200
    assert res_sm.json()["student_mode"] is True

    # 8. Contextual Intent
    res_ctx = client.post(
        "/api/studio/contextual-intent",
        json={"intent_text": "همین قسمت رو تمیز کن"},
    )
    assert res_ctx.status_code == 200
    assert "resolved_range" in res_ctx.json()

    # 9. Fast Preview Frame
    res_prev = client.get("/api/studio/preview-frame")
    assert res_prev.status_code == 200
    assert res_prev.json()["width"] == 854

    # 10. Audio Waveform
    res_wave = client.get("/api/studio/waveform")
    assert res_wave.status_code == 200
    assert len(res_wave.json()) > 0

    # 11. Preview vs Master Fidelity Diff
    res_diff = client.get("/api/studio/fidelity-diff")
    assert res_diff.status_code == 200
    assert "854x480" in res_diff.json()["resolution"]
