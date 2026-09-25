"""Nagar Creative Studio -- Timeline Interaction, Shared Playhead, and Moment Guidance.

Owned by Agent 2 (Principal Creative Systems Engineer + Product UX Architect).

Implements:
- Shared Playhead Cursor (bridge between user, assistant, timeline, preview, operation)
- Moment-first interaction model ("play -> hold -> identify t -> command")
- Range selection ("pin start -> pin end -> range command")
- Contextual intent resolution ("همین قسمت", "از اینجا تا ۵ ثانیه بعد")
- Smart Assistant Guidance Cards with Student Mode progressive disclosure
- Graceful Missing-Pack Experience (informative, non-crashing, actionable)
- Ergonomic keyboard (J-K-L) and touch gesture interaction models
- Accessibility ARIA announcements in Persian and English
"""

from __future__ import annotations

import re
import uuid
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from nexus_ai_agent.creative.studio.models import (
    MICROSECONDS_PER_SECOND,
    Clip,
    Playhead,
    Project,
    TimeBase,
    TimeRangeUS,
    frame_number_for,
)

# ---------------------------------------------------------------------------
# Shared Playhead Cursor
# ---------------------------------------------------------------------------


class PlayheadCursorSource(str, Enum):
    """Identifies who or what adjusted the shared playhead position."""

    USER_SCRUB = "user_scrub"
    USER_KEYBOARD = "user_keyboard"
    ASSISTANT_GUIDANCE = "assistant_guidance"
    PLAYBACK_TRANSPORT = "playback_transport"
    COMMAND_TARGET = "command_target"


class SharedPlayheadCursor(BaseModel):
    """The shared playhead cursor bridging User, Assistant, Timeline, and Preview.

    Unlike a bare playback timestamp, the shared cursor carries its provenance,
    freeze-hold state, and label so that conversational commands like
    "همین قسمت رو تمیز کن" or "اینجا رو علامت بزن" can unambiguously bind
    to the active editorial context.
    """

    model_config = ConfigDict(extra="forbid")

    timecode_us: int = Field(default=0, ge=0)
    frame_number: int | None = None
    timebase: TimeBase = Field(default_factory=lambda: TimeBase(numerator=30, denominator=1))
    source: PlayheadCursorSource = PlayheadCursorSource.USER_SCRUB
    captured_at_us: int = Field(default=0, ge=0)
    label: str | None = None
    is_holding: bool = False
    is_playing: bool = False

    @model_validator(mode="after")
    def _compute_frame(self) -> SharedPlayheadCursor:
        if self.frame_number is None:
            self.frame_number = frame_number_for(self.timecode_us, self.timebase)
        return self

    def seek(
        self,
        timecode_us: int,
        source: PlayheadCursorSource = PlayheadCursorSource.USER_SCRUB,
        label: str | None = None,
    ) -> SharedPlayheadCursor:
        """Seek the cursor to a specific microsecond timestamp."""
        return SharedPlayheadCursor(
            timecode_us=max(0, timecode_us),
            frame_number=frame_number_for(max(0, timecode_us), self.timebase),
            timebase=self.timebase,
            source=source,
            captured_at_us=self.captured_at_us,
            label=label,
            is_holding=False,
            is_playing=self.is_playing,
        )

    def hold(self, label: str | None = "Hold Marker") -> SharedPlayheadCursor:
        """Freeze/hold playhead for moment inspection ('play -> hold -> identify t -> command')."""
        return SharedPlayheadCursor(
            timecode_us=self.timecode_us,
            frame_number=self.frame_number,
            timebase=self.timebase,
            source=PlayheadCursorSource.USER_KEYBOARD,
            captured_at_us=self.timecode_us,
            label=label,
            is_holding=True,
            is_playing=False,
        )

    def release_hold(self) -> SharedPlayheadCursor:
        """Release the hold and return to normal scrub mode."""
        return SharedPlayheadCursor(
            timecode_us=self.timecode_us,
            frame_number=self.frame_number,
            timebase=self.timebase,
            source=self.source,
            captured_at_us=self.captured_at_us,
            label=None,
            is_holding=False,
            is_playing=self.is_playing,
        )

    def to_playhead(self) -> Playhead:
        """Convert into canonical studio Playhead model."""
        return Playhead(
            timecode_us=self.timecode_us,
            frame_number=self.frame_number,
            timebase=self.timebase,
            captured_at_command=True,
            is_playing=self.is_playing,
        )


# ---------------------------------------------------------------------------
# Range Selection (In / Out)
# ---------------------------------------------------------------------------


class RangeSelection(BaseModel):
    """In/Out range selection on the timeline ('pin start -> pin end -> range command')."""

    model_config = ConfigDict(extra="forbid")

    in_us: int | None = Field(default=None, ge=0)
    out_us: int | None = Field(default=None, ge=0)
    is_active: bool = False
    label: str | None = None

    @property
    def duration_us(self) -> int:
        if self.in_us is not None and self.out_us is not None and self.out_us > self.in_us:
            return self.out_us - self.in_us
        return 0

    def pin_start(self, timecode_us: int) -> RangeSelection:
        """Pin In-point."""
        t = max(0, timecode_us)
        new_out = self.out_us
        if new_out is not None and t >= new_out:
            new_out = None
        active = new_out is not None and new_out > t
        return RangeSelection(in_us=t, out_us=new_out, is_active=active, label=self.label)

    def pin_end(self, timecode_us: int) -> RangeSelection:
        """Pin Out-point."""
        t = max(0, timecode_us)
        new_in = self.in_us
        if new_in is not None and t <= new_in:
            new_in = None
        active = new_in is not None and t > new_in
        return RangeSelection(in_us=new_in, out_us=t, is_active=active, label=self.label)

    def set_range(self, start_us: int, end_us: int, label: str | None = None) -> RangeSelection:
        """Set complete [start, end) range."""
        if start_us >= end_us:
            raise ValueError(f"start_us ({start_us}) must be strictly less than end_us ({end_us})")
        return RangeSelection(
            in_us=max(0, start_us),
            out_us=max(0, end_us),
            is_active=True,
            label=label,
        )

    def clear(self) -> RangeSelection:
        """Clear the range selection."""
        return RangeSelection(in_us=None, out_us=None, is_active=False, label=None)

    def to_time_range_us(self) -> TimeRangeUS:
        """Convert to canonical TimeRangeUS. Raises ValueError if not active."""
        if not self.is_active or self.in_us is None or self.out_us is None:
            raise ValueError("RangeSelection is not active")
        return TimeRangeUS(start_us=self.in_us, end_us=self.out_us)

    def snap_to(self, timecode_us: int, candidates: list[int], threshold_us: int = 50_000) -> int:
        """Snap timecode to the nearest candidate within threshold (default: 50ms)."""
        best_t = timecode_us
        best_diff = threshold_us + 1
        for c in candidates:
            diff = abs(c - timecode_us)
            if diff <= threshold_us and diff < best_diff:
                best_diff = diff
                best_t = c
        return best_t


# ---------------------------------------------------------------------------
# Moment Guidance
# ---------------------------------------------------------------------------


class CandidateMomentKind(str, Enum):
    SILENCE = "silence"
    SPEECH_BEAT = "speech_beat"
    SCENE_CUT = "scene_cut"
    ACTION_PEAK = "action_peak"
    USER_PIN = "user_pin"
    MARKER = "marker"


class CandidateMoment(BaseModel):
    """An AI/heuristic identified moment or range on the timeline."""

    model_config = ConfigDict(extra="forbid")

    moment_id: str = Field(default_factory=lambda: f"moment_{uuid.uuid4().hex[:8]}")
    kind: CandidateMomentKind
    range: TimeRangeUS
    confidence: float = Field(ge=0.0, le=1.0)
    label: str
    label_fa: str
    reason: str
    reason_fa: str
    suggested_operation: str


class MomentGuidanceEngine:
    """Detects and guides moments around the shared playhead cursor."""

    @staticmethod
    def detect_candidate_moments(
        project: Project, playhead_us: int, window_us: int = 10_000_000
    ) -> list[CandidateMoment]:
        """Detect candidate moments within a time window around playhead."""
        moments: list[CandidateMoment] = []
        timeline = project.timeline

        # 1. Markers near playhead
        for m in timeline.markers:
            if abs(m.timecode_us - playhead_us) <= window_us:
                start_us = max(0, m.timecode_us - 500_000)
                end_us = min(timeline.duration_us, m.timecode_us + 500_000)
                if start_us < end_us:
                    moments.append(
                        CandidateMoment(
                            kind=CandidateMomentKind.MARKER,
                            range=TimeRangeUS(start_us=start_us, end_us=end_us),
                            confidence=1.0,
                            label=f"Marker: {m.label}",
                            label_fa=f"نشانگر: {m.label}",
                            reason=f"Timeline marker {m.label!r} at {m.timecode_us // 1000}ms",
                            reason_fa=(
                                f"نشانگر تایم‌لاین «{m.label}» در موقعیت "
                                f"{m.timecode_us // 1000} میلی‌ثانیه"
                            ),
                            suggested_operation="timeline.split_at_playhead",
                        )
                    )

        # 2. Clips around playhead
        for track in timeline.tracks:
            for clip in track.clips:
                c_range = clip.timeline_range
                # Clip containing playhead
                if c_range.start_us <= playhead_us < c_range.end_us:
                    moments.append(
                        CandidateMoment(
                            kind=CandidateMomentKind.SCENE_CUT,
                            range=c_range,
                            confidence=0.95,
                            label=f"Active Clip: {clip.clip_id}",
                            label_fa=f"کلیپ فعال: {clip.clip_id}",
                            reason="Current clip under the shared playhead cursor",
                            reason_fa="کلیپ جاری زیر مکان‌نمای مشترک پلی‌هد",
                            suggested_operation="timeline.trim",
                        )
                    )
                # Clip boundary near playhead
                elif abs(c_range.start_us - playhead_us) <= window_us:
                    span_start = max(0, c_range.start_us - 300_000)
                    span_end = min(timeline.duration_us, c_range.start_us + 300_000)
                    if span_start < span_end:
                        moments.append(
                            CandidateMoment(
                                kind=CandidateMomentKind.SCENE_CUT,
                                range=TimeRangeUS(start_us=span_start, end_us=span_end),
                                confidence=0.88,
                                label=f"Cut Boundary: {clip.clip_id}",
                                label_fa=f"مرز برش: {clip.clip_id}",
                                reason="Boundary between video clips",
                                reason_fa="مرز گذر میان کلیپ‌های ویدیو",
                                suggested_operation="motion.add_transition",
                            )
                        )

        # 3. Detect gap/silence if any
        # Sort clips by timeline start
        all_clips: list[Clip] = []
        for track in timeline.tracks:
            all_clips.extend(track.clips)
        sorted_clips = sorted(all_clips, key=lambda c: c.timeline_range.start_us)

        for i in range(len(sorted_clips) - 1):
            curr_end = sorted_clips[i].timeline_range.end_us
            next_start = sorted_clips[i + 1].timeline_range.start_us
            if next_start > curr_end + 100_000:  # gap > 100ms
                if abs(curr_end - playhead_us) <= window_us:
                    moments.append(
                        CandidateMoment(
                            kind=CandidateMomentKind.SILENCE,
                            range=TimeRangeUS(start_us=curr_end, end_us=next_start),
                            confidence=0.92,
                            label="Gap / Silence",
                            label_fa="فاصله خالی / سکوت",
                            reason=(
                                f"Empty space of {(next_start - curr_end) // 1000}ms between clips"
                            ),
                            reason_fa=(
                                f"فاصله خالی به طول {(next_start - curr_end) // 1000} میلی‌ثانیه "
                                "میان کلیپ‌ها"
                            ),
                            suggested_operation="timeline.ripple_delete_range",
                        )
                    )

        return moments


# ---------------------------------------------------------------------------
# Contextual Intent Resolver ("همین قسمت رو تمیز کن")
# ---------------------------------------------------------------------------


class ContextualResolver:
    """Resolves conversational and ergonomic context expressions into concrete time ranges."""

    @staticmethod
    def resolve_range(
        expression: str,
        project: Project,
        playhead_us: int,
        range_selection: RangeSelection | None = None,
    ) -> TimeRangeUS:
        """Resolve a natural contextual intent into an exact TimeRangeUS."""
        expr = expression.strip().lower()

        # 1. Selected range
        if expr in {
            "بازه انتخاب شده",
            "بازه جاری",
            "همین بازه",
            "selected range",
            "selection",
            "current selection",
        }:
            if range_selection and range_selection.is_active:
                return range_selection.to_time_range_us()
            raise ValueError("No active range selection to resolve 'بازه انتخاب شده'")

        # 2. Active clip ("همین قسمت", "این تکه", "this part", "this clip", or phrases)
        if (
            expr
            in {
                "همین قسمت",
                "این قسمت",
                "این تکه",
                "همین تکه",
                "کلیپ فعلی",
                "this part",
                "this clip",
                "current clip",
                "here",
            }
            or any(
                p in expr
                for p in (
                    "همین قسمت",
                    "این قسمت",
                    "این تکه",
                    "همین تکه",
                    "کلیپ فعلی",
                    "this part",
                    "this clip",
                )
            )
        ):
            for track in project.timeline.tracks:
                for clip in track.clips:
                    if clip.timeline_range.start_us <= playhead_us < clip.timeline_range.end_us:
                        return clip.timeline_range
            # Fallback to 2-second neighborhood around playhead
            start_us = max(0, playhead_us - 1_000_000)
            end_us = min(project.timeline.duration_us, playhead_us + 1_000_000)
            if start_us >= end_us:
                end_us = start_us + 1_000_000
            return TimeRangeUS(start_us=start_us, end_us=end_us)

        # 3. "از اینجا تا ۵ ثانیه بعد" / "from here to 5 seconds later"
        match_ahead = re.search(
            r"(?:از اینجا تا|از این لحظه تا|from here to)\s*(\d+(?:[.,]\d+)?)\s*"
            r"(ثانیه|ث|s|second|seconds)",
            expr,
        )
        if match_ahead:
            secs = float(match_ahead.group(1).replace(",", "."))
            span_us = int(secs * MICROSECONDS_PER_SECOND)
            start_us = playhead_us
            end_us = min(project.timeline.duration_us, playhead_us + span_us)
            if start_us >= end_us:
                end_us = start_us + 100_000
            return TimeRangeUS(start_us=start_us, end_us=end_us)

        # 4. "از شروع تا اینجا" / "from start to here"
        if expr in {
            "از شروع تا اینجا",
            "از ابتدا تا الان",
            "از اول تا اینجا",
            "from start to here",
            "head to playhead",
        }:
            end_us = max(100_000, playhead_us)
            return TimeRangeUS(start_us=0, end_us=end_us)

        # 5. "از اینجا تا پایان" / "from here to end"
        if expr in {
            "از اینجا تا پایان",
            "از اینجا تا آخر",
            "تا انتها",
            "from here to end",
            "playhead to end",
        }:
            start_us = min(playhead_us, max(0, project.timeline.duration_us - 100_000))
            return TimeRangeUS(start_us=start_us, end_us=project.timeline.duration_us)

        # 6. Default fallback
        raise ValueError(f"Unrecognized contextual range expression: {expression!r}")


# ---------------------------------------------------------------------------
# Assistant Guidance Cards & Student Mode
# ---------------------------------------------------------------------------


class GuidanceCardAvailability(str, Enum):
    AVAILABLE = "available"
    PACK_REQUIRED = "pack_required"
    EXPERIMENTAL = "experimental"


class GuidanceCard(BaseModel):
    """Smart guidance card surfacing an actionable studio recommendation."""

    model_config = ConfigDict(extra="forbid")

    card_id: str = Field(default_factory=lambda: f"card_{uuid.uuid4().hex[:8]}")
    operation: str
    title: str
    title_fa: str
    reason: str
    reason_fa: str
    target_type: Literal["clip", "range", "playhead", "timeline"]
    target_id: str | None = None
    range: TimeRangeUS | None = None
    availability: GuidanceCardAvailability = GuidanceCardAvailability.AVAILABLE
    pack_requirement: str | None = None
    confidence: float = Field(ge=0.0, le=1.0, default=0.9)
    student_mode_hint: str
    student_mode_hint_fa: str
    suggested_command: dict[str, Any]


class GuidanceCardManager:
    """Generates intelligent guidance cards based on current timeline state and student mode."""

    @staticmethod
    def generate_cards(
        project: Project,
        playhead_us: int,
        range_selection: RangeSelection | None = None,
        student_mode: bool = False,
    ) -> list[GuidanceCard]:
        cards: list[GuidanceCard] = []

        # 1. If Range Selection is active, recommend range operations
        if range_selection and range_selection.is_active:
            r = range_selection.to_time_range_us()
            cards.append(
                GuidanceCard(
                    operation="timeline.ripple_delete_range",
                    title="Ripple Delete Selected Range",
                    title_fa="حذف پیوسته بازه انتخاب‌شده",
                    reason=(
                        f"Remove the {range_selection.duration_us // 1000}ms "
                        "selected range and close the gap."
                    ),
                    reason_fa=(
                        f"حذف بازه انتخاب‌شده به طول "
                        f"{range_selection.duration_us // 1000} میلی‌ثانیه و بستن خودکار فاصله."
                    ),
                    target_type="range",
                    range=r,
                    availability=GuidanceCardAvailability.AVAILABLE,
                    pack_requirement="nexus.edit.timeline",
                    confidence=0.98,
                    student_mode_hint=(
                        "Ripple Delete removes the selected section and pulls "
                        "the rest of the timeline forward seamlessly."
                    ),
                    student_mode_hint_fa=(
                        "«حذف پیوسته» بخش انتخاب‌شده را می‌بُرد و بخش‌های "
                        "بعدی را بدون ایجاد جای خالی به جلو می‌کشد."
                    ),
                    suggested_command={
                        "operation": "timeline.ripple_delete_range",
                        "input": {"range": r.model_dump()},
                    },
                )
            )
            cards.append(
                GuidanceCard(
                    operation="color.adjust_exposure",
                    title="Color Grade Selection",
                    title_fa="تنظیم نور و رنگ این بازه",
                    reason="Improve visual balance and luminance across this selection.",
                    reason_fa="تنظیم روشنایی و تعادل رنگی در این بازه مشخص.",
                    target_type="range",
                    range=r,
                    availability=GuidanceCardAvailability.AVAILABLE,
                    pack_requirement="nexus.color.delivery",
                    confidence=0.85,
                    student_mode_hint="Adjusts brightness and contrast for the selected duration.",
                    student_mode_hint_fa=(
                        "میزان روشنایی و کنتراست تصویر را در این بازه دلخواه هماهنگ می‌کند."
                    ),
                    suggested_command={
                        "operation": "color.adjust_exposure",
                        "input": {"stops": 0.5, "range": r.model_dump()},
                    },
                )
            )

        # 2. Candidate Moments guidance
        moments = MomentGuidanceEngine.detect_candidate_moments(project, playhead_us)
        for m in moments:
            if m.kind == CandidateMomentKind.SILENCE:
                cards.append(
                    GuidanceCard(
                        operation="timeline.ripple_delete_range",
                        title="Cut Detected Silence",
                        title_fa="حذف سکوت شناسایی‌شده",
                        reason=m.reason,
                        reason_fa=m.reason_fa,
                        target_type="range",
                        range=m.range,
                        availability=GuidanceCardAvailability.AVAILABLE,
                        pack_requirement="nexus.edit.timeline",
                        confidence=m.confidence,
                        student_mode_hint=(
                            "Automates podcast/video pacing by removing dead air between sentences."
                        ),
                        student_mode_hint_fa=(
                            "سکوت‌های اضافی میان جملات را پاک می‌کند تا ریتم ویدیو جذاب و سریع‌تر شود."
                        ),
                        suggested_command={
                            "operation": "timeline.ripple_delete_range",
                            "input": {"range": m.range.model_dump()},
                        },
                    )
                )
            elif (
                m.kind == CandidateMomentKind.SCENE_CUT
                and m.suggested_operation == "motion.add_transition"
            ):
                cards.append(
                    GuidanceCard(
                        operation="motion.add_transition",
                        title="Add Smooth Dissolve",
                        title_fa="افزودن ترنزیشن دیزولو",
                        reason=m.reason,
                        reason_fa=m.reason_fa,
                        target_type="range",
                        range=m.range,
                        availability=GuidanceCardAvailability.AVAILABLE,
                        pack_requirement="nexus.motion.graphics",
                        confidence=m.confidence,
                        student_mode_hint=(
                            "Creates a gradual blend between two consecutive video shots."
                        ),
                        student_mode_hint_fa=(
                            "تصویر دو کلیپ مجاور را به‌صورت نرم و تدریجی در یکدیگر محو می‌کند."
                        ),
                        suggested_command={
                            "operation": "motion.add_transition",
                            "input": {"transition_type": "dissolve", "range": m.range.model_dump()},
                        },
                    )
                )

        # 3. Always offer Subtitle generation if near speech
        cards.append(
            GuidanceCard(
                operation="caption.create_subtitle",
                title="Generate Persian RTL Subtitle",
                title_fa="تولید زیرنویس فارسی راست‌به‌چپ",
                reason="Generate word-accurate subtitles with Vazirmatn typography.",
                reason_fa=(
                    "ایجاد زیرنویس دقیق کلمه‌به‌کلمه با فونت وزیرمتن و چیدمان استاندارد راست‌به‌چپ."
                ),
                target_type="playhead",
                range=TimeRangeUS(
                    start_us=playhead_us,
                    end_us=min(project.timeline.duration_us, playhead_us + 3_000_000),
                )
                if playhead_us + 3_000_000 <= project.timeline.duration_us
                else None,
                availability=GuidanceCardAvailability.AVAILABLE,
                pack_requirement="nexus.language.caption",
                confidence=0.90,
                student_mode_hint=(
                    "Automatically transcribes speech and styles it for Persian "
                    "readers with proper bidi alignment."
                ),
                student_mode_hint_fa=(
                    "صدای گوینده را به متن فارسی تبدیل کرده و با تراز صحیح "
                    "راست‌به‌چپ روی ویدیو قرار می‌دهد."
                ),
                suggested_command={
                    "operation": "caption.create_subtitle",
                    "input": {
                        "text": "عنوان نمونه زیرنویس",
                        "start_us": playhead_us,
                        "end_us": min(project.timeline.duration_us, playhead_us + 3_000_000),
                    },
                },
            )
        )

        return cards


# ---------------------------------------------------------------------------
# Missing Pack Experience (Law 8: Graceful, Actionable, Never Crashing)
# ---------------------------------------------------------------------------


class MissingPackReport(BaseModel):
    """Graceful report presented when an operation requires an uninstalled pack."""

    model_config = ConfigDict(extra="forbid")

    package_id: str
    display_name: str
    display_name_fa: str
    why_unavailable: str
    why_unavailable_fa: str
    capabilities_provided: list[str]
    size_mb: float
    status: Literal["not_installed", "available_to_download", "downloading", "installed"]
    install_action: str
    install_action_fa: str


class MissingPackExperience:
    """Builds user-friendly, non-crashing recovery reports for missing packs."""

    CATALOG: dict[str, dict[str, Any]] = {
        "nexus.motion.graphics": {
            "display_name": "Motion Graphics Pack",
            "display_name_fa": "بسته گرافیک و موشن نگار",
            "capabilities": ["motion.add_transition", "motion.keyframe", "motion.kinetic_title"],
            "size_mb": 14.5,
        },
        "nexus.vision.portrait": {
            "display_name": "Portrait AI Enhancer",
            "display_name_fa": "بسته هوش مصنوعی پرتره و چهره",
            "capabilities": [
                "portrait.smooth_skin",
                "portrait.enhance_eyes",
                "portrait.whiten_teeth",
            ],
            "size_mb": 42.0,
        },
        "nexus.vision.scene": {
            "display_name": "Scene AI & Tracking Pack",
            "display_name_fa": "بسته ردیابی و هوش مصنوعی صحنه",
            "capabilities": [
                "scene.detect_shot_boundaries",
                "scene.auto_reframe_subject",
                "scene.remove_background",
            ],
            "size_mb": 65.0,
        },
        "nexus.language.caption": {
            "display_name": "Persian & Multilingual Caption Pack",
            "display_name_fa": "بسته زیرنویس فارسی و چندزبانه",
            "capabilities": [
                "caption.transcribe",
                "caption.generate_ass_rtl",
                "caption.search_transcript",
            ],
            "size_mb": 28.0,
        },
    }

    @classmethod
    def report_for(cls, package_id: str) -> MissingPackReport:
        info = cls.CATALOG.get(
            package_id,
            {
                "display_name": f"Pack {package_id}",
                "display_name_fa": f"بسته {package_id}",
                "capabilities": ["additional creative tools"],
                "size_mb": 10.0,
            },
        )
        return MissingPackReport(
            package_id=package_id,
            display_name=info["display_name"],
            display_name_fa=info["display_name_fa"],
            why_unavailable=(
                f"Feature requires '{info['display_name']}' which is not currently installed."
            ),
            why_unavailable_fa=(
                f"این قابلیت نیازمند «{info['display_name_fa']}» است که هنوز "
                "روی سیستم فعال نشده است."
            ),
            capabilities_provided=info["capabilities"],
            size_mb=info["size_mb"],
            status="available_to_download",
            install_action=f"nexus packs install {package_id}",
            install_action_fa=f"نصب و فعال‌سازی بسته {info['display_name_fa']}",
        )


# ---------------------------------------------------------------------------
# Studio Ergonomics & Shortcuts
# ---------------------------------------------------------------------------


class StudioAction(str, Enum):
    PLAY_PAUSE = "play_pause"
    SHUTTLE_REWIND = "shuttle_rewind"
    SHUTTLE_FORWARD = "shuttle_forward"
    PIN_IN = "pin_in"
    PIN_OUT = "pin_out"
    CLEAR_RANGE = "clear_range"
    SPLIT_CLIP = "split_clip"
    ADD_MARKER = "add_marker"
    UNDO = "undo"
    REDO = "redo"
    RIPPLE_DELETE = "ripple_delete"


class StudioErgonomics:
    """Maps physical keyboard and touch gestures to ergonomic studio actions."""

    KEYBOARD_MAP: dict[str, StudioAction] = {
        " ": StudioAction.PLAY_PAUSE,
        "k": StudioAction.PLAY_PAUSE,
        "j": StudioAction.SHUTTLE_REWIND,
        "l": StudioAction.SHUTTLE_FORWARD,
        "i": StudioAction.PIN_IN,
        "o": StudioAction.PIN_OUT,
        "x": StudioAction.CLEAR_RANGE,
        "s": StudioAction.SPLIT_CLIP,
        "m": StudioAction.ADD_MARKER,
        "z": StudioAction.UNDO,
        "y": StudioAction.REDO,
        "backspace": StudioAction.RIPPLE_DELETE,
        "delete": StudioAction.RIPPLE_DELETE,
    }

    @classmethod
    def resolve_key(
        cls, key: str, is_cmd_or_ctrl: bool = False, is_shift: bool = False
    ) -> StudioAction | None:
        k = key.lower()
        if is_cmd_or_ctrl:
            if k == "z":
                return StudioAction.REDO if is_shift else StudioAction.UNDO
            if k == "y":
                return StudioAction.REDO
        return cls.KEYBOARD_MAP.get(k)


# ---------------------------------------------------------------------------
# Accessibility & ARIA Announcements
# ---------------------------------------------------------------------------


class StudioAccessibility:
    """Generates bilingual, high-clarity accessibility cues for assistive tech."""

    @staticmethod
    def playhead_announcement(playhead_us: int, lang: Literal["fa", "en"] = "fa") -> str:
        secs = playhead_us / MICROSECONDS_PER_SECOND
        if lang == "fa":
            return f"مکان‌نما در ثانیه {secs:.2f} قرار گرفت."
        return f"Playhead at {secs:.2f} seconds."

    @staticmethod
    def range_announcement(start_us: int, end_us: int, lang: Literal["fa", "en"] = "fa") -> str:
        s = start_us / MICROSECONDS_PER_SECOND
        e = end_us / MICROSECONDS_PER_SECOND
        dur = (end_us - start_us) / MICROSECONDS_PER_SECOND
        if lang == "fa":
            return f"بازه از ثانیه {s:.2f} تا {e:.2f} انتخاب شد (طول: {dur:.2f} ثانیه)."
        return f"Range selected from {s:.2f}s to {e:.2f}s (duration: {dur:.2f}s)."

    @staticmethod
    def card_announcement(card: GuidanceCard, lang: Literal["fa", "en"] = "fa") -> str:
        if lang == "fa":
            return f"پیشنهاد هوشمند: {card.title_fa}. دلیل: {card.reason_fa}"
        return f"Guidance suggestion: {card.title}. Reason: {card.reason}"
