"""Nagar Creative Studio -- Studio Session and Creative Operating System Engine.

Owned by Agent 2 (Principal Creative Systems Engineer + Product UX Architect).

Unifies all studio interaction components into a cohesive Creative OS:
- "Think -> Speak -> Point -> Edit -> Preview -> Refine -> Export" workflow
- Shared playhead cursor bridge ("play -> hold -> identify t -> command")
- In/Out Range selection ("pin start -> pin end -> range command")
- Contextual intent resolution ("همین قسمت رو تمیز کن")
- Assistant guidance cards & Student Mode progressive disclosure
- Bidirectional history: Reversible Undo and Redo with state-hash verification
- Preview vs Master two-tier pipeline
- Persian RTL Subtitle track management
- Graceful missing-pack discovery and reporting
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from nexus_ai_agent.creative.studio.capabilities import (
    CapabilityRegistry,
    build_wave1_registry,
)
from nexus_ai_agent.creative.studio.interaction import (
    CandidateMoment,
    ContextualResolver,
    GuidanceCard,
    GuidanceCardManager,
    MissingPackExperience,
    MissingPackReport,
    MomentGuidanceEngine,
    PlayheadCursorSource,
    RangeSelection,
    SharedPlayheadCursor,
    StudioAccessibility,
    StudioAction,
    StudioErgonomics,
)
from nexus_ai_agent.creative.studio.models import (
    CommandResult,
    EditTransaction,
    NagarError,
    PermissionLevel,
    Project,
    TimeRangeUS,
    TypedCommand,
    UndoStackEmptyError,
    compute_state_hash,
)
from nexus_ai_agent.creative.studio.preview import (
    AudioWaveformSample,
    FidelityDiff,
    MasterProfile,
    PipelineMode,
    PreviewFrameData,
    PreviewPipelineEngine,
    PreviewProfile,
)
from nexus_ai_agent.creative.studio.subtitles import SubtitleCue, SubtitleTrack


class RedoStackEmptyError(NagarError):
    """Raised when system.redo is invoked but the redo history is empty."""


class SessionSnapshot(BaseModel):
    """Serialisable state snapshot of the active Studio Session."""

    model_config = ConfigDict(extra="forbid")

    session_id: str
    project_id: str
    project_name: str
    state_revision: int
    state_hash: str
    cursor_timecode_us: int
    cursor_is_holding: bool
    cursor_source: str
    range_is_active: bool
    range_in_us: int | None
    range_out_us: int | None
    student_mode: bool
    pipeline_mode: str
    undo_count: int
    redo_count: int
    subtitles_count: int


class StudioSession:
    """The central Creative Operating System runtime session.

    Transforms Nagar from a loose backend tool collection into an integrated
    Creative OS with minimal cognitive friction.
    """

    def __init__(
        self,
        project: Project,
        command_bus: Any,
        registry: CapabilityRegistry | None = None,
        session_id: str | None = None,
    ) -> None:
        self.session_id = session_id or f"studio_{uuid.uuid4().hex[:12]}"
        self.registry = registry or build_wave1_registry()
        if command_bus is None:
            raise ValueError("command_bus must be provided to StudioSession")
        self.bus = command_bus
        self.cursor = SharedPlayheadCursor(
            timecode_us=project.timeline.playhead.timecode_us,
            timebase=project.timeline.playhead.timebase,
        )
        self.range_selection = RangeSelection()
        self.subtitle_track = SubtitleTrack()
        self.student_mode: bool = False
        self.preview_profile = PreviewProfile()
        self.master_profile = MasterProfile()
        self.pipeline_mode: PipelineMode = PipelineMode.PREVIEW
        self.redo_stack: list[Project] = []

    @property
    def project(self) -> Project:
        """The authoritative current in-memory project state."""
        return self.bus.project

    # -----------------------------------------------------------------------
    # Timeline Transport & Shared Cursor (Law 3 & Law 4)
    # -----------------------------------------------------------------------

    def seek(
        self,
        timecode_us: int,
        source: PlayheadCursorSource = PlayheadCursorSource.USER_SCRUB,
        label: str | None = None,
    ) -> SharedPlayheadCursor:
        """Seek playhead cursor to a specific microsecond position (Law 4 Shared Bridge)."""
        dur = self.project.timeline.duration_us
        t = max(0, min(dur, timecode_us))
        self.cursor = self.cursor.seek(t, source=source, label=label)
        new_tl = self.project.timeline.model_copy(
            update={"playhead": self.cursor.to_playhead()}
        )
        new_proj = self.project.model_copy(update={"timeline": new_tl})
        new_proj.state_hash = compute_state_hash(new_proj)
        self.bus._project = new_proj
        return self.cursor

    def hold_moment(self, label: str | None = "Inspected Moment") -> SharedPlayheadCursor:
        """Freeze/hold playhead for moment inspection ('play -> hold -> identify t -> command')."""
        self.cursor = self.cursor.hold(label=label)
        new_tl = self.project.timeline.model_copy(
            update={"playhead": self.cursor.to_playhead()}
        )
        new_proj = self.project.model_copy(update={"timeline": new_tl})
        new_proj.state_hash = compute_state_hash(new_proj)
        self.bus._project = new_proj
        return self.cursor

    def release_hold(self) -> SharedPlayheadCursor:
        """Release the hold and restore regular cursor mode."""
        self.cursor = self.cursor.release_hold()
        return self.cursor

    # -----------------------------------------------------------------------
    # Range Selection (Law 3: "pin start -> pin end -> range command")
    # -----------------------------------------------------------------------

    def pin_start(self, timecode_us: int | None = None) -> RangeSelection:
        """Pin In-point at cursor or specified timecode."""
        t = self.cursor.timecode_us if timecode_us is None else timecode_us
        self.range_selection = self.range_selection.pin_start(t)
        return self.range_selection

    def pin_end(self, timecode_us: int | None = None) -> RangeSelection:
        """Pin Out-point at cursor or specified timecode."""
        t = self.cursor.timecode_us if timecode_us is None else timecode_us
        self.range_selection = self.range_selection.pin_end(t)
        return self.range_selection

    def set_range(self, start_us: int, end_us: int, label: str | None = None) -> RangeSelection:
        """Set explicit In/Out range."""
        self.range_selection = self.range_selection.set_range(start_us, end_us, label=label)
        return self.range_selection

    def clear_range(self) -> RangeSelection:
        """Clear active range selection."""
        self.range_selection = self.range_selection.clear()
        return self.range_selection

    # -----------------------------------------------------------------------
    # Contextual Intent & Command Dispatch (Law 9: Reduce Interactions)
    # -----------------------------------------------------------------------

    def resolve_context_range(self, expression: str) -> TimeRangeUS:
        """Resolve conversational intent ('همین قسمت', 'این تکه') into exact TimeRangeUS."""
        return ContextualResolver.resolve_range(
            expression,
            self.project,
            self.cursor.timecode_us,
            self.range_selection,
        )

    def dispatch(self, command: TypedCommand | dict[str, Any]) -> CommandResult:
        """Dispatch a typed command to the authoritative CommandBus.

        Clears the redo stack on new forward state changes, preserving state hash integrity.
        """
        # Save snapshot of pre-state for possible redo invalidation
        result = self.bus.dispatch(command)
        # A new destructive/reversible forward operation invalidates old redo branch
        if isinstance(command, dict):
            op = command.get("operation", "")
        else:
            op = command.operation

        if op not in ("system.undo", "system.redo"):
            self.redo_stack.clear()

        # Sync cursor timebase and position
        self.cursor = SharedPlayheadCursor(
            timecode_us=self.project.timeline.playhead.timecode_us,
            timebase=self.project.timeline.playhead.timebase,
            source=PlayheadCursorSource.COMMAND_TARGET,
            is_playing=self.project.timeline.playhead.is_playing,
        )
        return result

    # -----------------------------------------------------------------------
    # Bidirectional Reversible History (Undo + Redo)
    # -----------------------------------------------------------------------

    def undo(self) -> CommandResult:
        """Rewind the most recent editable transaction and store it on redo stack."""
        if not self.bus.history:
            raise UndoStackEmptyError("nothing to undo: history is empty")

        # Capture the current project state before undoing so redo can restore it
        current_state = self.project

        cmd = TypedCommand(
            command_id=f"cmd_undo_{uuid.uuid4().hex[:8]}",
            operation="system.undo",
            input={},
        )
        result = self.bus.dispatch(cmd)
        # Push the undone forward state onto redo stack
        self.redo_stack.append(current_state)
        return result

    def redo(self) -> CommandResult:
        """Redo the most recently undone transaction."""
        if not self.redo_stack:
            raise RedoStackEmptyError("nothing to redo: redo stack is empty")

        forward_project = self.redo_stack.pop()
        # Restore forward project state into bus
        old_hash = self.bus.project.state_hash
        # Atomically apply forward state
        self.bus._project = forward_project
        self.bus._project.state_revision += 1

        tx = EditTransaction(
            transaction_id=f"tx_redo_{uuid.uuid4().hex[:8]}",
            command_id=f"cmd_redo_{uuid.uuid4().hex[:8]}",
            operation="system.redo",
            permission_level=PermissionLevel.REVERSIBLE,
            parent_revision=forward_project.state_revision - 1,
            previous_state_hash=old_hash,
            new_state_hash=forward_project.state_hash,
            state_before=self.bus.project.model_dump(),
        )
        self.bus._history.append(tx)

        return CommandResult(
            command_id=tx.command_id,
            transaction_id=tx.transaction_id,
            state_revision=self.bus._project.state_revision,
            state_hash=self.bus._project.state_hash,
            output={"restored_state_hash": forward_project.state_hash},
        )

    # -----------------------------------------------------------------------
    # Guidance Cards & Student Mode (Law 7)
    # -----------------------------------------------------------------------

    def get_guidance_cards(self) -> list[GuidanceCard]:
        """Generate smart guidance cards tailored to the current context and student mode."""
        return GuidanceCardManager.generate_cards(
            self.project,
            self.cursor.timecode_us,
            self.range_selection,
            self.student_mode,
        )

    def detect_moments(self) -> list[CandidateMoment]:
        """Detect candidate moments around the shared playhead cursor."""
        return MomentGuidanceEngine.detect_candidate_moments(self.project, self.cursor.timecode_us)

    def toggle_student_mode(self) -> bool:
        """Toggle Student Mode progressive disclosure on or off."""
        self.student_mode = not self.student_mode
        return self.student_mode

    # -----------------------------------------------------------------------
    # Preview vs Master Pipeline (Law 6)
    # -----------------------------------------------------------------------

    def get_preview_frame(self) -> PreviewFrameData:
        """Retrieve low-latency proxy preview frame at the current playhead cursor."""
        return PreviewPipelineEngine.render_preview_frame(
            self.project,
            self.cursor.timecode_us,
            self.preview_profile,
        )

    def get_waveform(self, bins: int = 100) -> list[AudioWaveformSample]:
        """Retrieve interactive audio waveform envelope data."""
        return PreviewPipelineEngine.generate_waveform(self.project, bins_count=bins)

    def get_fidelity_diff(self) -> FidelityDiff:
        """Report exact differences between Draft Preview and Final Master."""
        return PreviewPipelineEngine.get_fidelity_diff(self.preview_profile, self.master_profile)

    def set_pipeline_mode(self, mode: PipelineMode) -> PipelineMode:
        """Set active pipeline view mode ('preview' vs 'master')."""
        self.pipeline_mode = mode
        return self.pipeline_mode

    # -----------------------------------------------------------------------
    # Persian RTL Subtitles (Law 5)
    # -----------------------------------------------------------------------

    def add_subtitle_cue(
        self, start_us: int, end_us: int, text: str, speaker_id: str | None = None
    ) -> SubtitleCue:
        """Add a Persian RTL styled subtitle cue."""
        return self.subtitle_track.add_cue(start_us, end_us, text, speaker_id)

    def search_subtitles(self, query: str) -> list[SubtitleCue]:
        """Search subtitle cues using Persian-normalized fuzzy matching."""
        return self.subtitle_track.search_cues(query)

    # -----------------------------------------------------------------------
    # Missing Pack Graceful Experience (Law 8)
    # -----------------------------------------------------------------------

    @staticmethod
    def get_missing_pack_report(package_id: str) -> MissingPackReport:
        """Generate non-crashing, actionable explanation for an uninstalled pack."""
        return MissingPackExperience.report_for(package_id)

    # -----------------------------------------------------------------------
    # Ergonomics & Accessibility
    # -----------------------------------------------------------------------

    def handle_shortcut(
        self, key: str, is_cmd_or_ctrl: bool = False, is_shift: bool = False
    ) -> StudioAction | None:
        """Process a keyboard shortcut according to studio ergonomics."""
        action = StudioErgonomics.resolve_key(key, is_cmd_or_ctrl=is_cmd_or_ctrl, is_shift=is_shift)
        if action == StudioAction.PIN_IN:
            self.pin_start()
        elif action == StudioAction.PIN_OUT:
            self.pin_end()
        elif action == StudioAction.CLEAR_RANGE:
            self.clear_range()
        elif action == StudioAction.UNDO:
            self.undo()
        elif action == StudioAction.REDO:
            self.redo()
        return action

    def get_accessibility_cue(self, lang: Literal["fa", "en"] = "fa") -> str:
        """Return accessibility announcement for screen readers."""
        if self.range_selection.is_active and self.range_selection.in_us is not None:
            return StudioAccessibility.range_announcement(
                self.range_selection.in_us,
                self.range_selection.out_us or self.project.timeline.duration_us,
                lang=lang,
            )
        return StudioAccessibility.playhead_announcement(self.cursor.timecode_us, lang=lang)

    # -----------------------------------------------------------------------
    # Session Serialization
    # -----------------------------------------------------------------------

    def snapshot(self) -> SessionSnapshot:
        """Return a structured overview of the studio session state."""
        return SessionSnapshot(
            session_id=self.session_id,
            project_id=self.project.project_id,
            project_name=self.project.name,
            state_revision=self.project.state_revision,
            state_hash=self.project.state_hash,
            cursor_timecode_us=self.cursor.timecode_us,
            cursor_is_holding=self.cursor.is_holding,
            cursor_source=self.cursor.source.value,
            range_is_active=self.range_selection.is_active,
            range_in_us=self.range_selection.in_us,
            range_out_us=self.range_selection.out_us,
            student_mode=self.student_mode,
            pipeline_mode=self.pipeline_mode.value,
            undo_count=len(self.bus.history),
            redo_count=len(self.redo_stack),
            subtitles_count=len(self.subtitle_track.cues),
        )


def create_studio_session(
    project: Project,
    command_bus: Any,
    registry: CapabilityRegistry | None = None,
    session_id: str | None = None,
) -> StudioSession:
    """Factory helper to build a fresh, fully-configured StudioSession."""
    return StudioSession(
        project=project,
        command_bus=command_bus,
        registry=registry,
        session_id=session_id,
    )
