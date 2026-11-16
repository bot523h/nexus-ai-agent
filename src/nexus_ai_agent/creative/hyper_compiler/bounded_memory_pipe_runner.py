"""Streaming frame pipeline with a hard, enforced memory ceiling.

:class:`BoundedMemoryPipeRunner` consumes an ``Iterator`` of ``uint8`` frames and
never holds the whole clip in RAM.  Frames wait in a byte-capped ring buffer
(:class:`RingBufferFrameStore`); when the producer outruns the consumer the
runner applies *backpressure* — it drains the buffer instead of growing it — and
a single frame that cannot fit is refused with
:class:`MemoryBudgetExceededError`.

Two execution modes:

* ``deterministic_cpu_mux`` — combine, tone-map, dissolve and trim purely with
  NumPy and pack the result into the deterministic ``.nagarseg`` container
  (magic + geometry + frame digest + payload).  Byte-identical across runs, so
  it works in environments with no FFmpeg.
* ``ffmpeg_subprocess_pipe`` — assemble the full ``ffmpeg`` argv for a
  ``-f rawvideo -pix_fmt rgb24`` pipe (list argv, never ``shell=True``).

Both modes end in an immutable :class:`CompileExecutionReceipt`.
"""

from __future__ import annotations

import hashlib
import math
import struct
import subprocess
from collections import deque
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from enum import Enum
from typing import cast

import numpy as np
from numpy.typing import NDArray

from nexus_ai_agent.creative.hyper_compiler.errors import (
    MemoryBudgetExceededError,
    PipeExecutionError,
)
from nexus_ai_agent.creative.hyper_compiler.ffmpeg_filter_graph_compiler import (
    ColorGradeEqOp,
    CompiledFilterGraph,
    FFmpegFilterGraphCompiler,
    FilterOp,
    GraphInput,
    StreamKind,
    TrimOp,
)

DEFAULT_MAX_BUFFER_BYTES = 32 * 1024 * 1024
_MUX_MAGIC = b"NAGARMUX1"

Frame = NDArray[np.uint8]


class MuxMode(str, Enum):
    DETERMINISTIC_CPU_MUX = "deterministic_cpu_mux"
    FFMPEG_SUBPROCESS_PIPE = "ffmpeg_subprocess_pipe"


@dataclass(frozen=True)
class FramePipelineSpec:
    """Deterministic, validated description of the frame pipeline."""

    width: int
    height: int
    fps: float
    brightness: float = 0.0
    contrast: float = 1.0
    dissolve: float = 0.0
    trim_start: int = 0
    trim_end: int | None = None

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise PipeExecutionError(f"invalid frame size {self.width}x{self.height}")
        if not math.isfinite(self.fps) or self.fps <= 0.0:
            raise PipeExecutionError(f"fps must be finite and > 0, got {self.fps!r}")
        if not math.isfinite(self.brightness) or not -1.0 <= self.brightness <= 1.0:
            raise PipeExecutionError(f"brightness out of range: {self.brightness!r}")
        if not math.isfinite(self.contrast) or not 0.0 <= self.contrast <= 4.0:
            raise PipeExecutionError(f"contrast out of range: {self.contrast!r}")
        if not math.isfinite(self.dissolve) or not 0.0 <= self.dissolve <= 1.0:
            raise PipeExecutionError(f"dissolve out of range: {self.dissolve!r}")
        if self.trim_start < 0:
            raise PipeExecutionError(f"trim_start must be >= 0, got {self.trim_start}")
        if self.trim_end is not None and self.trim_end <= self.trim_start:
            raise PipeExecutionError("trim_end must be > trim_start")


@dataclass(frozen=True)
class CompileExecutionReceipt:
    """Immutable proof of one streaming execution."""

    timeline_merkle_root: str
    total_segments: int
    cache_hits: int
    rendered_segments: int
    cache_hit_ratio: float
    peak_buffer_bytes: int
    output_digest: str
    mode: str

    def as_dict(self) -> dict[str, object]:
        return {
            "timeline_merkle_root": self.timeline_merkle_root,
            "total_segments": self.total_segments,
            "cache_hits": self.cache_hits,
            "rendered_segments": self.rendered_segments,
            "cache_hit_ratio": self.cache_hit_ratio,
            "peak_buffer_bytes": self.peak_buffer_bytes,
            "output_digest": self.output_digest,
            "mode": self.mode,
        }


@dataclass(frozen=True)
class StreamOutcome:
    """Result of streaming frame iterators through the pipeline."""

    container: bytes
    output_digest: str
    frames_processed: int
    peak_buffer_bytes: int
    max_buffer_bytes: int


class RingBufferFrameStore:
    """Byte-capped FIFO of frames with explicit backpressure semantics."""

    def __init__(self, capacity_bytes: int) -> None:
        if capacity_bytes <= 0:
            raise MemoryBudgetExceededError(f"capacity must be > 0, got {capacity_bytes}")
        self.capacity_bytes = capacity_bytes
        self._frames: deque[Frame] = deque()
        self._bytes = 0
        self.peak_bytes = 0

    @property
    def buffered_bytes(self) -> int:
        return self._bytes

    def __len__(self) -> int:
        return len(self._frames)

    def try_push(self, frame: Frame) -> bool:
        """Enqueue ``frame``; ``False`` for backpressure, raise if oversized."""
        nbytes = int(frame.nbytes)
        if nbytes > self.capacity_bytes:
            raise MemoryBudgetExceededError(
                f"single frame of {nbytes} bytes exceeds capacity {self.capacity_bytes}"
            )
        if self._bytes + nbytes > self.capacity_bytes:
            return False
        self._frames.append(frame)
        self._bytes += nbytes
        self.peak_bytes = max(self.peak_bytes, self._bytes)
        return True

    def pop(self) -> Frame | None:
        if not self._frames:
            return None
        frame = self._frames.popleft()
        self._bytes -= int(frame.nbytes)
        return frame


def apply_tone(frame: Frame, *, brightness: float, contrast: float) -> Frame:
    """Deterministic brightness/contrast mapping (``brightness`` in [-1, 1])."""
    data = frame.astype(np.float32)
    data = (data - 128.0) * contrast + 128.0 + brightness * 255.0
    return np.clip(data, 0.0, 255.0).astype(np.uint8)


def dissolve(first: Frame, second: Frame, alpha: float) -> Frame:
    """Linear temporal crossfade: ``(1-a)*first + a*second`` (``a`` in [0, 1])."""
    if first.shape != second.shape:
        raise PipeExecutionError(f"dissolve shape mismatch: {first.shape} vs {second.shape}")
    blended = first.astype(np.float32) * (1.0 - alpha) + second.astype(np.float32) * alpha
    return np.clip(blended, 0.0, 255.0).astype(np.uint8)


class BoundedMemoryPipeRunner:
    """Stream frames through a bounded buffer into a deterministic container."""

    def __init__(
        self,
        *,
        max_buffer_bytes: int = DEFAULT_MAX_BUFFER_BYTES,
        ffmpeg_bin: str = "ffmpeg",
    ) -> None:
        if max_buffer_bytes <= 0:
            raise MemoryBudgetExceededError(f"max_buffer_bytes must be > 0, got {max_buffer_bytes}")
        self.max_buffer_bytes = max_buffer_bytes
        self.ffmpeg_bin = ffmpeg_bin

    # -- deterministic CPU mux ---------------------------------------------
    def _process_frame(self, frame: Frame, spec: FramePipelineSpec, prev: Frame | None) -> Frame:
        out = apply_tone(frame, brightness=spec.brightness, contrast=spec.contrast)
        if spec.dissolve > 0.0 and prev is not None:
            out = dissolve(prev, out, spec.dissolve)
        return out

    def _in_trim_range(self, index: int, spec: FramePipelineSpec) -> bool:
        if index < spec.trim_start:
            return False
        return spec.trim_end is None or index < spec.trim_end

    def stream_many(
        self,
        segments: Iterable[Iterable[Frame]],
        spec: FramePipelineSpec,
    ) -> StreamOutcome:
        """Stream one-or-more frame iterables under a single shared ceiling.

        Frames leave the FIFO in arrival order, so the *consumed* counter is the
        source frame index used for the trim range — no per-frame index needs to
        be buffered.
        """
        store = RingBufferFrameStore(self.max_buffer_bytes)
        payload = bytearray()
        hasher = hashlib.sha256()
        processed = 0
        consumed_index = 0
        prev: Frame | None = None

        def consume(frame: Frame) -> None:
            nonlocal processed, prev, consumed_index
            index = consumed_index
            consumed_index += 1
            if not self._in_trim_range(index, spec):
                return
            out = self._process_frame(frame, spec, prev)
            prev = out
            raw = out.tobytes()
            hasher.update(raw)
            payload.extend(raw)
            processed += 1

        for segment in segments:
            for frame in segment:
                while not store.try_push(frame):
                    # try_push refuses only while the FIFO holds >= 1 frame, so a
                    # pop always succeeds here (the cast documents that invariant).
                    consume(cast("Frame", store.pop()))
        while True:
            buffered = store.pop()
            if buffered is None:
                break
            consume(buffered)

        header = (
            _MUX_MAGIC
            + struct.pack(">II", spec.width, spec.height)
            + struct.pack(">d", spec.fps)
            + struct.pack(">Q", processed)
            + hasher.digest()
        )
        container = header + bytes(payload)
        return StreamOutcome(
            container=container,
            output_digest=hashlib.sha256(container).hexdigest(),
            frames_processed=processed,
            peak_buffer_bytes=store.peak_bytes,
            max_buffer_bytes=self.max_buffer_bytes,
        )

    def stream(
        self,
        frames: Iterator[Frame],
        spec: FramePipelineSpec,
    ) -> StreamOutcome:
        """Stream a single frame iterator under the memory ceiling."""
        return self.stream_many([frames], spec)

    # -- ffmpeg subprocess pipe --------------------------------------------
    def build_ffmpeg_command(self, spec: FramePipelineSpec) -> list[str]:
        """Assemble the ``-f rawvideo -pix_fmt rgb24`` pipe argv (no shell)."""
        ops: list[FilterOp] = []
        eq_in = "v0"
        if spec.trim_end is not None:
            ops.append(
                TrimOp(
                    in_pad="v0",
                    out_pad="v1",
                    start_frame=spec.trim_start,
                    end_frame=spec.trim_end,
                    fps=spec.fps,
                )
            )
            eq_in = "v1"
        ops.append(
            ColorGradeEqOp(
                in_pad=eq_in,
                out_pad="vout",
                brightness=spec.brightness,
                contrast=spec.contrast,
                saturation=1.0,
                gamma=1.0,
            )
        )
        compiler = FFmpegFilterGraphCompiler()
        graph: CompiledFilterGraph = compiler.compile(
            inputs=[GraphInput(label="v0", kind=StreamKind.VIDEO)],
            ops=ops,
            outputs=["vout"],
        )
        input_args = [
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-s",
            f"{spec.width}x{spec.height}",
            "-r",
            format(spec.fps, ".6f").rstrip("0").rstrip("."),
            "-i",
            "-",
        ]
        output_args = ["-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
        return graph.ffmpeg_argv(input_args, output_args, ffmpeg_bin=self.ffmpeg_bin)

    def run_ffmpeg_pipe(self, raw_frames: bytes, spec: FramePipelineSpec) -> bytes:
        """Execute the rawvideo pipe; fail closed when FFmpeg is unavailable."""
        command = self.build_ffmpeg_command(spec)
        try:
            completed = subprocess.run(
                command,
                input=raw_frames,
                capture_output=True,
                check=False,
                shell=False,
                timeout=600,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise PipeExecutionError(f"ffmpeg pipe failed to start: {exc}") from exc
        if completed.returncode != 0:
            detail = completed.stderr.decode("utf-8", "replace")[-400:]
            raise PipeExecutionError(f"ffmpeg pipe exited {completed.returncode}: {detail}")
        return completed.stdout

    # -- receipts -----------------------------------------------------------
    def build_receipt(
        self,
        *,
        timeline_merkle_root: str,
        total_segments: int,
        cache_hits: int,
        rendered_segments: int,
        peak_buffer_bytes: int,
        output_digest: str,
        mode: MuxMode,
    ) -> CompileExecutionReceipt:
        if total_segments < 0:
            raise PipeExecutionError("total_segments must be >= 0")
        ratio = 0.0 if total_segments == 0 else cache_hits / total_segments
        return CompileExecutionReceipt(
            timeline_merkle_root=timeline_merkle_root,
            total_segments=total_segments,
            cache_hits=cache_hits,
            rendered_segments=rendered_segments,
            cache_hit_ratio=ratio,
            peak_buffer_bytes=peak_buffer_bytes,
            output_digest=output_digest,
            mode=mode.value,
        )


__all__ = [
    "DEFAULT_MAX_BUFFER_BYTES",
    "BoundedMemoryPipeRunner",
    "CompileExecutionReceipt",
    "FramePipelineSpec",
    "MuxMode",
    "RingBufferFrameStore",
    "StreamOutcome",
    "apply_tone",
    "dissolve",
]
