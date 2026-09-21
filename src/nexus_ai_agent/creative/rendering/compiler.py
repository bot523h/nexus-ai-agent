"""Pure compiler: :class:`LaneIR` → filtergraph → byte-exact argv.

Everything here is a pure function of its input — no processes, no I/O — so
every op pins a golden argv test without ever executing FFmpeg.  The executor
is the only module allowed to run a process.

Two-pass loudness: :func:`compile_measure` derives the *measure* graph (the
full mix with the ``loudnorm`` op in ``print_format=json`` mode); its parsed
output feeds :func:`compile_lane` via :class:`MeasuredLoudness`, which emits
the linear ``loudnorm`` apply filter.  This is the film-standard EBU R128
two-pass flow.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from nexus_ai_agent.creative.rendering.ir import (
    DuckOp,
    FreezeOp,
    LaneError,
    LaneIR,
    LaneOp,
    LaneSource,
    LoudnormOp,
    ReverseOp,
    SpeedOp,
    TitleOp,
    TrimOp,
    XfadeOp,
)

MICROSECONDS_PER_SECOND = 1_000_000


class MeasuredLoudness(BaseModel):
    """Parsed ``loudnorm`` first-pass JSON (EBU R128 two-pass).

    ``extra`` is ignored on purpose: FFmpeg also reports ``output_*`` fields
    (and may add more), of which the linear apply pass needs nothing.
    """

    model_config = ConfigDict(extra="ignore", frozen=True, populate_by_name=True)

    measured_I: float = Field(alias="input_i")
    measured_TP: float = Field(alias="input_tp")
    measured_LRA: float = Field(alias="input_lra")
    measured_thresh: float = Field(alias="input_thresh")
    offset: float = Field(alias="target_offset")


@dataclass(frozen=True)
class CompiledLane:
    inputs: tuple[str, ...]
    filtergraph: str
    video_out: str | None
    audio_out: str | None
    duration_us: int
    ir_hash: str
    profile_args: tuple[str, ...]

    def argv(self, output_path: Path, *, binary: str = "ffmpeg") -> list[str]:
        """The exact argv the executor will run — assertable without running it."""
        args: list[str] = [binary, "-hide_banner", "-nostdin", "-loglevel", "error"]
        for source in self.inputs:
            args += ["-i", source]
        args += ["-filter_complex", self.filtergraph]
        if self.video_out is not None:
            args += ["-map", f"[{self.video_out}]"]
        if self.audio_out is not None:
            args += ["-map", f"[{self.audio_out}]"]
        args += list(self.profile_args)
        args += ["-t", _seconds(self.duration_us), "-y", str(output_path)]
        return args

    def measure_argv(self, *, binary: str = "ffmpeg") -> list[str]:
        """Argv for the loudness measure pass (decodes + filters to null).

        NOTE: the ``loudnorm`` JSON summary is logged at *info* level, so this
        pass deliberately does not use ``-loglevel error`` — it would silence
        the very payload the executor parses.
        """
        args: list[str] = [binary, "-hide_banner", "-nostdin"]
        for source in self.inputs:
            args += ["-i", source]
        args += ["-filter_complex", self.filtergraph]
        if self.video_out is not None:
            args += ["-map", f"[{self.video_out}]"]
        if self.audio_out is not None:
            args += ["-map", f"[{self.audio_out}]"]
        args += ["-f", "null", "-"]
        return args


def _seconds(microseconds: int) -> str:
    return f"{microseconds / MICROSECONDS_PER_SECOND:.6f}"


def _escape_drawtext(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", "\\'")
        .replace("%", "\\%")
        .replace(",", "\\,")
    )


def _escape_fontfile(value: str) -> str:
    return value.replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


def _atempo_chain(factor: float) -> list[str]:
    """Decompose a rate factor into ``atempo`` stages (each must be 0.5..2.0)."""
    if abs(factor - 1.0) < 1e-9:
        return []
    stages: list[float] = []
    rest = factor
    while rest > 2.0 + 1e-9:
        stages.append(2.0)
        rest /= 2.0
    while rest < 0.5 - 1e-9:
        stages.append(0.5)
        rest /= 0.5
    stages.append(rest)
    return [f"atempo={stage:.6f}" for stage in stages]


def _ordered_inputs(ir: LaneIR) -> tuple[LaneSource, ...]:
    extras = sorted({s.asset_id: s for s in ir.extra_sources}.values(), key=lambda s: s.asset_id)
    return (ir.main, *extras)


def _track_durations(ir: LaneIR) -> list[int]:
    """Duration (µs) after each op — integer math, validated xfade overlap."""
    durations: list[int] = []
    current = ir.main.duration_us
    by_asset = ir.sources_by_asset
    for op in ir.ops:
        if isinstance(op, TrimOp):
            current = op.out_us - op.in_us
        elif isinstance(op, SpeedOp):
            current = max(1, int(current / op.factor))
        elif isinstance(op, ReverseOp):
            pass
        elif isinstance(op, FreezeOp):
            current = op.hold_us
        elif isinstance(op, XfadeOp):
            other = by_asset[op.other_asset_id]
            if op.offset_us + op.duration_us > current:
                raise LaneError(
                    f"xfade offset+duration ({op.offset_us}+{op.duration_us}) exceeds "
                    f"main duration ({current})"
                )
            if op.duration_us > other.duration_us:
                raise LaneError(
                    f"xfade duration ({op.duration_us}) exceeds "
                    f"other clip duration ({other.duration_us})"
                )
            current = current + other.duration_us - op.duration_us
        durations.append(current)
    return durations


def _normalize_video(width: int, height: int, fps: int) -> str:
    return (
        f"format=yuv420p,scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps={fps}"
    )


_TITLE_POSITIONS = {
    "center": ("(w-text_w)/2", "(h-text_h)/2"),
    "lower_third": ("(w-text_w)/2", "h-2*text_h"),
    "top_header": ("(w-text_w)/2", "text_h"),
}


def compile_lane(
    ir: LaneIR,
    *,
    measured: MeasuredLoudness | None = None,
    fontfile: str | None = None,
) -> CompiledLane:
    """Compile the lane IR into a deterministic filtergraph + profile args."""
    return _compile(ir, measured=measured, fontfile=fontfile, measure_mode=False)


def compile_measure(ir: LaneIR, *, fontfile: str | None = None) -> CompiledLane:
    """Compile the loudness *measure* graph (loudnorm op in JSON-print mode)."""
    if not any(isinstance(op, LoudnormOp) for op in ir.ops):
        raise LaneError("measure pass needs at least one loudnorm op")
    return _compile(ir, measured=None, fontfile=fontfile, measure_mode=True)


def _compile(
    ir: LaneIR,
    *,
    measured: MeasuredLoudness | None,
    fontfile: str | None,
    measure_mode: bool,
) -> CompiledLane:
    if sum(isinstance(op, LoudnormOp) for op in ir.ops) > 1:
        raise LaneError("at most one loudnorm op per lane")
    if any(isinstance(op, LoudnormOp) for op in ir.ops) and not measure_mode:
        if measured is None:
            raise LaneError("loudnorm needs measured values — run measure_loudness first")

    durations = _track_durations(ir)
    total_us = durations[-1] if durations else ir.main.duration_us
    profile = ir.profile
    inputs = _ordered_inputs(ir)
    index_of = {s.asset_id: i for i, s in enumerate(inputs)}

    lines: list[str] = []
    has_video = ir.main.media_kind == "video"

    # -- video chain ---------------------------------------------------------
    video_label: str | None = None
    if has_video:
        normalize = _normalize_video(profile.width, profile.height, profile.fps)
        lines.append(f"[0:v]{normalize}[vbase]")
        video_label = "vbase"
        counter = 0
        for op in ir.ops:
            counter += 1
            nxt = f"v{counter}"
            if isinstance(op, TrimOp):
                lines.append(
                    f"[{video_label}]trim=start={_seconds(op.in_us)}:end={_seconds(op.out_us)},"
                    f"setpts=PTS-STARTPTS[{nxt}]"
                )
            elif isinstance(op, SpeedOp):
                lines.append(f"[{video_label}]setpts=PTS/{op.factor:.6f}[{nxt}]")
            elif isinstance(op, ReverseOp):
                lines.append(f"[{video_label}]reverse[{nxt}]")
            elif isinstance(op, FreezeOp):
                frame = int(op.at_us / MICROSECONDS_PER_SECOND * profile.fps)
                lines.append(
                    f"[{video_label}]select='eq(n\\,{frame})',setpts=N/FRAME_RATE/TB,"
                    f"tpad=stop_mode=clone:stop_duration={_seconds(op.hold_us)}[{nxt}]"
                )
            elif isinstance(op, XfadeOp):
                other_idx = index_of[op.other_asset_id]
                lines.append(
                    f"[{other_idx}:v]"
                    f"{_normalize_video(profile.width, profile.height, profile.fps)}"
                    f"[vx{counter}]"
                )
                lines.append(
                    f"[{video_label}][vx{counter}]xfade=transition={op.kind}:"
                    f"duration={_seconds(op.duration_us)}:offset={_seconds(op.offset_us)}[{nxt}]"
                )
            elif isinstance(op, TitleOp):
                if fontfile is None:
                    raise LaneError("title op requires an explicit fontfile")
                x, y = _TITLE_POSITIONS[op.position]
                if op.end_us is None:
                    enable = f"gte(t,{_seconds(op.start_us)})"
                else:
                    enable = f"between(t,{_seconds(op.start_us)},{_seconds(op.end_us)})"
                lines.append(
                    f"[{video_label}]drawtext=fontfile='{_escape_fontfile(fontfile)}':"
                    f"text='{_escape_drawtext(op.text)}':fontsize={op.font_size}:"
                    f"fontcolor={op.color}:x={x}:y={y}:enable='{enable}'[{nxt}]"
                )
            else:
                continue
            video_label = nxt
        out_label = "vout"
        lines.append(f"[{video_label}]format=yuv420p[{out_label}]")
        video_label = out_label

    # -- audio chain ---------------------------------------------------------
    lines.append("[0:a]aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo[abase]")
    audio_label = "abase"
    counter = 0
    for op, dur_after in zip(ir.ops, durations, strict=True):
        counter += 1
        nxt = f"a{counter}"
        if isinstance(op, TrimOp):
            lines.append(
                f"[{audio_label}]atrim=start={_seconds(op.in_us)}:end={_seconds(op.out_us)},"
                f"asetpts=PTS-STARTPTS[{nxt}]"
            )
        elif isinstance(op, SpeedOp):
            stages = _atempo_chain(op.factor)
            if not stages:
                continue
            lines.append(f"[{audio_label}]{','.join(stages)}[{nxt}]")
        elif isinstance(op, ReverseOp):
            lines.append(f"[{audio_label}]areverse[{nxt}]")
        elif isinstance(op, FreezeOp):
            frame_us = MICROSECONDS_PER_SECOND // profile.fps
            lines.append(
                f"[{audio_label}]atrim=start={_seconds(op.at_us)}:"
                f"end={_seconds(op.at_us + frame_us)},asetpts=PTS-STARTPTS,"
                f"apad,atrim=0:{_seconds(op.hold_us)},asetpts=PTS-STARTPTS[{nxt}]"
            )
        elif isinstance(op, XfadeOp):
            lines.append(
                f"[{audio_label}]apad=whole_dur={_seconds(dur_after)},asetpts=PTS-STARTPTS[{nxt}]"
            )
        elif isinstance(op, LoudnormOp):
            if measure_mode:
                lines.append(
                    f"[{audio_label}]loudnorm=I={op.target_lufs:.2f}:TP={op.true_peak_db:.2f}:"
                    f"LRA={op.lra:.2f}:print_format=json[{nxt}]"
                )
            else:
                assert measured is not None
                lines.append(
                    f"[{audio_label}]loudnorm=linear=true:I={op.target_lufs:.2f}:"
                    f"TP={op.true_peak_db:.2f}:LRA={op.lra:.2f}:"
                    f"measured_I={measured.measured_I:.2f}:"
                    f"measured_TP={measured.measured_TP:.2f}:"
                    f"measured_LRA={measured.measured_LRA:.2f}:"
                    f"measured_thresh={measured.measured_thresh:.2f}:"
                    f"offset={measured.offset:.2f}[{nxt}]"
                )
        elif isinstance(op, DuckOp):
            voice_idx = index_of[op.voice_asset_id]
            ratio = min(20.0, max(1.0, 10.0 ** (-op.attenuation_db / 20.0)))
            lines.append(f"[{voice_idx}:a]asplit[vsc{counter}][vmix{counter}]")
            lines.append(
                f"[{audio_label}][vsc{counter}]sidechaincompress=threshold=0.02:"
                f"ratio={ratio:.2f}:attack={op.attack_ms}:release={op.release_ms}[duck{counter}]"
            )
            lines.append(
                f"[duck{counter}][vmix{counter}]amix=inputs=2:duration=longest:"
                f"dropout_transition=0[{nxt}]"
            )
        else:
            continue
        audio_label = nxt
    lines.append(
        f"[{audio_label}]aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo[aout]"
    )
    audio_label = "aout"

    profile_args: list[str] = []
    if has_video:
        profile_args += [
            "-c:v",
            profile.video_codec,
            "-preset",
            profile.preset,
            "-crf",
            str(profile.crf),
            "-pix_fmt",
            "yuv420p",
            "-r",
            str(profile.fps),
            "-movflags",
            "+faststart",
        ]
    else:
        profile_args += ["-vn"]
    profile_args += ["-c:a", profile.audio_codec, "-b:a", profile.audio_bitrate]

    payload = {
        "inputs": [s.path for s in inputs],
        "ops": [op.model_dump(mode="json") for op in ir.ops],
        "profile": {
            "width": profile.width,
            "height": profile.height,
            "fps": profile.fps,
            "crf": profile.crf,
            "preset": profile.preset,
            "video_codec": profile.video_codec,
            "audio_codec": profile.audio_codec,
            "audio_bitrate": profile.audio_bitrate,
        },
        "container": ir.container,
        "measured": measured.model_dump(mode="json") if measured else None,
        "fontfile": fontfile,
        "measure_mode": measure_mode,
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    return CompiledLane(
        inputs=tuple(s.path for s in inputs),
        filtergraph=";".join(lines),
        video_out=video_label,
        audio_out=audio_label,
        duration_us=total_us,
        ir_hash="sha256:" + digest,
        profile_args=tuple(profile_args),
    )


__all__ = [
    "CompiledLane",
    "LaneOp",
    "MeasuredLoudness",
    "compile_lane",
    "compile_measure",
]
