"""Pure compiler: :class:`LaneIR` → filtergraph → byte-exact argv.

Everything here is a pure function of its input — no processes, no I/O — so
every op pins a golden argv test without ever executing FFmpeg.  The executor
is the only module allowed to run a process.

Two-pass loudness: :func:`compile_measure` derives the *measure* graph (the
full mix with the ``loudnorm`` op in ``print_format=json`` mode); its parsed
output feeds :func:`compile_lane` via :class:`MeasuredLoudness`, which emits
the linear ``loudnorm`` apply filter.  This is the film-standard EBU R128
two-pass flow.

Multi-segment assembly: :func:`compile_assembly` compiles a
:class:`LaneAssembly` (ordered lane segments plus timeline gaps) into **one**
:class:`CompiledLane` — one filtergraph with one ``concat`` stage, executed by
the same executor in exactly one FFmpeg process.  The per-op emission is shared
with the single-lane path (:func:`_video_chain` / :func:`_audio_chain`), so an
assembly of one segment compiles exactly like :func:`compile_lane` minus the
concat tail, and the single-lane golden tests pin the shared emission
byte-for-byte.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from nexus_ai_agent.creative.rendering.ir import (
    COLOR_TEMPERATURE_NEUTRAL_K,
    AssemblyGap,
    DuckOp,
    ExposureOp,
    FreezeOp,
    LaneAssembly,
    LaneError,
    LaneIR,
    LaneOp,
    LaneProfile,
    LaneSource,
    LoudnormOp,
    LutOp,
    ReverseOp,
    SpeedOp,
    SubtitleOp,
    TitleOp,
    TrimOp,
    XfadeOp,
)

MICROSECONDS_PER_SECOND = 1_000_000

#: The lane's canonical stereo mix format (single source of truth for every
#: audio chain head, tail, and generated-silence gap).
_AUDIO_FORMAT = "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo"


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
        # TitleOp / LoudnormOp / DuckOp / ExposureOp / LutOp / SubtitleOp
        # deliberately fall through: they touch pixels or samples, never the
        # clock, so `current` carries over unchanged.
        # test_lane_duration_algebra.py restates this algebra independently over
        # seeded random lanes and pins the agreement.
        durations.append(current)
    return durations


def _normalize_video(width: int, height: int, fps: int) -> str:
    return (
        f"format=yuv420p,scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps={fps}"
    )


def _exposure_stage(op: ExposureOp) -> str:
    """The exposure filter chain for one :class:`ExposureOp`.

    Two hard facts about FFmpeg drive the shape of this string (both verified in
    the filter sources, recorded as D-0007):

    * ``eq`` accepts only planar YUV/gray, and at neutral it is a *true* no-op —
      ``vf_eq.c`` ``check_values`` sets ``adjust = NULL`` when contrast is 1.0,
      brightness 0.0 and gamma 1.0.  So ``eq`` is always emitted, in place, with
      no pixel-format cost.
    * ``colortemperature`` and ``colorbalance`` accept only RGB, and neither
      short-circuits; ``kelvin2rgb(6500)`` is ≈ (1.000, 0.997, 0.981), **not** an
      exact identity.  So both are elided at neutral — which also removes the
      whole ``yuv420p → rgb24 → yuv420p`` round trip for an exposure-only edit.

    When a white-balance stage is needed the round trip happens exactly once and
    both filters share it, and the stream returns to ``yuv420p`` so every
    downstream op sees the format it would have seen anyway.
    """
    chain = [f"eq=gamma={op.gamma:.6f}:contrast={op.contrast:.6f}"]
    if not op.needs_rgb_pass:
        return ",".join(chain)
    preserve = 1 if op.preserve_lightness else 0
    rgb_stages = ["format=rgb24"]
    if op.temperature_k != COLOR_TEMPERATURE_NEUTRAL_K:
        rgb_stages.append(f"colortemperature=temperature={op.temperature_k}:pl={preserve}")
    if op.tint != 0.0:
        rgb_stages.append(f"colorbalance=gm={op.tint_gm:.6f}:pl={preserve}")
    rgb_stages.append("format=yuv420p")
    return ",".join([*chain, *rgb_stages])


_TITLE_POSITIONS = {
    "center": ("(w-text_w)/2", "(h-text_h)/2"),
    "lower_third": ("(w-text_w)/2", "h-2*text_h"),
    "top_header": ("(w-text_w)/2", "text_h"),
}


@dataclass(frozen=True)
class _ChainOptions:
    """Everything the shared chain emitters need besides the op itself."""

    fps: int
    fontfile: str | None
    fontsdir: str | None
    measured: MeasuredLoudness | None
    measure_mode: bool


def _video_op_lines(
    op: object,
    *,
    label_in: str,
    label_out: str,
    counter: int,
    tag: str,
    index_of: dict[str, int],
    options: _ChainOptions,
    normalize: str,
) -> list[str] | None:
    """Filter lines for one op on a video chain — ``None`` when audio-only.

    Shared by the single-lane path (``tag=""``) and the assembly path
    (``tag="s{i}_"``); the single-lane golden tests pin this emission
    byte-for-byte, so any change here must keep them green.
    """
    if isinstance(op, TrimOp):
        return [
            f"[{label_in}]trim=start={_seconds(op.in_us)}:end={_seconds(op.out_us)},"
            f"setpts=PTS-STARTPTS[{label_out}]"
        ]
    if isinstance(op, SpeedOp):
        return [f"[{label_in}]setpts=PTS/{op.factor:.6f}[{label_out}]"]
    if isinstance(op, ReverseOp):
        return [f"[{label_in}]reverse[{label_out}]"]
    if isinstance(op, FreezeOp):
        frame = int(op.at_us / MICROSECONDS_PER_SECOND * options.fps)
        return [
            f"[{label_in}]select='eq(n\\,{frame})',setpts=N/FRAME_RATE/TB,"
            f"tpad=stop_mode=clone:stop_duration={_seconds(op.hold_us)}[{label_out}]"
        ]
    if isinstance(op, XfadeOp):
        other_idx = index_of[op.other_asset_id]
        return [
            f"[{other_idx}:v]{normalize}[{tag}vx{counter}]",
            f"[{label_in}][{tag}vx{counter}]xfade=transition={op.kind}:"
            f"duration={_seconds(op.duration_us)}:offset={_seconds(op.offset_us)}[{label_out}]",
        ]
    if isinstance(op, TitleOp):
        fontfile = options.fontfile
        if fontfile is None:
            raise LaneError("title op requires an explicit fontfile")
        x, y = _TITLE_POSITIONS[op.position]
        if op.end_us is None:
            enable = f"gte(t,{_seconds(op.start_us)})"
        else:
            enable = f"between(t,{_seconds(op.start_us)},{_seconds(op.end_us)})"
        return [
            f"[{label_in}]drawtext=fontfile='{_escape_fontfile(fontfile)}':"
            f"text='{_escape_drawtext(op.text)}':fontsize={op.font_size}:"
            f"fontcolor={op.color}:x={x}:y={y}:enable='{enable}'[{label_out}]"
        ]
    if isinstance(op, ExposureOp):
        return [f"[{label_in}]{_exposure_stage(op)}[{label_out}]"]
    if isinstance(op, LutOp):
        lut_file = _escape_fontfile(op.lut_path)
        if op.intensity >= 1.0:
            return [f"[{label_in}]lut3d=file='{lut_file}'[{label_out}]"]
        # Blend graded over original so intensity is honored for real —
        # split preserves timestamps on both branches, blend mixes them.
        branch_a = f"{tag}lut{counter}a"
        branch_b = f"{tag}lut{counter}b"
        graded = f"{tag}lut{counter}g"
        return [
            f"[{label_in}]split[{branch_a}][{branch_b}]",
            f"[{branch_b}]lut3d=file='{lut_file}'[{graded}]",
            f"[{branch_a}][{graded}]blend=all_mode='normal':"
            f"all_opacity={op.intensity:.6f}[{label_out}]",
        ]
    if isinstance(op, SubtitleOp):
        stage = f"subtitles=filename='{_escape_fontfile(op.subtitle_path)}'"
        if options.fontsdir is not None:
            stage += f":fontsdir='{_escape_fontfile(options.fontsdir)}'"
        if op.force_style:
            stage += f":force_style='{_escape_drawtext(op.force_style)}'"
        return [f"[{label_in}]{stage}[{label_out}]"]
    return None


def _audio_op_lines(
    op: object,
    *,
    label_in: str,
    label_out: str,
    counter: int,
    tag: str,
    index_of: dict[str, int],
    dur_after: int,
    options: _ChainOptions,
) -> list[str] | None:
    """Filter lines for one op on an audio chain — ``None`` when video-only."""
    if isinstance(op, TrimOp):
        return [
            f"[{label_in}]atrim=start={_seconds(op.in_us)}:end={_seconds(op.out_us)},"
            f"asetpts=PTS-STARTPTS[{label_out}]"
        ]
    if isinstance(op, SpeedOp):
        stages = _atempo_chain(op.factor)
        if not stages:
            return None
        return [f"[{label_in}]{','.join(stages)}[{label_out}]"]
    if isinstance(op, ReverseOp):
        return [f"[{label_in}]areverse[{label_out}]"]
    if isinstance(op, FreezeOp):
        frame_us = MICROSECONDS_PER_SECOND // options.fps
        return [
            f"[{label_in}]atrim=start={_seconds(op.at_us)}:"
            f"end={_seconds(op.at_us + frame_us)},asetpts=PTS-STARTPTS,"
            f"apad,atrim=0:{_seconds(op.hold_us)},asetpts=PTS-STARTPTS[{label_out}]"
        ]
    if isinstance(op, XfadeOp):
        return [
            f"[{label_in}]apad=whole_dur={_seconds(dur_after)},asetpts=PTS-STARTPTS[{label_out}]"
        ]
    if isinstance(op, LoudnormOp):
        if options.measure_mode:
            return [
                f"[{label_in}]loudnorm=I={op.target_lufs:.2f}:TP={op.true_peak_db:.2f}:"
                f"LRA={op.lra:.2f}:print_format=json[{label_out}]"
            ]
        assert options.measured is not None
        measured = options.measured
        return [
            f"[{label_in}]loudnorm=linear=true:I={op.target_lufs:.2f}:"
            f"TP={op.true_peak_db:.2f}:LRA={op.lra:.2f}:"
            f"measured_I={measured.measured_I:.2f}:"
            f"measured_TP={measured.measured_TP:.2f}:"
            f"measured_LRA={measured.measured_LRA:.2f}:"
            f"measured_thresh={measured.measured_thresh:.2f}:"
            f"offset={measured.offset:.2f}[{label_out}]"
        ]
    if isinstance(op, DuckOp):
        voice_idx = index_of[op.voice_asset_id]
        ratio = min(20.0, max(1.0, 10.0 ** (-op.attenuation_db / 20.0)))
        return [
            f"[{voice_idx}:a]asplit[{tag}vsc{counter}][{tag}vmix{counter}]",
            f"[{label_in}][{tag}vsc{counter}]sidechaincompress=threshold=0.02:"
            f"ratio={ratio:.2f}:attack={op.attack_ms}:release={op.release_ms}[{tag}duck{counter}]",
            f"[{tag}duck{counter}][{tag}vmix{counter}]amix=inputs=2:duration=longest:"
            f"dropout_transition=0[{label_out}]",
        ]
    return None


def _video_chain(
    ops: tuple[object, ...],
    *,
    base_index: int,
    index_of: dict[str, int],
    options: _ChainOptions,
    normalize: str,
    tag: str,
    base_label: str,
    out_label: str,
) -> tuple[list[str], str]:
    """Emit one normalize → ops → yuv420p video chain; returns (lines, label)."""
    lines = [f"[{base_index}:v]{normalize}[{tag}{base_label}]"]
    label = f"{tag}{base_label}"
    counter = 0
    for op in ops:
        counter += 1
        nxt = f"{tag}v{counter}"
        emitted = _video_op_lines(
            op,
            label_in=label,
            label_out=nxt,
            counter=counter,
            tag=tag,
            index_of=index_of,
            options=options,
            normalize=normalize,
        )
        if emitted is None:
            continue
        lines.extend(emitted)
        label = nxt
    lines.append(f"[{label}]format=yuv420p[{tag}{out_label}]")
    return lines, f"{tag}{out_label}"


def _audio_chain(
    ops: tuple[object, ...],
    durations: list[int],
    *,
    base_index: int,
    index_of: dict[str, int],
    options: _ChainOptions,
    tag: str,
    base_label: str,
    out_label: str,
) -> tuple[list[str], str]:
    """Emit one aformat → ops → aformat audio chain; returns (lines, label)."""
    lines = [f"[{base_index}:a]{_AUDIO_FORMAT}[{tag}{base_label}]"]
    label = f"{tag}{base_label}"
    counter = 0
    for op, dur_after in zip(ops, durations, strict=True):
        counter += 1
        nxt = f"{tag}a{counter}"
        emitted = _audio_op_lines(
            op,
            label_in=label,
            label_out=nxt,
            counter=counter,
            tag=tag,
            index_of=index_of,
            dur_after=dur_after,
            options=options,
        )
        if emitted is None:
            continue
        lines.extend(emitted)
        label = nxt
    lines.append(f"[{label}]{_AUDIO_FORMAT}[{tag}{out_label}]")
    return lines, f"{tag}{out_label}"


def compile_lane(
    ir: LaneIR,
    *,
    measured: MeasuredLoudness | None = None,
    fontfile: str | None = None,
    fontsdir: str | None = None,
) -> CompiledLane:
    """Compile the lane IR into a deterministic filtergraph + profile args."""
    return _compile(ir, measured=measured, fontfile=fontfile, fontsdir=fontsdir, measure_mode=False)


def compile_measure(
    ir: LaneIR, *, fontfile: str | None = None, fontsdir: str | None = None
) -> CompiledLane:
    """Compile the loudness *measure* graph (loudnorm op in JSON-print mode)."""
    if not any(isinstance(op, LoudnormOp) for op in ir.ops):
        raise LaneError("measure pass needs at least one loudnorm op")
    return _compile(ir, measured=None, fontfile=fontfile, fontsdir=fontsdir, measure_mode=True)


def _compile(
    ir: LaneIR,
    *,
    measured: MeasuredLoudness | None,
    fontfile: str | None,
    fontsdir: str | None,
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
    options = _ChainOptions(
        fps=profile.fps,
        fontfile=fontfile,
        fontsdir=fontsdir,
        measured=measured,
        measure_mode=measure_mode,
    )

    lines: list[str] = []
    has_video = ir.main.media_kind == "video"
    if not has_video and any(isinstance(op, (ExposureOp, LutOp, SubtitleOp)) for op in ir.ops):
        # Fail closed: on an audio-only lane the video chain is never built, so
        # a pixel op would be silently dropped and the master would come back
        # ungraded while the journal claimed it was graded.
        guilty = next(op.op for op in ir.ops if isinstance(op, (ExposureOp, LutOp, SubtitleOp)))
        raise LaneError(f"{guilty} op needs a video main asset")

    # -- video chain ---------------------------------------------------------
    video_label: str | None = None
    if has_video:
        normalize = _normalize_video(profile.width, profile.height, profile.fps)
        video_lines, video_label = _video_chain(
            ir.ops,
            base_index=0,
            index_of=index_of,
            options=options,
            normalize=normalize,
            tag="",
            base_label="vbase",
            out_label="vout",
        )
        lines.extend(video_lines)

    # -- audio chain ---------------------------------------------------------
    audio_lines, audio_label = _audio_chain(
        ir.ops,
        durations,
        base_index=0,
        index_of=index_of,
        options=options,
        tag="",
        base_label="abase",
        out_label="aout",
    )
    lines.extend(audio_lines)

    profile_args = _profile_args(profile, has_video=has_video)

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
        "fontsdir": fontsdir,
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


def _profile_args(profile: LaneProfile, *, has_video: bool) -> list[str]:
    args: list[str] = []
    if has_video:
        args += [
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
        args += ["-vn"]
    args += ["-c:a", profile.audio_codec, "-b:a", profile.audio_bitrate]
    return args


def _assembly_inputs(assembly: LaneAssembly) -> tuple[tuple[LaneSource, ...], list[dict[str, int]]]:
    """Global input order + per-piece asset→index maps (piece order, main first)."""
    ordered: list[LaneSource] = []
    per_piece: list[dict[str, int]] = []
    for piece in assembly.segments:
        local = _ordered_inputs(piece)
        mapping: dict[str, int] = {}
        for source in local:
            mapping[source.asset_id] = len(ordered)
            ordered.append(source)
        per_piece.append(mapping)
    return tuple(ordered), per_piece


def _assembly_profile_payload(profile: LaneProfile) -> dict[str, object]:
    return {
        "width": profile.width,
        "height": profile.height,
        "fps": profile.fps,
        "crf": profile.crf,
        "preset": profile.preset,
        "video_codec": profile.video_codec,
        "audio_codec": profile.audio_codec,
        "audio_bitrate": profile.audio_bitrate,
    }


def compile_assembly(
    assembly: LaneAssembly, *, fontfile: str | None = None, fontsdir: str | None = None
) -> CompiledLane:
    """Compile ordered segments + gaps into one single-process :class:`CompiledLane`.

    Every segment keeps its own ops (trim, speed, exposure, …); gaps become
    generated black video + generated silence; one ``concat`` stage joins all
    of them in timeline order.  The result runs through the unchanged
    :func:`~nexus_ai_agent.creative.rendering.executor.encode_lane`.

    Fail-closed limits (each a :class:`LaneError`, each pinned by a test):

    * no segments, adjacent gaps, or non-positive gap durations;
    * mixed video/audio segment mains;
    * a segment whose profile/container differs from the assembly's;
    * any ``loudnorm`` op (the two-pass measure flow is single-lane);
    * a pixel op (``exposure``/``lut``/``subtitle``) in an audio-only assembly
      (same rule as single-lane).
    """
    segments = assembly.segments
    if not assembly.pieces or not segments:
        raise LaneError("assembly needs at least one lane segment")
    previous_was_gap = False
    for piece in assembly.pieces:
        if isinstance(piece, AssemblyGap):
            if previous_was_gap:
                raise LaneError("assembly has adjacent gaps — merge them at plan level")
            if piece.duration_us <= 0:
                raise LaneError(f"gap duration must be positive, got {piece.duration_us}")
            previous_was_gap = True
        else:
            previous_was_gap = False
    kinds = {segment.main.media_kind for segment in segments}
    if kinds - {"video", "audio"}:
        raise LaneError(f"assembly segment mains must be video or audio, got {sorted(kinds)}")
    if len(kinds) > 1:
        raise LaneError(f"assembly mixes segment media kinds: {sorted(kinds)}")
    has_video = "video" in kinds
    for segment in segments:
        if segment.profile != assembly.profile:
            raise LaneError("every assembly segment must share the assembly profile")
        if segment.container != assembly.container:
            raise LaneError("every assembly segment must share the assembly container")
        if any(isinstance(op, LoudnormOp) for op in segment.ops):
            raise LaneError(
                "loudnorm in an assembly needs a per-piece measure pass — "
                "render the pieces separately or extend compile_measure"
            )
        if not has_video and any(
            isinstance(op, (ExposureOp, LutOp, SubtitleOp)) for op in segment.ops
        ):
            guilty = next(
                op.op for op in segment.ops if isinstance(op, (ExposureOp, LutOp, SubtitleOp))
            )
            raise LaneError(f"{guilty} op needs a video main asset")

    profile = assembly.profile
    normalize = _normalize_video(profile.width, profile.height, profile.fps)
    options = _ChainOptions(
        fps=profile.fps, fontfile=fontfile, fontsdir=fontsdir, measured=None, measure_mode=False
    )
    ordered, per_piece = _assembly_inputs(assembly)

    lines: list[str] = []
    concat_inputs: list[str] = []
    total_us = 0
    piece_position = 0
    gap_position = 0
    for piece in assembly.pieces:
        if isinstance(piece, AssemblyGap):
            tag = f"g{gap_position}_"
            gap_position += 1
            if has_video:
                lines.append(
                    f"color=c=black:s={profile.width}x{profile.height}:r={profile.fps}:"
                    f"d={_seconds(piece.duration_us)},{normalize}[{tag}vout]"
                )
                concat_inputs.append(f"[{tag}vout]")
            lines.append(
                f"anullsrc=r=48000:cl=stereo:d={_seconds(piece.duration_us)},"
                f"{_AUDIO_FORMAT},asetpts=PTS-STARTPTS[{tag}aout]"
            )
            concat_inputs.append(f"[{tag}aout]")
            total_us += piece.duration_us
            continue
        tag = f"s{piece_position}_"
        mapping = per_piece[piece_position]
        main_index = mapping[piece.main.asset_id]
        durations = _track_durations(piece)
        total_us += durations[-1] if durations else piece.main.duration_us
        if has_video:
            video_lines, video_label = _video_chain(
                piece.ops,
                base_index=main_index,
                index_of=mapping,
                options=options,
                normalize=normalize,
                tag=tag,
                base_label="vbase",
                out_label="vout",
            )
            lines.extend(video_lines)
            concat_inputs.append(f"[{video_label}]")
        audio_lines, audio_label = _audio_chain(
            piece.ops,
            durations,
            base_index=main_index,
            index_of=mapping,
            options=options,
            tag=tag,
            base_label="abase",
            out_label="aout",
        )
        lines.extend(audio_lines)
        concat_inputs.append(f"[{audio_label}]")
        piece_position += 1

    stage_count = len(assembly.pieces)
    if has_video:
        lines.append(f"{''.join(concat_inputs)}concat=n={stage_count}:v=1:a=1[vcat][acat]")
        lines.append("[vcat]format=yuv420p[vout]")
        video_out: str | None = "vout"
    else:
        lines.append(f"{''.join(concat_inputs)}concat=n={stage_count}:v=0:a=1[acat]")
        video_out = None
    lines.append(f"[acat]{_AUDIO_FORMAT}[aout]")

    payload = {
        "assembly": [
            (
                {"gap_us": piece.duration_us}
                if isinstance(piece, AssemblyGap)
                else {
                    "inputs": [s.path for s in _ordered_inputs(piece)],
                    "ops": [op.model_dump(mode="json") for op in piece.ops],
                }
            )
            for piece in assembly.pieces
        ],
        "profile": _assembly_profile_payload(profile),
        "container": assembly.container,
        "fontfile": fontfile,
        "fontsdir": fontsdir,
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    return CompiledLane(
        inputs=tuple(s.path for s in ordered),
        filtergraph=";".join(lines),
        video_out=video_out,
        audio_out="aout",
        duration_us=total_us,
        ir_hash="sha256:" + digest,
        profile_args=tuple(_profile_args(profile, has_video=has_video)),
    )


__all__ = [
    "CompiledLane",
    "LaneAssembly",
    "LaneOp",
    "MeasuredLoudness",
    "compile_assembly",
    "compile_lane",
    "compile_measure",
]
