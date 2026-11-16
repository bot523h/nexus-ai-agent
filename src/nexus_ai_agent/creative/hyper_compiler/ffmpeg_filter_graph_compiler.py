"""Single-pass, strictly validated ``-filter_complex`` compiler.

The compiler turns a typed chain of video/audio ops into one topologically valid
``-filter_complex`` string plus an FFmpeg argv list (never ``shell=True``).
Validation is hard and fail-closed:

* every numeric operand must be finite (``math.isfinite``) and inside its
  filter's declared range;
* pad labels and file paths are matched against an allow-list, so a shell
  metacharacter, a quote, a newline or a ``..`` traversal is refused with
  :class:`InvalidFilterGraphError`;
* every declared input pad must be consumed, every op operand must be produced
  by an earlier op (or be an input pad), and the set of pads left at the end
  must be exactly the requested outputs — a dangling pad is an error, not a
  silently dropped stream.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from typing import ClassVar

from nexus_ai_agent.creative.hyper_compiler.errors import InvalidFilterGraphError

_LABEL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,62}$")
_PATH_RE = re.compile(r"^[A-Za-z0-9._/\-]+$")

#: Shell metacharacters and control bytes that must never reach an argv or a
#: filter argument (checked explicitly so the refusal is legible, even though
#: the process is spawned without a shell).
_FORBIDDEN_PATH_CHARS = frozenset(";|&$`\n\r\t\"'\\<>(){}[]!*?#~ ")


class StreamKind(str, Enum):
    VIDEO = "video"
    AUDIO = "audio"


def _num(value: float) -> str:
    """Canonical, deterministic decimal rendering of a finite float."""
    if not math.isfinite(value):
        raise InvalidFilterGraphError(f"non-finite numeric operand: {value!r}")
    text = format(value, ".6f").rstrip("0").rstrip(".")
    return text if text not in ("", "-0") else "0"


def _check_label(label: str, *, what: str) -> str:
    if not _LABEL_RE.match(label):
        raise InvalidFilterGraphError(f"unsafe {what} label: {label!r}")
    return label


def _check_path(path: str, *, what: str) -> str:
    if not path:
        raise InvalidFilterGraphError(f"empty {what} path")
    if any(ch in _FORBIDDEN_PATH_CHARS for ch in path):
        raise InvalidFilterGraphError(f"forbidden character in {what} path: {path!r}")
    if not _PATH_RE.match(path):
        raise InvalidFilterGraphError(f"unsafe {what} path: {path!r}")
    if any(part == ".." for part in path.split("/")):
        raise InvalidFilterGraphError(f"path traversal in {what} path: {path!r}")
    return path


def _check_range(value: float, low: float, high: float, *, what: str) -> float:
    if not math.isfinite(value):
        raise InvalidFilterGraphError(f"{what} must be finite, got {value!r}")
    if value < low or value > high:
        raise InvalidFilterGraphError(f"{what}={value} outside [{low}, {high}]")
    return value


def _check_int(value: int, low: int, high: int, *, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidFilterGraphError(f"{what} must be an int, got {type(value).__name__}")
    if value < low or value > high:
        raise InvalidFilterGraphError(f"{what}={value} outside [{low}, {high}]")
    return value


def _as_float(value: float | None) -> float:
    """Narrow an optional measurement the caller already proved present."""
    assert value is not None  # guarded by the caller's all-or-none check
    return value


# ── Typed ops ───────────────────────────────────────────────────────────────
class FilterOp:
    """Base class for a typed filter operation (video or audio)."""

    kind: ClassVar[StreamKind] = StreamKind.VIDEO

    def input_pads(self) -> tuple[str, ...]:
        raise NotImplementedError

    def output_pads(self) -> tuple[str, ...]:
        raise NotImplementedError

    def render(self) -> str:
        raise NotImplementedError


@dataclass(frozen=True)
class TrimOp(FilterOp):
    """Cut ``[start_frame, end_frame)`` and rebase the PTS to zero."""

    kind: ClassVar[StreamKind] = StreamKind.VIDEO

    in_pad: str
    out_pad: str
    start_frame: int
    end_frame: int
    fps: float

    def __post_init__(self) -> None:
        _check_label(self.in_pad, what="input pad")
        _check_label(self.out_pad, what="output pad")
        _check_int(self.start_frame, 0, 2**53, what="start_frame")
        _check_int(self.end_frame, 1, 2**53, what="end_frame")
        if self.end_frame <= self.start_frame:
            raise InvalidFilterGraphError("end_frame must be > start_frame")
        _check_range(self.fps, 1e-6, 1e6, what="fps")

    def input_pads(self) -> tuple[str, ...]:
        return (self.in_pad,)

    def output_pads(self) -> tuple[str, ...]:
        return (self.out_pad,)

    def render(self) -> str:
        return (
            f"trim=start_frame={self.start_frame}:end_frame={self.end_frame},"
            f"setpts=PTS-STARTPTS,fps={_num(self.fps)}"
        )


@dataclass(frozen=True)
class TrimStartOp(FilterOp):
    """Drop every frame before ``start_frame`` (open-ended trim).

    The CPU engine selects frames by index; this is its exact FFmpeg
    counterpart for the ``trim_end is None`` case, so an open-ended trim is
    never silently dropped on the FFmpeg path.  No ``fps`` filter is emitted:
    the source is already at the target rate, and forcing a rate here could
    duplicate or drop frames the CPU path would have kept.
    """

    kind: ClassVar[StreamKind] = StreamKind.VIDEO

    in_pad: str
    out_pad: str
    start_frame: int

    def __post_init__(self) -> None:
        _check_label(self.in_pad, what="input pad")
        _check_label(self.out_pad, what="output pad")
        _check_int(self.start_frame, 1, 2**53, what="start_frame")

    def input_pads(self) -> tuple[str, ...]:
        return (self.in_pad,)

    def output_pads(self) -> tuple[str, ...]:
        return (self.out_pad,)

    def render(self) -> str:
        return f"trim=start_frame={self.start_frame},setpts=PTS-STARTPTS"


@dataclass(frozen=True)
class ScalePadOp(FilterOp):
    """Scale preserving aspect ratio, then pad to an exact canvas."""

    kind: ClassVar[StreamKind] = StreamKind.VIDEO

    in_pad: str
    out_pad: str
    width: int
    height: int

    def __post_init__(self) -> None:
        _check_label(self.in_pad, what="input pad")
        _check_label(self.out_pad, what="output pad")
        _check_int(self.width, 1, 65536, what="width")
        _check_int(self.height, 1, 65536, what="height")

    def input_pads(self) -> tuple[str, ...]:
        return (self.in_pad,)

    def output_pads(self) -> tuple[str, ...]:
        return (self.out_pad,)

    def render(self) -> str:
        return (
            f"scale={self.width}:{self.height}:force_original_aspect_ratio=decrease,"
            f"pad={self.width}:{self.height}:(ow-iw)/2:(oh-ih)/2"
        )


@dataclass(frozen=True)
class ColorGradeEqOp(FilterOp):
    """Brightness / contrast / saturation / gamma via ``eq``."""

    kind: ClassVar[StreamKind] = StreamKind.VIDEO

    in_pad: str
    out_pad: str
    brightness: float
    contrast: float
    saturation: float
    gamma: float

    def __post_init__(self) -> None:
        _check_label(self.in_pad, what="input pad")
        _check_label(self.out_pad, what="output pad")
        _check_range(self.brightness, -1.0, 1.0, what="brightness")
        _check_range(self.contrast, 0.0, 4.0, what="contrast")
        _check_range(self.saturation, 0.0, 3.0, what="saturation")
        _check_range(self.gamma, 0.1, 10.0, what="gamma")

    def input_pads(self) -> tuple[str, ...]:
        return (self.in_pad,)

    def output_pads(self) -> tuple[str, ...]:
        return (self.out_pad,)

    def render(self) -> str:
        return (
            f"eq=brightness={_num(self.brightness)}:contrast={_num(self.contrast)}:"
            f"saturation={_num(self.saturation)}:gamma={_num(self.gamma)}"
        )


@dataclass(frozen=True)
class LUT3DOp(FilterOp):
    """Apply a 3D LUT cube file."""

    kind: ClassVar[StreamKind] = StreamKind.VIDEO

    _INTERPS: ClassVar[tuple[str, ...]] = (
        "nearest",
        "trilinear",
        "tetrahedral",
        "pyramid",
        "prism",
    )

    in_pad: str
    out_pad: str
    lut_path: str
    interp: str = "tetrahedral"

    def __post_init__(self) -> None:
        _check_label(self.in_pad, what="input pad")
        _check_label(self.out_pad, what="output pad")
        _check_path(self.lut_path, what="lut3d")
        if self.interp not in self._INTERPS:
            raise InvalidFilterGraphError(f"unsupported lut3d interp: {self.interp!r}")

    def input_pads(self) -> tuple[str, ...]:
        return (self.in_pad,)

    def output_pads(self) -> tuple[str, ...]:
        return (self.out_pad,)

    def render(self) -> str:
        return f"lut3d=file='{self.lut_path}':interp={self.interp}"


@dataclass(frozen=True)
class VignetteOp(FilterOp):
    """Radial darkening; ``angle`` is in radians, ``0`` disables it."""

    kind: ClassVar[StreamKind] = StreamKind.VIDEO

    in_pad: str
    out_pad: str
    angle: float

    def __post_init__(self) -> None:
        _check_label(self.in_pad, what="input pad")
        _check_label(self.out_pad, what="output pad")
        _check_range(self.angle, 0.0, math.pi / 2.0, what="vignette angle")

    def input_pads(self) -> tuple[str, ...]:
        return (self.in_pad,)

    def output_pads(self) -> tuple[str, ...]:
        return (self.out_pad,)

    def render(self) -> str:
        return f"vignette=angle={_num(self.angle)}"


@dataclass(frozen=True)
class FilmGrainOp(FilterOp):
    """Temporal + uniform film grain via ``noise``."""

    kind: ClassVar[StreamKind] = StreamKind.VIDEO

    in_pad: str
    out_pad: str
    strength: float

    def __post_init__(self) -> None:
        _check_label(self.in_pad, what="input pad")
        _check_label(self.out_pad, what="output pad")
        _check_range(self.strength, 0.0, 100.0, what="grain strength")

    def input_pads(self) -> tuple[str, ...]:
        return (self.in_pad,)

    def output_pads(self) -> tuple[str, ...]:
        return (self.out_pad,)

    def render(self) -> str:
        return f"noise=alls={_num(self.strength)}:allf=t+u"


@dataclass(frozen=True)
class CrossfadeVideoOp(FilterOp):
    """Two-input video crossfade via ``xfade``."""

    kind: ClassVar[StreamKind] = StreamKind.VIDEO

    _TRANSITIONS: ClassVar[tuple[str, ...]] = (
        "fade",
        "dissolve",
        "wipeleft",
        "wiperight",
        "wipeup",
        "wipedown",
        "slideleft",
        "slideright",
        "circleopen",
        "circleclose",
    )

    in_pad: str
    other_pad: str
    out_pad: str
    duration: float
    offset: float
    transition: str = "dissolve"

    def __post_init__(self) -> None:
        _check_label(self.in_pad, what="input pad")
        _check_label(self.other_pad, what="input pad")
        _check_label(self.out_pad, what="output pad")
        _check_range(self.duration, 1e-6, 600.0, what="xfade duration")
        _check_range(self.offset, 0.0, 86400.0, what="xfade offset")
        if self.transition not in self._TRANSITIONS:
            raise InvalidFilterGraphError(f"unsupported xfade transition: {self.transition!r}")

    def input_pads(self) -> tuple[str, ...]:
        return (self.in_pad, self.other_pad)

    def output_pads(self) -> tuple[str, ...]:
        return (self.out_pad,)

    def render(self) -> str:
        return (
            f"xfade=transition={self.transition}:duration={_num(self.duration)}:"
            f"offset={_num(self.offset)}"
        )


@dataclass(frozen=True)
class CrossfadeAudioOp(FilterOp):
    """Two-input audio crossfade via ``acrossfade``."""

    kind: ClassVar[StreamKind] = StreamKind.AUDIO

    _CURVES: ClassVar[tuple[str, ...]] = ("tri", "qsin", "hsin", "log", "exp", "nofade")

    in_pad: str
    other_pad: str
    out_pad: str
    duration: float
    curve1: str = "tri"
    curve2: str = "tri"

    def __post_init__(self) -> None:
        _check_label(self.in_pad, what="input pad")
        _check_label(self.other_pad, what="input pad")
        _check_label(self.out_pad, what="output pad")
        _check_range(self.duration, 1e-6, 600.0, what="acrossfade duration")
        if self.curve1 not in self._CURVES or self.curve2 not in self._CURVES:
            raise InvalidFilterGraphError("unsupported acrossfade curve")

    def input_pads(self) -> tuple[str, ...]:
        return (self.in_pad, self.other_pad)

    def output_pads(self) -> tuple[str, ...]:
        return (self.out_pad,)

    def render(self) -> str:
        return f"acrossfade=d={_num(self.duration)}:c1={self.curve1}:c2={self.curve2}"


@dataclass(frozen=True)
class LoudnessNormOp(FilterOp):
    """EBU R128 loudness normalisation via ``loudnorm``.

    The base form (``linear`` unset) is a single-pass dynamic normalisation.
    Supplying the four ``measured_*`` values of a first measurement pass enables
    the two-pass *linear* mode (``linear=true``), which applies a constant gain
    instead of dynamic gain.  All four measurements must be supplied together —
    a partial set is refused, because FFmpeg ignores them without ``linear``.
    """

    kind: ClassVar[StreamKind] = StreamKind.AUDIO

    in_pad: str
    out_pad: str
    integrated: float = -16.0
    true_peak: float = -1.5
    lra: float = 11.0
    measured_i: float | None = None
    measured_tp: float | None = None
    measured_lra: float | None = None
    measured_thresh: float | None = None
    linear: bool = False

    def __post_init__(self) -> None:
        _check_label(self.in_pad, what="input pad")
        _check_label(self.out_pad, what="output pad")
        _check_range(self.integrated, -70.0, -5.0, what="loudnorm I")
        _check_range(self.true_peak, -9.0, 0.0, what="loudnorm TP")
        _check_range(self.lra, 1.0, 50.0, what="loudnorm LRA")
        measured = (self.measured_i, self.measured_tp, self.measured_lra, self.measured_thresh)
        supplied = [value for value in measured if value is not None]
        if supplied and len(supplied) != 4:
            raise InvalidFilterGraphError(
                "loudnorm linear mode needs all of measured_i/measured_tp/"
                "measured_lra/measured_thresh"
            )
        if self.linear and not supplied:
            raise InvalidFilterGraphError("loudnorm linear=true requires the measured_* values")
        if supplied:
            _check_range(_as_float(self.measured_i), -70.0, 0.0, what="loudnorm measured_i")
            _check_range(_as_float(self.measured_tp), -9.0, 0.0, what="loudnorm measured_tp")
            _check_range(_as_float(self.measured_lra), 0.0, 99.0, what="loudnorm measured_lra")
            _check_range(
                _as_float(self.measured_thresh), -70.0, 0.0, what="loudnorm measured_thresh"
            )

    def input_pads(self) -> tuple[str, ...]:
        return (self.in_pad,)

    def output_pads(self) -> tuple[str, ...]:
        return (self.out_pad,)

    def render(self) -> str:
        parts = [
            f"I={_num(self.integrated)}",
            f"TP={_num(self.true_peak)}",
            f"LRA={_num(self.lra)}",
        ]
        if self.measured_i is not None:
            parts.append(f"measured_I={_num(self.measured_i)}")
            parts.append(f"measured_TP={_num(_as_float(self.measured_tp))}")
            parts.append(f"measured_LRA={_num(_as_float(self.measured_lra))}")
            parts.append(f"measured_thresh={_num(_as_float(self.measured_thresh))}")
            parts.append(f"linear={'true' if self.linear else 'false'}")
        return "loudnorm=" + ":".join(parts)


# ── Graph description ───────────────────────────────────────────────────────
@dataclass(frozen=True)
class GraphInput:
    """A source pad the graph may consume (an FFmpeg ``-i`` or a stdin pipe)."""

    label: str
    kind: StreamKind
    path: str | None = None

    def __post_init__(self) -> None:
        _check_label(self.label, what="input pad")
        if self.path is not None:
            _check_path(self.path, what="input")


@dataclass(frozen=True)
class CompiledFilterGraph:
    """The compiler's output: one filter graph plus an argv assembler."""

    filter_complex: str
    output_pads: tuple[str, ...]
    input_pads: tuple[str, ...]

    def ffmpeg_argv(
        self,
        input_args: Sequence[str],
        output_args: Sequence[str],
        *,
        ffmpeg_bin: str = "ffmpeg",
    ) -> list[str]:
        """Assemble the full FFmpeg argv (list, never a shell string)."""
        argv = [ffmpeg_bin, "-hide_banner", "-nostdin", "-y"]
        argv.extend(input_args)
        argv.extend(["-filter_complex", self.filter_complex])
        for pad in self.output_pads:
            argv.extend(["-map", f"[{pad}]"])
        argv.extend(output_args)
        return argv


class FFmpegFilterGraphCompiler:
    """Compile typed ops into one valid, injection-free ``-filter_complex``."""

    def compile(
        self,
        inputs: Sequence[GraphInput],
        ops: Sequence[FilterOp],
        outputs: Sequence[str],
    ) -> CompiledFilterGraph:
        for out_pad in outputs:
            _check_label(out_pad, what="output pad")
        if len(set(outputs)) != len(outputs):
            raise InvalidFilterGraphError("duplicate output pad")

        available: dict[str, StreamKind] = {}
        for graph_input in inputs:
            if graph_input.label in available:
                raise InvalidFilterGraphError(f"duplicate input pad: {graph_input.label!r}")
            available[graph_input.label] = graph_input.kind

        chains: list[str] = []
        for op in ops:
            in_pads = op.input_pads()
            if len(set(in_pads)) != len(in_pads):
                raise InvalidFilterGraphError(f"{type(op).__name__} reuses an input pad")
            for pad in in_pads:
                if pad not in available:
                    raise InvalidFilterGraphError(
                        f"op consumes unknown or already-consumed pad [{pad}]"
                    )
            in_kind = available[in_pads[0]]
            if any(available[pad] is not in_kind for pad in in_pads):
                raise InvalidFilterGraphError("cannot mix video and audio pads in one op")
            if in_kind is not op.kind:
                raise InvalidFilterGraphError(
                    f"{type(op).__name__} expects {op.kind.value} but got {in_kind.value}"
                )
            for pad in in_pads:
                del available[pad]
            for pad in op.output_pads():
                if pad in available:
                    raise InvalidFilterGraphError(f"op produces duplicate pad [{pad}]")
                available[pad] = op.kind
            head = "".join(f"[{pad}]" for pad in in_pads)
            tail = "".join(f"[{pad}]" for pad in op.output_pads())
            chains.append(f"{head}{op.render()}{tail}")

        leftover = set(available) - set(outputs)
        if leftover:
            raise InvalidFilterGraphError(f"dangling pad(s): {sorted(leftover)}")
        missing = set(outputs) - set(available)
        if missing:
            raise InvalidFilterGraphError(f"unbound output pad(s): {sorted(missing)}")

        return CompiledFilterGraph(
            filter_complex=";".join(chains),
            output_pads=tuple(outputs),
            input_pads=tuple(graph_input.label for graph_input in inputs),
        )


__all__ = [
    "ColorGradeEqOp",
    "CompiledFilterGraph",
    "CrossfadeAudioOp",
    "CrossfadeVideoOp",
    "FFmpegFilterGraphCompiler",
    "FilmGrainOp",
    "FilterOp",
    "GraphInput",
    "LUT3DOp",
    "LoudnessNormOp",
    "ScalePadOp",
    "StreamKind",
    "TrimOp",
    "TrimStartOp",
    "VignetteOp",
]
