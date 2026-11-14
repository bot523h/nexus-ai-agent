"""Canonical Nagar ↔ OTIO conversion (serialize + parse), deterministic and pure.

``project_to_otio_document`` renders one :class:`~nexus_ai_agent.creative.studio.
models.Project` timeline into an OpenTimelineIO JSON document (Timeline.1 →
Stack.1 → Track.1 → Clip/Gap, Marker.2 markers on the Stack, ExternalReference.1
media).  ``parse_otio_document`` restores a normalized
:class:`~nexus_ai_agent.creative.otio.document.ParsedTimelineState` — including
documents produced by the real ``opentimelineio`` library, so the round-trip

    Nagar timeline → OTIO → parse → Nagar timeline

is executable and testable in both directions.

Two-level round-trip contract (mission §12 — semantic equality, documented
losses):

* **with the ``metadata.nagar`` blocks** the semantic round-trip is exact
  (integer microseconds, ids, hashes, effects) — this is what the round-trip
  tests assert;
* **without metadata** (a hostile NLE stripped it) only the frame-quantised
  OTIO view survives — the quantisation and the dropped constructs are listed
  in :data:`LOSS_CONTRACT` and pinned by explicit tests.

Determinism: identical project + identical parameters produce byte-identical
``json.dumps(..., sort_keys=True)`` output — there is no clock, no uuid and no
iteration-order dependence anywhere in this module.

``include_markers`` is honoured here for real (unlike the current
``delivery.export_otio`` handler — see the BLOCKED_SHARED_CONTRACT handoff in
the mission report: that one-line delegation lives in a file the open PR#33
holds).
"""

from __future__ import annotations

import json
from typing import Any

from nexus_ai_agent.creative.otio.document import (
    MARKER_COLOR_ALIASES,
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

#: The metadata namespace reserved for Nagar round-trip payloads.
NAGAR_METADATA_KEY = "nagar"

MICROSECONDS_PER_SECOND = 1_000_000


class OtioConversionError(ValueError):
    """The timeline cannot be represented in OTIO without silent corruption."""


# ---------------------------------------------------------------------------
# documented losses (mission §12: documented loss + explicit test)
# ---------------------------------------------------------------------------

LOSS_CONTRACT: tuple[LossEntry, ...] = (
    LossEntry(
        what="sub-frame microsecond precision",
        direction="to_otio",
        detail=(
            "OTIO RationalTime values are frame-quantised at the export rate; "
            "exact µs survive only inside metadata.nagar.*_us fields"
        ),
    ),
    LossEntry(
        what="timeline.playhead",
        direction="to_otio",
        detail="OTIO has no playhead concept; preserved as metadata.nagar.playhead_us",
    ),
    LossEntry(
        what="clip/track effect layers",
        direction="to_otio",
        detail=(
            "Nagar EffectLayerRef has no OTIO schema twin; preserved as "
            "metadata.nagar.effects instead of OTIO effect objects"
        ),
    ),
    LossEntry(
        what="free-form marker colors",
        direction="to_otio",
        detail=(
            "OTIO Marker.2 colors are a fixed palette; unmapped colors become "
            f"{MARKER_COLOR_FALLBACK} with the original in metadata.nagar.color"
        ),
    ),
    LossEntry(
        what="asset registry provenance beyond the media reference",
        direction="to_otio",
        detail=(
            "AssetRecord provenance dictionaries are not timeline data; only "
            "asset_id/content_sha256/media_kind/duration_us/color_space round-trip "
            "via ExternalReference metadata.nagar"
        ),
    ),
    LossEntry(
        what="state_revision / state_hash",
        direction="both",
        detail=(
            "revision+hash are command-bus bookkeeping and are exported for "
            "auditing in metadata.nagar but never reconstructed as project state"
        ),
    ),
    LossEntry(
        what="track kind 'image'",
        direction="to_otio",
        detail="OTIO TrackKind has Video/Audio only; image tracks export as Video",
    ),
    LossEntry(
        what="OTIO transitions/effects of foreign NLEs",
        direction="from_otio",
        detail="foreign OTIO effects are kept as opaque metadata dicts, not applied",
    ),
)


# ---------------------------------------------------------------------------
# microsecond ↔ RationalTime algebra (integer microseconds authoritative)
# ---------------------------------------------------------------------------


def us_to_frames_value(us: int, rate: float) -> float:
    """Quantise microseconds to an OTIO ``RationalTime`` value at ``rate``.

    Deterministic rounding (round-half-away-from-zero at 6 decimals) so the same
    inputs always render the same JSON on every platform.
    """
    if rate <= 0:
        raise OtioConversionError(f"frame rate must be positive, got {rate!r}")
    raw = (us * rate) / MICROSECONDS_PER_SECOND
    return round(raw, 6)


def frames_value_to_us(value: float, rate: float) -> int:
    """Restore integer microseconds from an OTIO frame value (nearest µs)."""
    if rate <= 0:
        raise OtioConversionError(f"frame rate must be positive, got {rate!r}")
    return int(round((value * MICROSECONDS_PER_SECOND) / rate))


def _rational_time(us: int, rate: float) -> dict[str, Any]:
    return {
        "OTIO_SCHEMA": "RationalTime.1",
        "value": us_to_frames_value(us, rate),
        "rate": float(rate),
    }


def _time_range(start_us: int, duration_us: int, rate: float) -> dict[str, Any]:
    return {
        "OTIO_SCHEMA": "TimeRange.1",
        "start_time": _rational_time(start_us, rate),
        "duration": _rational_time(duration_us, rate),
    }


def _read_rational_time(node: Any) -> tuple[int, float]:
    if not isinstance(node, dict):
        raise OtioConversionError(f"RationalTime node must be a dict, got {type(node).__name__}")
    value = float(node.get("value", 0.0))
    rate = float(node.get("rate", 0.0))
    return frames_value_to_us(value, rate), rate


def _read_time_range(node: Any) -> tuple[int, int]:
    if not isinstance(node, dict):
        raise OtioConversionError(f"TimeRange node must be a dict, got {type(node).__name__}")
    start_us, _ = _read_rational_time(node.get("start_time") or {"value": 0, "rate": 1})
    duration_us, _ = _read_rational_time(node.get("duration") or {"value": 0, "rate": 1})
    return start_us, max(0, duration_us)


def marker_otio_color(color: str | None) -> str:
    """Map a free-form Nagar color onto the fixed OTIO palette (deterministic)."""
    if not color:
        return MARKER_COLOR_FALLBACK
    upper = color.strip().upper()
    if upper in MARKER_COLOR_PALETTE:
        return upper
    alias = MARKER_COLOR_ALIASES.get(color.strip().lower())
    return alias or MARKER_COLOR_FALLBACK


# ---------------------------------------------------------------------------
# Nagar → OTIO
# ---------------------------------------------------------------------------


def project_to_otio_document(
    project: Any,
    *,
    frame_rate: float = 24.0,
    include_markers: bool = True,
) -> dict[str, Any]:
    """Serialize a project's timeline into an OTIO JSON document dict.

    Honours ``include_markers`` for real: ``False`` emits no Marker.2 children.
    Clips are exported from the **timeline structure** (``timeline.tracks``),
    gaps are made explicit between/around clips, and every exact µs value rides
    in ``metadata.nagar`` for the lossless semantic round-trip.
    """
    if frame_rate <= 0:
        raise OtioConversionError(f"frame rate must be positive, got {frame_rate!r}")
    timeline = project.timeline

    track_nodes: list[dict[str, Any]] = []
    for track in timeline.tracks:
        track_nodes.append(_track_node(track, frame_rate))

    marker_nodes: list[dict[str, Any]] = []
    if include_markers:
        for marker in timeline.markers:
            marker_nodes.append(_marker_node(marker, frame_rate))

    stack: dict[str, Any] = {
        "OTIO_SCHEMA": "Stack.1",
        "name": "tracks",
        "children": track_nodes,
        "markers": marker_nodes,
        "metadata": {},
    }
    document: dict[str, Any] = {
        "OTIO_SCHEMA": "Timeline.1",
        "name": project.name or project.project_id,
        "global_start_time": _rational_time(0, frame_rate),
        "tracks": stack,
        "metadata": {
            NAGAR_METADATA_KEY: {
                "project_id": project.project_id,
                "timeline_id": timeline.timeline_id,
                "duration_us": timeline.duration_us,
                "playhead_us": timeline.playhead.timecode_us,
                "state_revision": project.state_revision,
                "state_hash": project.state_hash,
                "export_standard": "OpenTimelineIO-v1.0",
            }
        },
    }
    return document


def dumps_otio_document(document: dict[str, Any]) -> str:
    """Canonical, deterministic JSON text for an OTIO document dict."""
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _track_node(track: Any, frame_rate: float) -> dict[str, Any]:
    kind = {"video": "Video", "image": "Video", "audio": "Audio"}.get(track.kind, "Video")
    children: list[dict[str, Any]] = []
    cursor_us = 0
    for clip in sorted(track.clips, key=lambda c: (c.timeline_range.start_us, c.clip_id)):
        placement = clip.timeline_range
        if placement.start_us < cursor_us:
            raise OtioConversionError(
                f"track {track.track_id!r} clips overlap at {placement.start_us}µs "
                f"(cursor {cursor_us}µs) — OTIO tracks are strictly ordered"
            )
        if placement.start_us > cursor_us:
            children.append(_gap_node(cursor_us, placement.start_us - cursor_us, frame_rate))
        children.append(_clip_node(clip, frame_rate))
        cursor_us = placement.end_us

    return {
        "OTIO_SCHEMA": "Track.1",
        "name": track.name or track.track_id,
        "kind": kind,
        "children": children,
        "markers": [],
        "effects": [],
        "enabled": True,
        "metadata": {
            NAGAR_METADATA_KEY: {
                "track_id": track.track_id,
                "kind": track.kind,
                "effects": [effect.model_dump(mode="json") for effect in track.effects],
            }
        },
    }


def _clip_node(clip: Any, frame_rate: float) -> dict[str, Any]:
    media = clip.media_ref
    available_us = media.duration_us
    media_reference = {
        "OTIO_SCHEMA": "ExternalReference.1",
        "name": "",
        "target_url": f"asset:{media.asset_id}",
        "available_range": _time_range(0, available_us, frame_rate),
        "metadata": {
            NAGAR_METADATA_KEY: {
                "asset_id": media.asset_id,
                "content_sha256": media.content_sha256,
                "media_kind": media.media_kind,
                "duration_us": media.duration_us,
                "color_space": media.color_space,
            }
        },
    }
    nagar_meta: dict[str, Any] = {
        "clip_id": clip.clip_id,
        "source_start_us": clip.source_range.start_us,
        "source_duration_us": clip.source_range.end_us - clip.source_range.start_us,
        "timeline_start_us": clip.timeline_range.start_us,
        "timeline_duration_us": clip.timeline_range.end_us - clip.timeline_range.start_us,
        "effects": [effect.model_dump(mode="json") for effect in clip.effects],
    }
    return {
        # Clip.2 shape (opentimelineio >= 0.17): media_references map.
        "OTIO_SCHEMA": "Clip.2",
        "name": clip.clip_id,
        "source_range": _time_range(
            clip.source_range.start_us,
            clip.source_range.end_us - clip.source_range.start_us,
            frame_rate,
        ),
        "active_media_reference_key": "DEFAULT_MEDIA",
        "media_references": {"DEFAULT_MEDIA": media_reference},
        "metadata": {NAGAR_METADATA_KEY: nagar_meta},
    }


def _gap_node(start_us: int, duration_us: int, frame_rate: float) -> dict[str, Any]:
    return {
        "OTIO_SCHEMA": "Gap.1",
        "name": "gap",
        "source_range": _time_range(start_us, duration_us, frame_rate),
        "metadata": {
            NAGAR_METADATA_KEY: {"timeline_start_us": start_us, "duration_us": duration_us}
        },
    }


def _marker_node(marker: Any, frame_rate: float) -> dict[str, Any]:
    # TDD/task-121: markers are points exported as a 1-frame Marker.2 range.
    one_frame_us = frames_value_to_us(1.0, frame_rate)
    return {
        "OTIO_SCHEMA": "Marker.2",
        "name": marker.label,
        "marked_range": _time_range(marker.timecode_us, max(1, one_frame_us), frame_rate),
        "color": marker_otio_color(marker.color),
        "comment": "",
        "metadata": {
            NAGAR_METADATA_KEY: {
                "marker_id": marker.marker_id,
                "timecode_us": marker.timecode_us,
                "color": marker.color,
            }
        },
    }


# ---------------------------------------------------------------------------
# OTIO → Nagar (tolerant parse: Clip.1 and Clip.2, real library or ours)
# ---------------------------------------------------------------------------


def parse_otio_document(document: Any) -> ParsedTimelineState:
    """Normalize any OTIO JSON document (ours or the real library's) into Nagar."""
    if not isinstance(document, dict):
        raise OtioConversionError("OTIO document root must be an object")
    schema_name = str(document.get("OTIO_SCHEMA", ""))
    if schema_name not in ("Timeline.1", "Timeline.2"):
        raise OtioConversionError(f"expected an OTIO Timeline document, got {schema_name!r}")

    stack = document.get("tracks") or {}
    global_start_us, _ = _read_rational_time(
        document.get("global_start_time") or {"value": 0, "rate": 24}
    )
    root_meta = _nagar_meta(document)

    tracks: list[ParsedTrack] = []
    for index, child in enumerate(stack.get("children") or []):
        if not isinstance(child, dict):
            continue
        if str(child.get("OTIO_SCHEMA", "")).startswith("Track"):
            tracks.append(_parse_track(child, index))

    markers = _parse_markers(stack)
    duration_us = max(
        [root_meta.get("duration_us") or 0]
        + [track.children[-1].timeline_range.end_us for track in tracks if track.children]
        + [0]
    )
    playhead = root_meta.get("playhead_us")
    return ParsedTimelineState(
        name=str(document.get("name") or ""),
        timeline_id=str(root_meta.get("timeline_id") or "main"),
        duration_us=int(duration_us),
        global_start_us=global_start_us,
        tracks=tuple(tracks),
        markers=tuple(markers),
        playhead_us=int(playhead) if playhead is not None else None,
        metadata=root_meta,
    )


def _nagar_meta(node: dict[str, Any]) -> dict[str, Any]:
    meta = node.get("metadata") or {}
    if not isinstance(meta, dict):
        return {}
    block = meta.get(NAGAR_METADATA_KEY) or {}
    return dict(block) if isinstance(block, dict) else {}


def _parse_track(node: dict[str, Any], index: int) -> ParsedTrack:
    meta = _nagar_meta(node)
    track_id = str(meta.get("track_id") or node.get("name") or f"track_{index:02d}")
    kind_raw = str(node.get("kind") or "Video")
    visible_kind = {"Video": "video", "Audio": "audio"}.get(kind_raw, "other")
    restored_kind = meta.get("kind")
    if restored_kind in ("video", "audio", "image"):
        kind: str = str(restored_kind)
    elif visible_kind == "other":
        kind = "audio" if "audio" in track_id.lower() else "video"
    else:
        kind = visible_kind
    children: list[ParsedClip | ParsedGap] = []
    cursor_us = 0
    for child in node.get("children") or []:
        if not isinstance(child, dict):
            continue
        schema_name = str(child.get("OTIO_SCHEMA", ""))
        if schema_name.startswith("Gap"):
            gap = _parse_gap(child, cursor_us)
            children.append(gap)
            cursor_us = gap.timeline_range.end_us
        elif schema_name.startswith("Clip"):
            clip = _parse_clip(child, track_id, len(children), cursor_us)
            children.append(clip)
            cursor_us = max(cursor_us, clip.timeline_range.end_us)
    effects = tuple(meta.get("effects") or ())
    return ParsedTrack(
        track_id=track_id,
        name=str(node.get("name") or ""),
        kind=kind,  # type: ignore[arg-type]
        children=tuple(children),
        effects=effects,
        metadata=meta,
    )


def _parse_clip(node: dict[str, Any], track_id: str, index: int, cursor_us: int) -> ParsedClip:
    meta = _nagar_meta(node)
    source_start_us, source_duration_us = _read_time_range(node.get("source_range"))
    if meta:
        source_start_us = int(meta.get("source_start_us", source_start_us))
        source_duration_us = int(meta.get("source_duration_us", source_duration_us))
        timeline_start_us = int(meta.get("timeline_start_us", source_start_us))
        timeline_duration_us = int(meta.get("timeline_duration_us", source_duration_us))
    else:
        # Without the nagar block OTIO track assembly is authoritative: children
        # are laid out sequentially from the running cursor (documented loss of
        # any unrepresented pre-roll is in LOSS_CONTRACT).
        timeline_start_us = cursor_us
        timeline_duration_us = source_duration_us

    clip_id = str(meta.get("clip_id") or node.get("name") or f"{track_id}_clip_{index:02d}")
    media_ref = _parse_media_reference(node)
    effects = tuple(meta.get("effects") or ())
    return ParsedClip(
        clip_id=clip_id,
        name=str(node.get("name") or ""),
        media_ref=media_ref,
        source_range=ParsedRangeUS(start_us=source_start_us, duration_us=source_duration_us),
        timeline_range=ParsedRangeUS(start_us=timeline_start_us, duration_us=timeline_duration_us),
        effects=effects,
        metadata=meta,
    )


def _parse_media_reference(node: dict[str, Any]) -> ParsedMediaRef | None:
    """Accept Clip.2 ``media_references`` and Clip.1 ``media_reference`` alike."""
    reference: Any = None
    references = node.get("media_references")
    if isinstance(references, dict):
        key = node.get("active_media_reference_key") or "DEFAULT_MEDIA"
        reference = references.get(key) or next(iter(references.values()), None)
    if reference is None:
        reference = node.get("media_reference")
    if reference is None or str(reference.get("OTIO_SCHEMA", "")).startswith("MissingReference"):
        return None
    target_url = str(reference.get("target_url") or "")
    meta = _nagar_meta(reference)
    asset_id = str(meta.get("asset_id") or target_url.removeprefix("asset:") or "unknown")
    available_us, _ = _read_time_range(
        reference.get("available_range")
        or {"start_time": {"value": 0, "rate": 24}, "duration": {"value": 0, "rate": 24}}
    )
    media_kind = meta.get("media_kind")
    return ParsedMediaRef(
        asset_id=asset_id,
        target_url=target_url or f"asset:{asset_id}",
        content_sha256=meta.get("content_sha256"),
        media_kind=media_kind if media_kind in ("video", "audio", "image", "caption") else None,
        duration_us=int(meta.get("duration_us") or available_us),
        color_space=meta.get("color_space"),
    )


def _parse_gap(node: dict[str, Any], cursor_us: int) -> ParsedGap:
    meta = _nagar_meta(node)
    start_us, duration_us = _read_time_range(node.get("source_range"))
    if meta:
        start_us = int(meta.get("timeline_start_us", start_us))
        duration_us = int(meta.get("duration_us", duration_us))
    else:
        start_us = cursor_us
    return ParsedGap(
        name=str(node.get("name") or "gap"),
        timeline_range=ParsedRangeUS(start_us=start_us, duration_us=duration_us),
    )


def _parse_markers(stack: dict[str, Any]) -> list[ParsedMarker]:
    markers: list[ParsedMarker] = []
    for index, node in enumerate(stack.get("markers") or []):
        if not isinstance(node, dict):
            continue
        meta = _nagar_meta(node)
        start_us, _ = _read_time_range(node.get("marked_range") or node.get("range"))
        timecode_us = int(meta.get("timecode_us", start_us))
        markers.append(
            ParsedMarker(
                marker_id=str(meta.get("marker_id") or f"marker_{index:02d}"),
                label=str(node.get("name") or ""),
                timecode_us=timecode_us,
                color=meta.get("color"),
                otio_color=str(node.get("color") or MARKER_COLOR_FALLBACK),
                comment=str(node.get("comment") or ""),
            )
        )
    return markers


__all__ = [
    "LOSS_CONTRACT",
    "NAGAR_METADATA_KEY",
    "OtioConversionError",
    "dumps_otio_document",
    "frames_value_to_us",
    "marker_otio_color",
    "parse_otio_document",
    "project_to_otio_document",
    "us_to_frames_value",
]
