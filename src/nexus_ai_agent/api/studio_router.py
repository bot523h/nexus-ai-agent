"""Nagar Creative Studio -- API Router and Web Operating System Surface.

Owned by Agent 2 (Interaction Designer + Product UX Architect + Creative Systems Engineer).

Provides:
- GET /api/studio/ui: The live interactive, accessible, RTL-first Creative Studio Web Interface
- Complete API endpoints for timeline interaction, shared playhead, moment guidance,
  range selection, assistant guidance cards, student mode, and preview vs master.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

import nexus_ai_agent.creative.studio.bus as studio_bus
from nexus_ai_agent.creative.studio.capabilities import build_wave1_registry
from nexus_ai_agent.creative.studio.engine import (
    RedoStackEmptyError,
    SessionSnapshot,
    StudioSession,
)
from nexus_ai_agent.creative.studio.interaction import (
    CandidateMoment,
    GuidanceCard,
    MissingPackReport,
    PlayheadCursorSource,
    SharedPlayheadCursor,
)
from nexus_ai_agent.creative.studio.models import (
    Clip,
    CommandResult,
    MediaRef,
    TimeBase,
    Timeline,
    TimeRangeUS,
    Track,
    UndoStackEmptyError,
    new_project,
)
from nexus_ai_agent.creative.studio.preview import (
    AudioWaveformSample,
    FidelityDiff,
    PreviewFrameData,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/studio", tags=["creative-studio"])

HTML_FILE = Path(__file__).parent / "templates" / "studio.html"

# In-memory shared studio session for the live web surface
_active_session: StudioSession | None = None


def get_or_create_session() -> StudioSession:
    """Retrieve or initialize the active interactive Studio Session."""
    global _active_session
    if _active_session is None:
        tb = TimeBase(numerator=30, denominator=1)
        media = MediaRef(
            asset_id="asset_demo_01",
            content_sha256=(
                "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
            ),
            media_kind="video",
            duration_us=30_000_000,
            timebase=tb,
        )
        clip1 = Clip(
            clip_id="clip_intro",
            media_ref=media,
            source_range=TimeRangeUS(start_us=0, end_us=12_000_000),
            timeline_range=TimeRangeUS(start_us=0, end_us=12_000_000),
        )
        clip2 = Clip(
            clip_id="clip_main",
            media_ref=media,
            source_range=TimeRangeUS(start_us=14_000_000, end_us=28_000_000),
            timeline_range=TimeRangeUS(start_us=14_000_000, end_us=28_000_000),
        )
        track = Track(
            track_id="video_track_1", name="Main Video", kind="video", clips=[clip1, clip2]
        )
        tl = Timeline(timeline_id="tl_master", duration_us=30_000_000, tracks=[track])
        proj = new_project(
            project_id="proj_studio_live", name="Nagar Creative Project", timeline=tl
        )
        reg = build_wave1_registry()
        bus = studio_bus.CommandBus(proj, registry=reg)
        _active_session = StudioSession(proj, command_bus=bus, registry=reg)

        # Seed sample Persian subtitle
        _active_session.add_subtitle_cue(
            start_us=1_000_000,
            end_us=4_500_000,
            text="سلام به استودیوی خلاق نگار؛ ویرایش هوشمند و بی‌دردسر ویدیو.",
        )
    return _active_session


# ---------------------------------------------------------------------------
# API DTO Models
# ---------------------------------------------------------------------------


class SeekRequest(BaseModel):
    timecode_us: int = Field(ge=0)
    source: PlayheadCursorSource = PlayheadCursorSource.USER_SCRUB


class RangeRequest(BaseModel):
    action: str = Field(default="set_range", description="pin_start | pin_end | set_range | clear")
    start_us: int | None = None
    end_us: int | None = None


class ContextualIntentRequest(BaseModel):
    intent_text: str = Field(min_length=1, max_length=500)
    suggested_operation: str = "timeline.ripple_delete_range"


# ---------------------------------------------------------------------------
# API Endpoints
# ---------------------------------------------------------------------------


@router.get("/session", response_model=SessionSnapshot)
async def get_session_state() -> SessionSnapshot:
    """Return the current active studio session state snapshot."""
    session = get_or_create_session()
    return session.snapshot()


@router.post("/seek", response_model=SharedPlayheadCursor)
async def seek_playhead(req: SeekRequest) -> SharedPlayheadCursor:
    """Seek the shared playhead cursor to a specific timestamp."""
    session = get_or_create_session()
    return session.seek(req.timecode_us, source=req.source)


@router.post("/hold", response_model=SharedPlayheadCursor)
async def hold_playhead() -> SharedPlayheadCursor:
    """Freeze/hold the shared playhead cursor for moment inspection."""
    session = get_or_create_session()
    return session.hold_moment()


@router.post("/release-hold", response_model=SharedPlayheadCursor)
async def release_hold() -> SharedPlayheadCursor:
    """Release the playhead hold."""
    session = get_or_create_session()
    return session.release_hold()


@router.post("/range")
async def update_range(req: RangeRequest) -> dict[str, Any]:
    """Update timeline In/Out range selection."""
    session = get_or_create_session()
    if req.action == "pin_start":
        r = session.pin_start(req.start_us)
    elif req.action == "pin_end":
        r = session.pin_end(req.end_us)
    elif req.action == "set_range":
        if req.start_us is None or req.end_us is None:
            raise HTTPException(status_code=400, detail="start_us and end_us are required")
        r = session.set_range(req.start_us, req.end_us)
    elif req.action == "clear":
        r = session.clear_range()
    else:
        raise HTTPException(status_code=400, detail=f"unknown action: {req.action}")
    return r.model_dump()


@router.post("/command", response_model=CommandResult)
async def dispatch_command(cmd: dict[str, Any]) -> CommandResult:
    """Dispatch a typed studio command through the atomic command bus."""
    session = get_or_create_session()
    try:
        return session.dispatch(cmd)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/undo", response_model=CommandResult)
async def undo_transaction() -> CommandResult:
    """Undo the most recent editable transaction."""
    session = get_or_create_session()
    try:
        return session.undo()
    except UndoStackEmptyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/redo", response_model=CommandResult)
async def redo_transaction() -> CommandResult:
    """Redo the most recently undone transaction."""
    session = get_or_create_session()
    try:
        return session.redo()
    except RedoStackEmptyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/cards", response_model=list[GuidanceCard])
async def get_guidance_cards() -> list[GuidanceCard]:
    """Retrieve smart assistant guidance cards for the active timeline position."""
    session = get_or_create_session()
    return session.get_guidance_cards()


@router.get("/moments", response_model=list[CandidateMoment])
async def get_candidate_moments() -> list[CandidateMoment]:
    """Detect candidate moments around the shared playhead cursor."""
    session = get_or_create_session()
    return session.detect_moments()


@router.get("/preview-frame", response_model=PreviewFrameData)
async def get_preview_frame() -> PreviewFrameData:
    """Retrieve instant low-latency preview frame metadata."""
    session = get_or_create_session()
    return session.get_preview_frame()


@router.get("/waveform", response_model=list[AudioWaveformSample])
async def get_audio_waveform(bins: int = 100) -> list[AudioWaveformSample]:
    """Retrieve interactive audio waveform envelope data."""
    session = get_or_create_session()
    return session.get_waveform(bins=bins)


@router.get("/fidelity-diff", response_model=FidelityDiff)
async def get_fidelity_diff() -> FidelityDiff:
    """Report Preview vs Master fidelity differences."""
    session = get_or_create_session()
    return session.get_fidelity_diff()


@router.get("/missing-pack/{package_id}", response_model=MissingPackReport)
async def get_missing_pack_report(package_id: str) -> MissingPackReport:
    """Retrieve actionable explanation for an uninstalled pack."""
    session = get_or_create_session()
    return session.get_missing_pack_report(package_id)


@router.post("/student-mode")
async def toggle_student_mode() -> dict[str, bool]:
    """Toggle beginner-friendly Student Mode progressive disclosure."""
    session = get_or_create_session()
    active = session.toggle_student_mode()
    return {"student_mode": active}


@router.post("/contextual-intent")
async def execute_contextual_intent(req: ContextualIntentRequest) -> dict[str, Any]:
    """Resolve conversational intent ('همین قسمت رو تمیز کن') and execute."""
    session = get_or_create_session()
    try:
        resolved_range = session.resolve_context_range(req.intent_text)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    msg_fa = (
        f"بازه از {resolved_range.start_us // 1000}ms تا "
        f"{resolved_range.end_us // 1000}ms تشخیص داده شد."
    )
    return {
        "status": "resolved",
        "resolved_range": resolved_range.model_dump(),
        "action": req.suggested_operation,
        "message_fa": msg_fa,
    }


@router.get("/ui", response_class=HTMLResponse)
async def get_studio_ui() -> str:
    """Serve the complete, accessible, Persian-RTL Creative Studio Web Application."""
    if HTML_FILE.exists():
        return HTML_FILE.read_text(encoding="utf-8")
    return "<html><body><h1>Nagar Creative Studio</h1></body></html>"
