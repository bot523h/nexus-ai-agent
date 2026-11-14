"""``nexus_ai_agent.creative.otio`` — the canonical Nagar ↔ OTIO interchange.

One module pair, two directions, one documented loss contract:

* :mod:`nexus_ai_agent.creative.otio.convert` — ``project_to_otio_document`` /
  ``parse_otio_document`` / ``LOSS_CONTRACT``;
* :mod:`nexus_ai_agent.creative.otio.document` — the normalized parsed models
  and the fixed ``Marker.2`` colour palette.

This is the **only** OTIO interchange implementation in the creative tree; the
``delivery.export_otio`` handler is scheduled to delegate here (see the
BLOCKED_SHARED_CONTRACT handoff — that call site lives in a file held by the
open PR#33).
"""

from __future__ import annotations

from nexus_ai_agent.creative.otio.convert import (
    LOSS_CONTRACT,
    NAGAR_METADATA_KEY,
    OtioConversionError,
    dumps_otio_document,
    frames_value_to_us,
    marker_otio_color,
    parse_otio_document,
    project_to_otio_document,
    us_to_frames_value,
)
from nexus_ai_agent.creative.otio.document import (
    MARKER_COLOR_FALLBACK,
    MARKER_COLOR_PALETTE,
    LossEntry,
    ParsedClip,
    ParsedGap,
    ParsedMarker,
    ParsedMediaRef,
    ParsedRangeUS,
    ParsedTimelineState,
    ParsedTrack,
)

__all__ = [
    "LOSS_CONTRACT",
    "MARKER_COLOR_FALLBACK",
    "MARKER_COLOR_PALETTE",
    "NAGAR_METADATA_KEY",
    "LossEntry",
    "OtioConversionError",
    "ParsedClip",
    "ParsedGap",
    "ParsedMarker",
    "ParsedMediaRef",
    "ParsedRangeUS",
    "ParsedTimelineState",
    "ParsedTrack",
    "dumps_otio_document",
    "frames_value_to_us",
    "marker_otio_color",
    "parse_otio_document",
    "project_to_otio_document",
    "us_to_frames_value",
]
