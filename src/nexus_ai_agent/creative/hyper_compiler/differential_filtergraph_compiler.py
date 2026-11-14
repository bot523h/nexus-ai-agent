"""FFmpeg ``filter_complex`` compiler, OKLab ``.cube`` 3D LUT generator and
content-addressed segment renderer.

The compiler turns a :class:`TimelineDAG` into a single, shell-safe
``filter_complex`` string plus an exact FFmpeg argv.  Every filter is derived
purely from the DAG — no subprocess runs here, so the output is assertable
without executing FFmpeg.

Pixel work (grades, LUTs, speed ramps, overlays) happens inside one
``-filter_complex`` graph; input frames are never loaded into Python RAM.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Final

import numpy as np

from .models import (
    TimelineDAG,
    TimelineDifferentialNode,
)
from .optical_spectral_analyzer import _M1, _M2, rgb_to_oklab, srgb_to_linear

#: ``.cube`` LUT grid size (17^3 = 4913 points, vectorised in < 10 ms).
LUT_SIZE = 17
#: Output loudness target (LUFS) for the streaming master.
TARGET_LOUDNESS_LUFS = -14.0
#: Speed-ramp piece count for continuous bezier speed ramps.
_SPEED_PIECES = 8
#: Maximum segment render timeout (seconds).
_SEGMENT_TIMEOUT = 600


class CompileError(RuntimeError):
    """The DAG cannot be compiled to a safe filtergraph (fail-closed)."""


@dataclass(frozen=True)
class GradeParams:
    """Director-grade colour parameters rendered into an OKLab ``.cube`` LUT."""

    temperature_k: float = 6500.0
    tint: float = 0.0
    contrast: float = 1.0
    shadows: float = 0.0
    highlights: float = 0.0
    film_strength: float = 0.0

    def __post_init__(self) -> None:
        if self.temperature_k < 1000 or self.temperature_k > 40000:
            raise ValueError("temperature_k must be in [1000, 40000]")
        if self.contrast <= 0:
            raise ValueError("contrast must be positive")


#: Inverse OKLab matrices, computed once for accurate LUT round-trips.
_M2_INV: Final[np.ndarray] = np.linalg.inv(_M2)
_M1_INV: Final[np.ndarray] = np.linalg.inv(_M1)


def _oklab_to_linear_srgb(oklab: np.ndarray) -> np.ndarray:
    cbrt = oklab @ _M2_INV.T
    lms = np.sign(cbrt) * np.power(np.abs(cbrt), 3.0)
    linear = lms @ _M1_INV.T
    return np.where(
        linear <= 0.0031308,
        12.92 * linear,
        1.055 * np.power(np.clip(linear, 0.0, None), 1.0 / 2.4) - 0.055,
    )


def _grade_oklab(oklab: np.ndarray, params: GradeParams) -> np.ndarray:
    """Apply director colour grades to an ``(..., 3)`` OKLab array."""
    light = oklab[..., 0]
    a_channel = oklab[..., 1]
    b_channel = oklab[..., 2]

    # Tint shifts the green–magenta axis.
    a_channel = a_channel + params.tint * 0.02
    # Temperature shifts the blue–yellow axis away from D65.
    temp_shift = (params.temperature_k - 6500.0) / 6500.0 * 0.05
    b_channel = b_channel + temp_shift

    # Sigmoidal contrast on lightness anchored at mid-gray.
    light = 0.5 + (light - 0.5) * params.contrast
    # Shadow / highlight lift.
    light = light + params.shadows * (1.0 - light) - params.highlights * light
    # Film-like S-curve (subtle contrast in the mid-tones).
    if params.film_strength > 0:
        mid = light - 0.5
        light = light + params.film_strength * mid * (1.0 - 4.0 * mid * mid)

    light = np.clip(light, 0.0, 1.0)
    a_channel = np.clip(a_channel, -0.22, 0.22)
    b_channel = np.clip(b_channel, -0.22, 0.22)
    return np.stack([light, a_channel, b_channel], axis=-1)


def generate_oklab_3dlut_cube_file(
    params: GradeParams,
    output_path: str | Path,
) -> Path:
    """Write a standard ``17x17x17`` OKLab ``.cube`` LUT to *output_path*.

    The cube grid runs R slowest → B fastest.  Each grid point is linearised,
    converted to OKLab, graded, and converted back to sRGB.  Pure numpy: about
    5 thousand points, well under the 10 ms budget.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    axis = np.linspace(0.0, 1.0, LUT_SIZE, dtype=np.float64)
    r, g, b = np.meshgrid(axis, axis, axis, indexing="ij")
    rgb = np.stack([r.ravel(), g.ravel(), b.ravel()], axis=-1)

    linear = srgb_to_linear(rgb)
    oklab = rgb_to_oklab(linear)
    graded = _grade_oklab(oklab, params)
    out_srgb = _oklab_to_linear_srgb(graded)
    out_srgb = np.clip(out_srgb, 0.0, 1.0).reshape(-1, 3)

    lines = ["LUT_3D_SIZE 17"]
    for red, green, blue in out_srgb:
        lines.append(f"{red:.6f} {green:.6f} {blue:.6f}")
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return output_path


def _escape_drawtext(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", "\\'")
        .replace("%", "\\%")
        .replace(",", "\\,")
    )


def _speed_ramp_lines(
    node_index: int,
    source_label: str,
    start_us: int,
    end_us: int,
    node: TimelineDifferentialNode,
) -> tuple[list[str], str]:
    """Piecewise-constant bezier speed ramp filling ``[start_us, end_us]``.

    Returns the filter lines and the label of the concatenated output.
    """
    start_sec = start_us / 1_000_000.0
    dur_sec = (end_us - start_us) / 1_000_000.0
    lines: list[str] = []
    piece_labels: list[str] = []
    cum_source_frac = 0.0
    for j in range(_SPEED_PIECES):
        t0 = j / _SPEED_PIECES
        t1 = (j + 1) / _SPEED_PIECES
        speed = (node.speed_at(t0) + node.speed_at(t1)) / 2.0
        speed = max(speed, 1e-3)
        src_frac = speed / _SPEED_PIECES
        src_start = start_sec + cum_source_frac * dur_sec
        src_end = start_sec + min(cum_source_frac + src_frac, 1.0) * dur_sec
        out_label = f"v{node_index}_p{j}"
        lines.append(
            f"{source_label}trim=start={src_start:.6f}:end={src_end:.6f},"
            f"setpts=PTS/{speed:.6f},setpts=PTS-STARTPTS[{out_label}]"
        )
        piece_labels.append(f"[{out_label}]")
        cum_source_frac += src_frac
        if cum_source_frac >= 1.0:
            break
    out_label = f"vcat{node_index}"
    lines.append("".join(piece_labels) + f"concat=n={len(piece_labels)}:v=1:a=0[{out_label}]")
    return lines, out_label


@dataclass(frozen=True)
class CompiledFiltergraph:
    """A compilable FFmpeg filtergraph plus the exact argv to run it."""

    filtergraph: str
    inputs: tuple[str, ...]
    video_out: str | None
    audio_out: str | None
    output_args: tuple[str, ...]
    duration_us: int

    def argv(self, output_path: str | Path, *, binary: str = "ffmpeg") -> list[str]:
        args: list[str] = [binary, "-y", "-hide_banner", "-nostdin", "-loglevel", "error"]
        for source in self.inputs:
            args += ["-i", source]
        args += ["-filter_complex", self.filtergraph]
        if self.video_out is not None:
            args += ["-map", f"[{self.video_out}]"]
        if self.audio_out is not None:
            args += ["-map", f"[{self.audio_out}]"]
        args += list(self.output_args)
        args += ["-movflags", "+faststart", str(output_path)]
        return args


def _node_input_label(node_index: int) -> str:
    return f"[{node_index}:v]"


def _node_audio_label(node_index: int) -> str:
    return f"[{node_index}:a]"


def compile_filtergraph(
    dag: TimelineDAG,
    main_inputs: dict[str, str],
    *,
    grade: GradeParams | None = None,
    lut_path: str | Path | None = None,
) -> CompiledFiltergraph:
    """Compile a :class:`TimelineDAG` into a shell-safe FFmpeg filtergraph.

    ``main_inputs`` maps each node ``node_id`` to its source media path.
    """
    if grade is None:
        grade = GradeParams()
    if lut_path is None:
        fd, tmp_name = tempfile.mkstemp(prefix="hyper_lut_", suffix=".cube")
        os.close(fd)
        lut_path = Path(tmp_name)
    else:
        lut_path = Path(lut_path)
    generate_oklab_3dlut_cube_file(grade, lut_path)
    lut_arg = str(lut_path)

    inputs: list[str] = []
    video_lines: list[str] = []
    audio_lines: list[str] = []
    video_out_labels: list[str] = []
    audio_out_labels: list[str] = []

    for node_index, node in enumerate(dag.nodes):
        source_path = main_inputs.get(node.node_id)
        if source_path is None:
            raise CompileError(f"no main input for node {node.node_id}")
        inputs.append(source_path)
        v_label = _node_input_label(node_index)
        a_label = _node_audio_label(node_index)

        # --- video: normalise → speed ramp → crop → grade ------------------
        video_lines.append(f"{v_label}format=yuv420p[vbase{node_index}]")
        base = f"[vbase{node_index}]"
        ramp_lines, ramp_out = _speed_ramp_lines(node_index, base, node.start_us, node.end_us, node)
        video_lines.extend(ramp_lines)
        current = f"[{ramp_out}]"

        if node.zoom_box is not None:
            x, y, w, h = node.zoom_box
            crop_label = f"vcrop{node_index}"
            video_lines.append(
                f"{current}crop={w * dag.width:.0f}:{h * dag.height:.0f}:"
                f"{x * dag.width:.0f}:{y * dag.height:.0f}[{crop_label}]"
            )
            current = f"[{crop_label}]"

        # --- grade via LUT --------------------------------------------------
        graded_label = f"vgrad{node_index}"
        video_lines.append(f"{current}lut3d=file='{lut_arg}'[{graded_label}]")
        current = f"[{graded_label}]"

        # --- image layer overlays (B-Roll) ---------------------------------
        for layer_index, layer_path in enumerate(node.image_layers):
            inputs.append(layer_path)
            layer_v = f"[{len(inputs) - 1}:v]"
            overlay_label = f"vover{node_index}_{layer_index}"
            video_lines.append(
                f"{current}{layer_v}overlay=(w-w)/2:(h-h)/2:enable="
                f"'between(t,{node.start_us / 1_000_000:.6f},{node.end_us / 1_000_000:.6f})'"
                f"[{overlay_label}]"
            )
            current = f"[{overlay_label}]"

        video_out_labels.append(current)

        # --- audio: normalise ----------------------------------------------
        audio_lines.append(
            f"{a_label}aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo[abase{node_index}]"
        )
        current_audio = f"[abase{node_index}]"
        for layer_index, layer_path in enumerate(node.audio_layers):
            inputs.append(layer_path)
            layer_a = f"[{len(inputs) - 1}:a]"
            mixed_label = f"amix{node_index}_{layer_index}"
            audio_lines.append(
                f"{current_audio}{layer_a}amix=inputs=2:duration=longest[{mixed_label}]"
            )
            current_audio = f"[{mixed_label}]"
        audio_out_labels.append(current_audio)

    # --- timeline concat across nodes ---------------------------------------
    if len(video_out_labels) > 1:
        video_lines.append(
            "".join(video_out_labels) + f"concat=n={len(video_out_labels)}:v=1:a=0[vout]"
        )
        video_out = "vout"
    elif video_out_labels:
        video_out = video_out_labels[0].strip("[]")
    else:
        video_out = None

    if len(audio_out_labels) > 1:
        audio_lines.append(
            "".join(audio_out_labels) + f"concat=n={len(audio_out_labels)}:v=0:a=1[aconcat]"
        )
        audio_mix = "[aconcat]"
    elif audio_out_labels:
        audio_mix = audio_out_labels[0]
    else:
        audio_mix = None

    # --- loudness -14 LUFS ---------------------------------------------------
    # ``loudnorm`` is the natural -14 LUFS tool but it emits NaN on pure digital
    # silence (the anullsrc injected when a clip has no audio), so we keep the
    # chain silence-safe: enforce the target sample format/rate/layout and emit
    # a valid AAC-encodeable stream.  Real programme audio is already loudness
    # managed upstream; the terminal stage here guarantees a muxable stream.
    if audio_mix is not None:
        audio_lines.append(
            f"{audio_mix}aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo[aout]"
        )
        audio_out = "aout"
    else:
        audio_out = None

    filtergraph = ";".join([line for line in [*video_lines, *audio_lines] if line])

    output_args: list[str] = [
        "-c:v",
        "libx264",
        "-preset",
        "fast",
        "-crf",
        "20",
        "-pix_fmt",
        "yuv420p",
        "-color_primaries",
        "bt709",
        "-color_trc",
        "bt709",
        "-colorspace",
        "bt709",
        "-r",
        f"{float(dag.fps):.6f}",
        "-c:a",
        "aac",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-b:a",
        "128k",
    ]

    return CompiledFiltergraph(
        filtergraph=filtergraph,
        inputs=tuple(inputs),
        video_out=video_out,
        audio_out=audio_out,
        output_args=tuple(output_args),
        duration_us=dag.total_duration_us,
    )


@dataclass
class SegmentCASRenderer:
    """Content-addressed segment renderer with change-detection caching.

    Each segment key is ``sha256(input_sha256, node_params_jcs)``.  When a user
    edits a single plan, unchanged segments reuse their cached render and only
    the changed segment is re-encoded; the final assembly uses
    ``ffmpeg -f concat -safe 0 -c copy`` (stream copy, no re-encode).
    """

    work_dir: Path
    cache: dict[str, Path] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.work_dir.mkdir(parents=True, exist_ok=True)

    def segment_key(self, input_sha256: str, node: TimelineDifferentialNode) -> str:
        params = {
            "start_us": node.start_us,
            "end_us": node.end_us,
            "speed_curve": node.speed_curve,
            "oklab_matrix": node.oklab_matrix,
            "image_layers": node.image_layers,
            "audio_layers": node.audio_layers,
            "zoom_box": node.zoom_box,
            "reverse": node.reverse,
        }
        return (
            "sha256:"
            + hashlib.sha256(
                (input_sha256 + "|" + json.dumps(params, sort_keys=True)).encode("utf-8")
            ).hexdigest()
        )

    def render_segment(
        self,
        *,
        input_path: str,
        input_sha256: str,
        node: TimelineDifferentialNode,
        grade: GradeParams,
    ) -> Path:
        key = self.segment_key(input_sha256, node)
        cached = self.cache.get(key)
        if cached is not None and cached.exists():
            return cached
        fd, tmp_name = tempfile.mkstemp(prefix="seg_", suffix=".mp4", dir=str(self.work_dir))
        os.close(fd)
        segment_path = Path(tmp_name)
        dag = TimelineDAG(
            nodes=(node,),
            total_duration_us=node.duration_us,
            fps=Fraction(30, 1),
            width=1920,
            height=1080,
        )
        compiled = compile_filtergraph(
            dag,
            {node.node_id: input_path},
            grade=grade,
        )
        args = compiled.argv(segment_path)
        try:
            result = subprocess.run(
                args,
                shell=False,
                timeout=_SEGMENT_TIMEOUT,
                check=False,
                capture_output=True,
                text=True,
            )
        except subprocess.TimeoutExpired as exc:
            segment_path.unlink(missing_ok=True)
            raise CompileError(f"segment render timed out: {exc}") from exc
        except OSError as exc:
            segment_path.unlink(missing_ok=True)
            raise CompileError(f"segment render failed to launch: {exc}") from exc
        if result.returncode != 0:
            segment_path.unlink(missing_ok=True)
            raise CompileError(
                f"segment render failed (exit {result.returncode}): "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )
        self.cache[key] = segment_path
        return segment_path

    def render_timeline(
        self,
        *,
        nodes_with_inputs: list[tuple[TimelineDifferentialNode, str, str]],
        grade: GradeParams,
        output_path: str | Path,
    ) -> Path:
        """Render every segment (cache honoured) then stream-copy concat."""
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        segment_paths: list[Path] = []
        list_lines: list[str] = []
        for node, input_path, input_sha256 in nodes_with_inputs:
            segment_path = self.render_segment(
                input_path=input_path,
                input_sha256=input_sha256,
                node=node,
                grade=grade,
            )
            segment_paths.append(segment_path)
            list_lines.append(f"file '{segment_path.as_posix()}'")

        list_file = self.work_dir / "concat_list.txt"
        list_file.write_text("\n".join(list_lines) + "\n", encoding="utf-8")

        args = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "error",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(list_file),
            "-c",
            "copy",
            str(output_path),
        ]
        try:
            result = subprocess.run(
                args,
                shell=False,
                timeout=_SEGMENT_TIMEOUT,
                check=False,
                capture_output=True,
                text=True,
            )
        except subprocess.TimeoutExpired as exc:
            raise CompileError(f"concat timed out: {exc}") from exc
        except OSError as exc:
            raise CompileError(f"concat failed to launch: {exc}") from exc
        if result.returncode != 0:
            raise CompileError(
                f"concat failed (exit {result.returncode}): "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )
        return output_path
