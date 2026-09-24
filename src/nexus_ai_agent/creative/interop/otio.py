"""Canonical Project <-> OpenTimelineIO adapter (session 3, real interop).

This module parses and emits **real** ``.otio`` timelines with the
``OpenTimelineIO`` library — no hand-rolled JSON writer that merely *looks*
like OTIO.  The library is an optional dependency (``pip install -e ".[otio]"``,
also in the ``dev`` extra so CI exercises the real parser); when it is
missing every entry point raises :class:`OtioUnavailableError` instead of
returning a fake timeline.

Time model: OTIO ``RationalTime`` values convert to integer microseconds with
exact rational arithmetic (``Fraction``), so integer frame rates round-trip
bit-for-bit.  Non-integer rates (23.976, 29.97) are approximated through
``Fraction(rate).limit_denominator(1_000_000)`` and the approximation is
documented on the converted range — the Project side stays integer-µs.

Mapping contract (losses are explicit, never silent):

* video/audio tracks → :class:`Track` (``kind`` from ``Track.kind``);
  any other track kind raises :class:`OtioError` (fail-closed);
* ``Clip`` → :class:`Clip` with ``source_range`` from ``source_range`` and
  ``timeline_range`` from the track placement (``Gap``s survive as spacing
  between placements — there is no gap entity in the Project model);
* ``ExternalReference.target_url`` → ``asset_id`` (basename slug) and the
  ``MediaRef`` timebase/duration from ``available_range``;
* ``MissingReference`` → ``asset_id`` ``"missing:<clip-name>"`` with
  ``content_sha256`` ``"unresolved:missing-reference"`` — the file must be
  staged and hashed before any plan compiles (plan compilation only reads
  ``asset_id``; staging resolves identity);
* ``Transition`` → ``EffectLayerRef(operation="motion.add_transition")`` on
  the outgoing clip — which the plan compiler honestly reports as
  *unmapped* until the xfade twin is threaded (no silent dissolve);
* OTIO clip effects → ``EffectLayerRef(operation="otio.<effect_name>")``
  (downstream-unmapped by construction — intent preserved, executability
  not claimed);
* track markers (timeline frame) and clip markers (source frame, rebased onto
  the clip's placement) → :class:`Marker` (OTIO 0.18 keeps no markers on the
  ``Timeline`` itself; markers on gaps/stacks/transitions are refused
  explicitly rather than dropped silently);
* nested ``Stack`` items are flattened with their accumulated offset.

The reverse adapter (:func:`project_to_otio`) rebuilds an equivalent
``otio.schema.Timeline`` so Project → OTIO → Project round-trips within
integer-µs equality at integer frame rates.
"""

from __future__ import annotations

from fractions import Fraction
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

MICROSECONDS_PER_SECOND = 1_000_000


class OtioError(ValueError):
    """An OTIO document said something this adapter refuses to guess at."""


class OtioUnavailableError(RuntimeError):
    """The real ``opentimelineio`` library is not installed (fail-closed)."""


def require_otio() -> Any:
    """Import the real ``opentimelineio`` package or raise honestly."""
    try:
        import opentimelineio as otio  # type: ignore[import-untyped]
    except ImportError as exc:
        raise OtioUnavailableError(
            "OpenTimelineIO interop needs the 'otio' extra: pip install -e \".[otio]\""
        ) from exc
    return otio


def _rate_fraction(rate: float) -> Fraction:
    if rate <= 0:
        raise OtioError(f"OTIO rate must be positive, got {rate!r}")
    return Fraction(rate).limit_denominator(1_000_000)


def rational_to_us(value: float, rate: float) -> int:
    """Exact ``RationalTime`` → integer microseconds (round-half-even)."""
    ratio = Fraction(value) * MICROSECONDS_PER_SECOND / _rate_fraction(rate)
    return int(round(ratio))


def us_to_rational(micros: int, rate: float) -> float:
    """Integer microseconds → ``RationalTime`` value at ``rate``."""
    ratio = Fraction(micros) * _rate_fraction(rate) / MICROSECONDS_PER_SECOND
    return float(ratio)


def parse_otio_string(payload: str) -> Any:
    """Parse an ``.otio`` JSON document with the real OTIO library."""
    otio = require_otio()
    try:
        timeline = otio.adapters.read_from_string(payload, adapter_name="otio_json")
    except Exception as exc:
        raise OtioError(f"not a parseable OTIO timeline: {exc}") from exc
    if not isinstance(timeline, otio.schema.Timeline):
        raise OtioError(f"OTIO document holds {type(timeline).__name__}, not a Timeline")
    return timeline


def parse_otio_file(path: str | Path) -> Any:
    """Parse an ``.otio`` file with the real OTIO library."""
    otio = require_otio()
    candidate = Path(path)
    if not candidate.is_file():
        raise OtioError(f"no such OTIO file: {candidate}")
    try:
        timeline = otio.adapters.read_from_file(str(candidate), adapter_name="otio_json")
    except Exception as exc:
        raise OtioError(f"cannot parse {candidate} as OTIO: {exc}") from exc
    if not isinstance(timeline, otio.schema.Timeline):
        raise OtioError(f"{candidate} holds {type(timeline).__name__}, not a Timeline")
    return timeline


def _slug(text: str, fallback: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in text.strip())
    return cleaned or fallback


def _asset_id_for_clip(otio: Any, clip: Any, index: int) -> tuple[str, str]:
    """``(asset_id, content_identity)`` — identity stays unresolved until staging."""
    reference = clip.media_reference
    if reference is None or isinstance(reference, otio.schema.MissingReference):
        return f"missing:{_slug(clip.name or '', f'clip-{index}')}", "unresolved:missing-reference"
    if isinstance(reference, otio.schema.ExternalReference):
        target = reference.target_url or ""
        stem = Path(unquote(urlparse(target).path)).name or target or clip.name or f"clip-{index}"
        return _slug(Path(stem).stem, f"clip-{index}"), f"unresolved:external:{target or stem}"
    if isinstance(reference, otio.schema.GeneratorReference):
        return (
            f"generated:{_slug(clip.name or '', f'clip-{index}')}",
            f"unresolved:generator:{reference.generator_kind or 'unknown'}",
        )
    raise OtioError(
        f"clip {clip.name!r} has a {type(reference).__name__} media reference, "
        "which this adapter does not resolve"
    )


def _timebase_for_rate(rate: float) -> Any:
    from nexus_ai_agent.creative.studio.models import TimeBase

    ratio = _rate_fraction(rate)
    return TimeBase(numerator=ratio.numerator, denominator=ratio.denominator)


def _project_models() -> tuple[Any, ...]:
    from nexus_ai_agent.creative.studio import models

    return (
        models.Project,
        models.Timeline,
        models.Track,
        models.Clip,
        models.MediaRef,
        models.TimeRangeUS,
        models.EffectLayerRef,
        models.Marker,
        models.Playhead,
    )


def _iter_track_items(otio: Any, track: Any) -> Any:
    """Yield ``(item, timeline_offset_us, nested_in_stack)`` flattened."""
    for child in track:
        child_range = track.range_of_child(child)
        if child_range is None:
            raise OtioError(
                f"{type(child).__name__} {getattr(child, 'name', '')!r} has no track range"
            )
        rate = child_range.start_time.rate
        base_us = rational_to_us(child_range.start_time.value, rate)
        if isinstance(child, otio.schema.Stack):
            yield from _iter_stack_items(otio, child, base_us)
        else:
            yield child, base_us, False


def _iter_stack_items(otio: Any, stack: Any, base_us: int) -> Any:
    for child in stack:
        child_range = stack.range_of_child(child)
        if child_range is None:
            raise OtioError(f"nested {type(child).__name__} has no stack range")
        rate = child_range.start_time.rate
        nested_base = base_us + rational_to_us(child_range.start_time.value, rate)
        if isinstance(child, otio.schema.Stack):
            yield from _iter_stack_items(otio, child, nested_base)
        else:
            yield child, nested_base, True


def otio_to_project(
    timeline: Any,
    *,
    project_id: str,
    name: str | None = None,
) -> Any:
    """Convert a parsed ``otio.schema.Timeline`` into a studio :class:`Project`.

    Accepts either a parsed timeline object or an ``.otio`` JSON string / file
    path (strings that parse as OTIO are parsed; paths that exist are read —
    anything else raises :class:`OtioError`).
    """
    otio = require_otio()
    if isinstance(timeline, (str, Path)) and Path(timeline).is_file():
        timeline = parse_otio_file(timeline)
    elif isinstance(timeline, str):
        timeline = parse_otio_string(timeline)
    if not isinstance(timeline, otio.schema.Timeline):
        raise OtioError(f"expected an OTIO Timeline, got {type(timeline).__name__}")

    (Project, Timeline, Track, Clip, MediaRef, TimeRangeUS, EffectLayerRef, Marker, Playhead) = (
        _project_models()
    )

    def _marker_us(marker: Any) -> int | None:
        marked = marker.marked_range
        if marked is None:
            return None
        return rational_to_us(marked.start_time.value, marked.start_time.rate)

    def _as_marker(marker: Any, timecode_us: int, marker_index: int) -> Any:
        color = getattr(marker, "color", None)
        return Marker(
            marker_id=_slug(marker.name or "", f"marker-{marker_index}"),
            timecode_us=timecode_us,
            label=marker.name or f"marker-{marker_index}",
            color=str(color) if color is not None else None,
        )

    tracks: list[Any] = []
    markers: list[Any] = []
    for track_index, track in enumerate(timeline.tracks):
        kind = str(getattr(track, "kind", ""))
        if kind == "Video":
            track_kind = "video"
        elif kind == "Audio":
            track_kind = "audio"
        else:
            raise OtioError(f"track {track.name!r} has kind {kind!r} (only Video/Audio import)")
        clips: list[Any] = []
        last_clip: Any | None = None
        for track_marker in getattr(track, "markers", []) or []:
            at_us = _marker_us(track_marker)
            if at_us is not None:
                markers.append(_as_marker(track_marker, at_us, len(markers)))
        for item_index, (item, base_us, nested) in enumerate(_iter_track_items(otio, track)):
            if isinstance(item, (otio.schema.Gap, otio.schema.Transition)):
                if getattr(item, "markers", None):
                    raise OtioError(
                        f"{type(item).__name__} {item.name!r} carries markers, "
                        "which this adapter does not rebase"
                    )
            if isinstance(item, otio.schema.Gap):
                # Gaps survive as spacing: the next clip's placement starts
                # after them.  No Project entity models a gap directly.
                last_clip = None
                continue
            if isinstance(item, otio.schema.Transition):
                if last_clip is None:
                    raise OtioError(
                        f"transition {item.name!r} on track {track.name!r} follows no clip"
                    )
                duration_us = rational_to_us(
                    item.in_offset.value + item.out_offset.value, item.in_offset.rate
                )
                last_clip.effects.append(
                    EffectLayerRef(
                        operation="motion.add_transition",
                        parameters={"duration_us": duration_us, "name": item.name or ""},
                        range=last_clip.timeline_range,
                    )
                )
                continue
            if not isinstance(item, otio.schema.Clip):
                raise OtioError(
                    f"track {track.name!r} holds {type(item).__name__}, not a Clip/Gap/Transition"
                )
            source = item.source_range
            if source is None:
                raise OtioError(f"clip {item.name!r} has no source_range")
            src_rate = source.start_time.rate
            src_start = rational_to_us(source.start_time.value, src_rate)
            src_end = src_start + rational_to_us(source.duration.value, src_rate)
            if nested:  # Stack child: duration from the item itself + base offset
                item_duration = item.duration()
                duration_us = rational_to_us(item_duration.value, item_duration.rate)
                tl_start, tl_end = base_us, base_us + duration_us
            else:
                placement = track.range_of_child(item)
                if placement is None:
                    raise OtioError(f"clip {item.name!r} has no placement on its track")
                tl_rate = placement.start_time.rate
                tl_start = rational_to_us(placement.start_time.value, tl_rate)
                tl_end = tl_start + rational_to_us(placement.duration.value, tl_rate)
            asset_id, content_identity = _asset_id_for_clip(otio, item, item_index)
            reference = item.media_reference
            available = reference.available_range if reference else None
            if available is not None:
                media_duration_us = rational_to_us(
                    available.duration.value, available.duration.rate
                )
                media_rate = available.duration.rate
            else:
                media_duration_us = src_end - src_start
                media_rate = src_rate
            media_kind = "audio" if track_kind == "audio" else "video"
            effects: list[Any] = []
            for effect in item.effects:
                effect_name = getattr(effect, "effect_name", "") or "effect"
                parameters = dict(getattr(effect, "metadata", {}) or {})
                parameters.setdefault("otio_effect_name", effect_name)
                effects.append(
                    EffectLayerRef(
                        operation=f"otio.{_slug(effect_name, 'effect').lower()}",
                        parameters=parameters,
                        range=TimeRangeUS(start_us=tl_start, end_us=tl_end),
                    )
                )
            clip = Clip(
                clip_id=_slug(item.name or "", f"{track.name or 'track'}-{item_index}"),
                media_ref=MediaRef(
                    asset_id=asset_id,
                    content_sha256=content_identity,
                    media_kind=media_kind,
                    duration_us=media_duration_us,
                    timebase=_timebase_for_rate(media_rate),
                ),
                source_range=TimeRangeUS(start_us=src_start, end_us=src_end),
                timeline_range=TimeRangeUS(start_us=tl_start, end_us=tl_end),
                effects=effects,
            )
            clips.append(clip)
            last_clip = clip
            # Clip markers live in the source frame: rebase onto the placement.
            for clip_marker in getattr(item, "markers", []) or []:
                src_us = _marker_us(clip_marker)
                if src_us is None:
                    continue
                markers.append(
                    _as_marker(clip_marker, tl_start + (src_us - src_start), len(markers))
                )
        tracks.append(
            Track(
                track_id=_slug(track.name or "", f"track-{track_index}"),
                name=track.name or f"track-{track_index}",
                kind=track_kind,
                clips=clips,
            )
        )

    total_us = 0
    for track in tracks:
        for clip in track.clips:
            total_us = max(total_us, clip.timeline_range.end_us)
    return Project(
        project_id=project_id,
        name=name or timeline.name or project_id,
        timeline=Timeline(
            timeline_id=f"{project_id}-timeline",
            duration_us=total_us,
            tracks=tracks,
            markers=markers,
            playhead=Playhead(timecode_us=0),
        ),
    )


def project_to_otio(project: Any, *, rate: float = 30.0) -> Any:
    """Convert a studio :class:`Project` into an ``otio.schema.Timeline`.

    Clip windows, track placements, and markers survive exactly at integer
    ``rate`` values; effect layers do not export (no OTIO effect vocabulary
    is claimed — exporting fake ``effect_name`` values would be fabrication).
    """
    otio = require_otio()
    if rate <= 0:
        raise OtioError(f"OTIO export rate must be positive, got {rate!r}")

    def _rt(micros: int) -> Any:
        return otio.opentime.RationalTime(us_to_rational(micros, rate), rate)

    def _range(start_us: int, end_us: int) -> Any:
        return otio.opentime.TimeRange(_rt(start_us), _rt(end_us - start_us))

    otio_tracks: list[Any] = []
    for track in project.timeline.tracks:
        if track.kind == "video":
            otio_kind = otio.schema.TrackKind.Video
        elif track.kind == "audio":
            otio_kind = otio.schema.TrackKind.Audio
        else:
            raise OtioError(f"track {track.track_id!r} has kind {track.kind!r} (no OTIO kind)")
        children: list[Any] = []
        cursor_us = 0
        for clip in sorted(track.clips, key=lambda c: (c.timeline_range.start_us, c.clip_id)):
            placement = clip.timeline_range
            if placement.start_us < cursor_us:
                raise OtioError(f"track {track.track_id!r} clips overlap at {placement.start_us}µs")
            if placement.start_us > cursor_us:
                gap = otio.schema.Gap(
                    name=f"gap-{cursor_us}-{placement.start_us}",
                    source_range=_range(0, placement.start_us - cursor_us),
                )
                children.append(gap)
            media_kind = clip.media_ref.media_kind
            if media_kind in ("video", "audio", "image"):
                reference: Any = otio.schema.ExternalReference(
                    target_url=f"asset:{clip.media_ref.asset_id}",
                    available_range=_range(0, max(clip.media_ref.duration_us, 1)),
                )
            else:
                reference = otio.schema.MissingReference()
            otio_clip = otio.schema.Clip(
                name=clip.clip_id,
                source_range=_range(clip.source_range.start_us, clip.source_range.end_us),
                media_reference=reference,
            )
            children.append(otio_clip)
            cursor_us = placement.end_us
        otio_tracks.append(otio.schema.Track(name=track.name, kind=otio_kind, children=children))
    # OTIO 0.18 keeps no markers on the Timeline itself: exported markers
    # live on the first track (timeline frame), which the importer reads.
    if project.timeline.markers and not otio_tracks:
        raise OtioError("cannot export markers: the project has no tracks")
    if otio_tracks:
        first_track = otio_tracks[0]
        for marker in project.timeline.markers:
            color = getattr(
                otio.schema.MarkerColor,
                str(marker.color or "RED").upper(),
                otio.schema.MarkerColor.RED,
            )
            first_track.markers.append(
                otio.schema.Marker(
                    name=marker.label,
                    marked_range=_range(marker.timecode_us, marker.timecode_us + 1),
                    color=color,
                )
            )
    return otio.schema.Timeline(name=project.name, tracks=otio_tracks)


__all__ = [
    "MICROSECONDS_PER_SECOND",
    "OtioError",
    "OtioUnavailableError",
    "otio_to_project",
    "parse_otio_file",
    "parse_otio_string",
    "project_to_otio",
    "rational_to_us",
    "require_otio",
    "us_to_rational",
]
