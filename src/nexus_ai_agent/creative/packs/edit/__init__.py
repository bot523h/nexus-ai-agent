"""``nexus.edit.timeline`` — Local non-destructive timeline editing, trimming,
ripple delete, and speed ramping (Wave 3 substrate).

This package provides the pure substrate for Nagar timeline editing:
* typed data models (:class:`TrimInput`, :class:`RippleDeleteInput`, :class:`InsertGapInput`,
  :class:`SpeedRampInput`, :class:`ReverseSegmentInput`, :class:`FreezeFrameInput`,
  :class:`AttachBRollInput`, :class:`RetimeToMusicInput`);
* pure capability operations: ``timeline.trim``, ``timeline.ripple_delete``,
  ``timeline.insert_gap``, ``timeline.speed_ramp``, ``timeline.reverse_segment``,
  ``timeline.freeze_frame``, ``timeline.attach_b_roll``, and ``timeline.retime_to_music``.

In accordance with Nagar substrate architecture, this package has zero heavy dependencies
(no PyTorch, OpenCV, or external C-extensions).
"""

from __future__ import annotations

from nexus_ai_agent.creative.packs.edit.models import (
    DOMAIN,
    EDIT_PACKAGE_ID,
    OPERATION_ATTACH_B_ROLL,
    OPERATION_FREEZE_FRAME,
    OPERATION_INSERT_GAP,
    OPERATION_RETIME_TO_MUSIC,
    OPERATION_REVERSE_SEGMENT,
    OPERATION_RIPPLE_DELETE,
    OPERATION_SPEED_RAMP,
    OPERATION_TRIM,
    AttachBRollInput,
    FreezeFrameInput,
    InsertGapInput,
    RetimeToMusicInput,
    ReverseSegmentInput,
    RippleDeleteInput,
    SpeedRampInput,
    TrimInput,
)
from nexus_ai_agent.creative.packs.edit.operations import (
    build_edit_registry,
    register_edit_operations,
)

__all__ = [
    "DOMAIN",
    "EDIT_PACKAGE_ID",
    "OPERATION_ATTACH_B_ROLL",
    "OPERATION_FREEZE_FRAME",
    "OPERATION_INSERT_GAP",
    "OPERATION_RETIME_TO_MUSIC",
    "OPERATION_REVERSE_SEGMENT",
    "OPERATION_RIPPLE_DELETE",
    "OPERATION_SPEED_RAMP",
    "OPERATION_TRIM",
    "AttachBRollInput",
    "FreezeFrameInput",
    "InsertGapInput",
    "RetimeToMusicInput",
    "ReverseSegmentInput",
    "RippleDeleteInput",
    "SpeedRampInput",
    "TrimInput",
    "build_edit_registry",
    "register_edit_operations",
]
