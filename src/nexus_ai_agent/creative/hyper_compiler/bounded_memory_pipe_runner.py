"""Streaming frame pipeline with a hard, enforced memory ceiling.

:class:`BoundedMemoryPipeRunner` consumes an ``Iterator`` of ``uint8`` frames and
never holds the whole clip in RAM.  Frames wait in a byte-capped ring buffer
(:class:`RingBufferFrameStore`); when the producer outruns the consumer the
runner applies *backpressure* — it drains the buffer instead of growing it — and
a single frame that cannot fit is refused with
:class:`MemoryBudgetExceededError`.

Processed frames are written straight to a file sink in fixed-size chunks, so
the peak resident payload is bounded by the ring buffer, never by the clip
length; a ``bytearray`` holding the whole timeline is never built.  Every frame
is shape- and dtype-checked against its :class:`FramePipelineSpec` before use,
and a closed trim range must yield exactly its expected frame count.

Two execution modes, both ending in an immutable
:class:`CompileExecutionReceipt`:

* ``deterministic_cpu_mux`` — combine, tone-map, dissolve and trim purely with
  NumPy and pack the result into the deterministic ``.nagarmux`` container
  (magic + geometry + frame digest + payload).  Byte-identical across runs, so
  it works in environments with no FFmpeg.
* ``ffmpeg_subprocess_pipe`` — drive ``ffmpeg`` through a real
  :class:`subprocess.Popen` pipe: frames are written to ``stdin`` one at a time
  and the result is drained from ``stdout`` in fixed-size chunks, so the raw
  frames are never collected in memory as a single blob.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import math
import os
import struct
import subprocess
import tempfile
import threading
import time
from collections import deque
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import BinaryIO, Protocol, cast

import numpy as np
from numpy.typing import NDArray

from nexus_ai_agent.creative.hyper_compiler.errors import (
    MemoryBudgetExceededError,
    PipeExecutionError,
)
from nexus_ai_agent.creative.hyper_compiler.ffmpeg_filter_graph_compiler import (
    ColorGradeLutRgbOp,
    CompiledFilterGraph,
    FFmpegFilterGraphCompiler,
    FilterOp,
    GraphInput,
    StreamKind,
    TrimOp,
    TrimStartOp,
)

DEFAULT_MAX_BUFFER_BYTES = 32 * 1024 * 1024
_MUX_MAGIC = b"NAGARMUX1"
_MUX_HEADER_LEN = len(_MUX_MAGIC) + 4 + 4 + 8 + 8 + 32
_CHUNK_BYTES = 64 * 1024
#: How long to wait for a wedged FFmpeg child to die before giving up.
_KILL_TIMEOUT_SECONDS = 5.0
#: Per-pixel widths of the intermediate buffers the CPU path allocates.
_UINT8_BYTES_PER_PIXEL = 3
_FLOAT64_BYTES_PER_PIXEL = 8

Frame = NDArray[np.uint8]


class ByteSink(Protocol):
    """Anything bytes can be written to (a file handle or an in-memory buffer)."""

    def write(self, data: bytes, /) -> int: ...


class MuxMode(str, Enum):
    DETERMINISTIC_CPU_MUX = "deterministic_cpu_mux"
    FFMPEG_SUBPROCESS_PIPE = "ffmpeg_subprocess_pipe"


def _require_int(value: object, *, what: str) -> int:
    """Strict integer check: a ``bool`` or a ``float`` is refused."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise PipeExecutionError(f"{what} must be an int, got {type(value).__name__}")
    return value


@dataclass(frozen=True)
class FramePipelineSpec:
    """Deterministic, strictly validated description of the frame pipeline."""

    width: int
    height: int
    fps: float
    brightness: float = 0.0
    contrast: float = 1.0
    dissolve: float = 0.0
    trim_start: int = 0
    trim_end: int | None = None

    def __post_init__(self) -> None:
        _require_int(self.width, what="width")
        _require_int(self.height, what="height")
        _require_int(self.trim_start, what="trim_start")
        if self.trim_end is not None:
            _require_int(self.trim_end, what="trim_end")
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

    @property
    def frame_shape(self) -> tuple[int, int, int]:
        return (self.height, self.width, 3)

    @property
    def expected_frame_count(self) -> int | None:
        """Frames the pipeline must emit when the trim range is fully closed."""
        if self.trim_end is None:
            return None
        return self.trim_end - self.trim_start

    def fingerprint(self) -> str:
        """Canonical SHA-256 of every knob that changes the rendered pixels.

        Folded into the segment cache key so that flipping a single setting (for
        example ``brightness``) invalidates exactly the affected segments.
        """
        payload = "|".join(
            (
                f"w={self.width}",
                f"h={self.height}",
                f"fps={self.fps!r}",
                f"b={self.brightness!r}",
                f"c={self.contrast!r}",
                f"d={self.dissolve!r}",
                f"ts={self.trim_start}",
                f"te={self.trim_end!r}",
            )
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


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
    """Result of streaming frame iterables through the pipeline.

    The container is *not* carried in memory: ``container_path`` points at the
    file it was written to (when the caller did not supply its own sink).
    """

    output_digest: str
    body_digest: str
    body_size: int
    container_size: int
    frames_processed: int
    peak_buffer_bytes: int
    max_buffer_bytes: int
    mode: MuxMode
    container_path: Path | None = None


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
    """Deterministic brightness/contrast mapping (``brightness`` in [-1, 1]).

    The work is done *in place* on a single buffer so at most one floating-point
    temporary and one ``uint8`` output coexist with the input frame (instead of
    the several simultaneous temporaries a chained-expression form would
    allocate).  This keeps the peak in step with the working-set budget in
    :func:`cpu_working_set_bytes`.

    The arithmetic is byte-identical to
    ``clip((f - 128) * contrast + 128 + brightness*255, 0, 255)`` evaluated in
    double precision — the same precision FFmpeg uses for the ``lutrgb``
    expression — and truncates to ``uint8`` exactly as FFmpeg does.  Using
    ``float32`` here instead would disagree with FFmpeg on the handful of pixels
    that straddle an integer boundary (the differential test pins this).
    """
    buf = frame.astype(np.float64, copy=True)
    np.subtract(buf, 128.0, out=buf)
    np.multiply(buf, float(contrast), out=buf)
    np.add(buf, 128.0, out=buf)
    np.add(buf, float(brightness) * 255.0, out=buf)
    np.clip(buf, 0.0, 255.0, out=buf)
    return buf.astype(np.uint8)


def dissolve(first: Frame, second: Frame, alpha: float) -> Frame:
    """Linear temporal crossfade: ``(1-a)*first + a*second`` (``a`` in [0, 1])."""
    if first.shape != second.shape:
        raise PipeExecutionError(f"dissolve shape mismatch: {first.shape} vs {second.shape}")
    blended = first.astype(np.float64) * (1.0 - alpha) + second.astype(np.float64) * alpha
    return np.clip(blended, 0.0, 255.0).astype(np.uint8)


def frame_bytes(spec: FramePipelineSpec) -> int:
    """Raw byte size of one ``rgb24`` frame at ``spec``'s geometry."""
    return spec.width * spec.height * 3


#: Errnos that only mean "this filesystem cannot fsync a directory"; they are
#: benign and ignored.  Any *other* errno (e.g. ``EIO``, ``ENOSPC``) is a real
#: durability failure and must propagate — a rename we cannot prove durable must
#: never be reported as a successful write.
_FSYNC_UNSUPPORTED_ERRNOS = frozenset(
    {
        errno.EINVAL,
        errno.EBADF,
        errno.ENOTSUP,
        errno.EACCES,
        errno.EPERM,
        errno.ENOSYS,
    }
)


def _fsync_directory(path: Path) -> None:
    """``fsync`` a directory so a rename is durable (fail-closed on real errors).

    Filesystems that cannot fsync a directory (or a platform that refuses to open
    one for reading) raise a benign errno listed in
    :data:`_FSYNC_UNSUPPORTED_ERRNOS`, which is ignored.  A genuine durability
    failure — ``EIO``, ``ENOSPC`` — is re-raised so the caller never believes a
    non-durable rename succeeded.
    """
    try:
        dir_fd = os.open(str(path), os.O_RDONLY)
    except OSError as exc:
        if exc.errno in _FSYNC_UNSUPPORTED_ERRNOS:
            return
        raise
    try:
        os.fsync(dir_fd)
    except OSError as exc:
        if exc.errno not in _FSYNC_UNSUPPORTED_ERRNOS:
            raise
    finally:
        os.close(dir_fd)


def cpu_working_set_bytes(spec: FramePipelineSpec) -> int:
    """Transient bytes the CPU path holds *outside* the ring buffer, per frame.

    While one frame is being consumed the CPU path holds, simultaneously: the
    input ``uint8`` frame, a ``float64`` tone-map temporary, the ``uint8`` tone
    output and the packed ``uint8`` body bytes.  ``dissolve`` additionally
    retains the previous ``uint8`` frame and two ``float64`` blend temporaries.
    Counting these is what keeps ``max_buffer_bytes`` an honest *total* ceiling
    rather than a ceiling on the ring buffer alone.
    """
    pixels = spec.width * spec.height
    per_pixel = 3 * _UINT8_BYTES_PER_PIXEL + _FLOAT64_BYTES_PER_PIXEL
    if spec.dissolve > 0.0:
        per_pixel += _UINT8_BYTES_PER_PIXEL + 2 * _FLOAT64_BYTES_PER_PIXEL
    return pixels * per_pixel


class BoundedMemoryPipeRunner:
    """Stream frames through a bounded buffer into a deterministic container."""

    def __init__(
        self,
        *,
        max_buffer_bytes: int = DEFAULT_MAX_BUFFER_BYTES,
        ffmpeg_bin: str = "ffmpeg",
        timeout_seconds: float = 30.0,
    ) -> None:
        if max_buffer_bytes <= 0:
            raise MemoryBudgetExceededError(f"max_buffer_bytes must be > 0, got {max_buffer_bytes}")
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0.0:
            raise PipeExecutionError(
                f"timeout_seconds must be finite and > 0, got {timeout_seconds!r}"
            )
        self.max_buffer_bytes = max_buffer_bytes
        self.ffmpeg_bin = ffmpeg_bin
        self.timeout_seconds = timeout_seconds

    # -- validation & CPU frame math ---------------------------------------
    def validate_frame(self, frame: Frame, spec: FramePipelineSpec) -> None:
        """Refuse a frame whose shape or dtype disagrees with ``spec``."""
        if frame.ndim != 3 or frame.shape != spec.frame_shape:
            raise PipeExecutionError(f"frame shape {frame.shape} does not match {spec.frame_shape}")
        if frame.dtype != np.uint8:
            raise PipeExecutionError(f"frame dtype must be uint8, got {frame.dtype}")

    def _process_frame(self, frame: Frame, spec: FramePipelineSpec, prev: Frame | None) -> Frame:
        out = apply_tone(frame, brightness=spec.brightness, contrast=spec.contrast)
        if spec.dissolve > 0.0 and prev is not None:
            out = dissolve(prev, out, spec.dissolve)
        return out

    def _in_trim_range(self, index: int, spec: FramePipelineSpec) -> bool:
        if index < spec.trim_start:
            return False
        return spec.trim_end is None or index < spec.trim_end

    def _ring_capacity(self, spec: FramePipelineSpec) -> int:
        """Ring-buffer bytes available once the CPU working set is reserved.

        ``max_buffer_bytes`` is a ceiling on the *total* resident payload, so the
        transient buffers the CPU path holds while a frame is being processed
        (``float32`` tone-map temporaries, the retained ``dissolve`` frame and the
        packed output) are subtracted before any frame may be buffered.
        """
        reserve = cpu_working_set_bytes(spec)
        capacity = self.max_buffer_bytes - reserve
        if capacity < frame_bytes(spec):
            raise MemoryBudgetExceededError(
                f"max_buffer_bytes={self.max_buffer_bytes} cannot hold the CPU working "
                f"set ({reserve} B) plus one frame ({frame_bytes(spec)} B)"
            )
        return capacity

    def _stream_body(
        self,
        segments: Iterable[Iterable[Frame]],
        spec: FramePipelineSpec,
        body_handle: ByteSink,
    ) -> tuple[int, str, int]:
        """Stream processed frames into ``body_handle``; return (count, digest, peak).

        Frames leave the FIFO in arrival order, so the *consumed* counter is the
        source frame index used for the trim range — no per-frame index needs to
        be buffered and no frame is retained once written.  The ring buffer is
        capped below ``max_buffer_bytes`` so the resident payload plus the CPU
        working set can never exceed the ceiling.
        """
        store = RingBufferFrameStore(self._ring_capacity(spec))
        body_hasher = hashlib.sha256()
        processed = 0
        consumed_index = 0
        prev: Frame | None = None

        def consume(frame: Frame) -> None:
            nonlocal processed, prev, consumed_index
            self.validate_frame(frame, spec)
            index = consumed_index
            consumed_index += 1
            if not self._in_trim_range(index, spec):
                return
            out = self._process_frame(frame, spec, prev)
            prev = out
            raw = out.tobytes()
            body_handle.write(raw)
            body_hasher.update(raw)
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

        expected = spec.expected_frame_count
        if expected is not None and processed != expected:
            raise PipeExecutionError(
                f"pipeline emitted {processed} frames, expected {expected} "
                f"(trim range {spec.trim_start}..{spec.trim_end})"
            )
        peak = store.peak_bytes + cpu_working_set_bytes(spec)
        return processed, body_hasher.hexdigest(), peak

    # -- deterministic CPU mux ---------------------------------------------
    def stream_many(
        self,
        segments: Iterable[Iterable[Frame]],
        spec: FramePipelineSpec,
        *,
        sink: ByteSink | None = None,
    ) -> StreamOutcome:
        """Stream frame iterables under one shared ceiling into a container.

        The frame body is buffered on disk (never in RAM), then the mux header
        and body are written to ``sink`` (or to a fresh temporary file whose path
        is returned on the outcome) in fixed-size chunks.
        """
        body_fd, body_name = tempfile.mkstemp(suffix=".nagarmux.body")
        body_path = Path(body_name)
        try:
            with os.fdopen(body_fd, "wb") as body_handle:
                count, body_digest, peak = self._stream_body(segments, spec, body_handle)
            header = (
                _MUX_MAGIC
                + struct.pack(">II", spec.width, spec.height)
                + struct.pack(">d", spec.fps)
                + struct.pack(">Q", count)
                + bytes.fromhex(body_digest)
            )
            container_path: Path | None = None
            owned_handle: BinaryIO | None = None
            out_handle: ByteSink
            if sink is None:
                out_fd, out_name = tempfile.mkstemp(suffix=".nagarmux")
                container_path = Path(out_name)
                owned_handle = os.fdopen(out_fd, "wb")
                out_handle = owned_handle
            else:
                out_handle = sink
            container_hasher = hashlib.sha256()
            try:
                out_handle.write(header)
                container_hasher.update(header)
                with body_path.open("rb") as body_reader:
                    while True:
                        chunk = body_reader.read(_CHUNK_BYTES)
                        if not chunk:
                            break
                        out_handle.write(chunk)
                        container_hasher.update(chunk)
            finally:
                if owned_handle is not None:
                    owned_handle.flush()
                    os.fsync(owned_handle.fileno())
                    owned_handle.close()
            body_size = count * spec.width * spec.height * 3
            return StreamOutcome(
                output_digest=container_hasher.hexdigest(),
                body_digest=body_digest,
                body_size=body_size,
                container_size=_MUX_HEADER_LEN + body_size,
                frames_processed=count,
                peak_buffer_bytes=peak,
                max_buffer_bytes=self.max_buffer_bytes,
                mode=MuxMode.DETERMINISTIC_CPU_MUX,
                container_path=container_path,
            )
        finally:
            body_path.unlink(missing_ok=True)

    def stream(
        self,
        frames: Iterator[Frame],
        spec: FramePipelineSpec,
        *,
        sink: ByteSink | None = None,
    ) -> StreamOutcome:
        """Stream a single frame iterator under the memory ceiling."""
        return self.stream_many([frames], spec, sink=sink)

    def stream_to_path(
        self,
        segments: Iterable[Iterable[Frame]],
        spec: FramePipelineSpec,
        path: Path,
    ) -> StreamOutcome:
        """Stream a container atomically to ``path`` (``.tmp`` → ``fsync`` → ``replace``)."""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".nagarmux.tmp")
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(tmp_fd, "wb") as handle:
                outcome = self.stream_many(segments, spec, sink=handle)
                # Durability: the bytes must be on disk before the rename, so a
                # crash can never leave a renamed-but-empty container behind.
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, path)
            # The rename itself must be durable too: fsync the parent directory so
            # the new directory entry survives a crash (matches segment_cas_dag).
            _fsync_directory(path.parent)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise
        return outcome

    # -- ffmpeg subprocess pipe --------------------------------------------
    def build_ffmpeg_command(self, spec: FramePipelineSpec) -> list[str]:
        """Assemble the ``-f rawvideo -pix_fmt rgb24`` pipe argv (no shell).

        The filter chain mirrors the CPU engine exactly: a closed trim becomes
        ``trim=start_frame:s``/``end_frame``, an open-ended ``trim_start`` becomes
        ``trim=start_frame=s`` (never dropped), and the tone map is ``lutrgb``
        (RGB-domain, so it reproduces the CPU arithmetic instead of ``eq``'s
        YUV round-trip).

        A ``dissolve`` is refused: FFmpeg's ``tblend`` aligns and blends frames
        differently from the CPU engine's per-frame ``(1-a)*prev + a*cur`` (its
        first output pair is duplicated), so there is no byte-equivalent graph.
        Rather than let one knob produce two different results on the two engines,
        the FFmpeg path fails closed and the caller must use the CPU mux.
        """
        if spec.dissolve > 0.0:
            raise PipeExecutionError(
                "dissolve is not supported on the ffmpeg_subprocess_pipe engine "
                "(no byte-equivalent filter graph); use deterministic_cpu_mux"
            )
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
        elif spec.trim_start > 0:
            ops.append(TrimStartOp(in_pad="v0", out_pad="v1", start_frame=spec.trim_start))
            eq_in = "v1"
        ops.append(
            ColorGradeLutRgbOp(
                in_pad=eq_in,
                out_pad="vout",
                brightness=spec.brightness,
                contrast=spec.contrast,
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

    def run_ffmpeg_stream(
        self,
        segments: Iterable[Iterable[Frame]],
        spec: FramePipelineSpec,
        *,
        sink: ByteSink | None = None,
    ) -> StreamOutcome:
        """Drive ``ffmpeg`` over a real pipe, frame-in / chunk-out (fail-closed).

        Frames are written to the process ``stdin`` one at a time from a writer
        thread (so a full ``stdout`` buffer cannot deadlock us) while ``stdout``
        and ``stderr`` are drained concurrently in fixed-size chunks.  The raw
        frames are never collected in memory as a single blob.

        The in-flight resident bytes (the frame being written plus the output
        chunk being drained) are measured and checked against ``max_buffer_bytes``,
        so a frame too large for the budget is refused with
        :class:`MemoryBudgetExceededError`.  A child that makes no progress for
        ``timeout_seconds`` is killed and the call fails with
        :class:`PipeExecutionError`; a wedged or failing child is always killed
        and reaped in ``finally``, so no FFmpeg process outlives the call.
        """
        return self._run_ffmpeg_pipe(self.build_ffmpeg_command(spec), segments, spec, sink)

    def _run_ffmpeg_pipe(
        self,
        command: Sequence[str],
        segments: Iterable[Iterable[Frame]],
        spec: FramePipelineSpec,
        sink: ByteSink | None,
    ) -> StreamOutcome:
        """Spawn ``command`` and pump frames through it under the memory ceiling.

        Split out from :meth:`run_ffmpeg_stream` so the pipe watchdog can be
        exercised with a real, deliberately-wedged child process.
        """
        try:
            proc: subprocess.Popen[bytes] = subprocess.Popen(
                list(command),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise PipeExecutionError(f"ffmpeg pipe failed to start: {exc}") from exc
        stdin = proc.stdin
        stdout = proc.stdout
        stderr_pipe = proc.stderr
        assert stdin is not None and stdout is not None and stderr_pipe is not None
        writer_error: list[BaseException] = []
        # F3b: track the *measured* live in-flight bytes (the frame the writer
        # holds plus the chunk the reader holds), starting from zero, instead of
        # seeding the peak with a static working-set floor.  ``live_frame`` and
        # ``peak_inflight`` are written by the writer thread and read by the
        # reader thread, so *every* access is guarded by ``state_lock`` — without
        # it the two threads could race and the measured peak would be a lie.
        peak_inflight = [0]
        live_frame = [0]
        state_lock = threading.Lock()
        # F2: a wedged child that never makes progress must not block forever.
        timed_out = [False]
        last_activity = [time.monotonic()]
        activity_lock = threading.Lock()
        finished = threading.Event()

        def mark_activity() -> None:
            with activity_lock:
                last_activity[0] = time.monotonic()

        def watchdog() -> None:
            poll = max(min(self.timeout_seconds, 0.1), 0.01)
            while not finished.wait(timeout=poll):
                with activity_lock:
                    idle = time.monotonic() - last_activity[0]
                if idle > self.timeout_seconds:
                    timed_out[0] = True
                    # Killing an already-dead child is a no-op (suppressed).
                    with contextlib.suppress(OSError):
                        proc.kill()
                    return

        def feed() -> None:
            try:
                for segment in segments:
                    for frame in segment:
                        self.validate_frame(frame, spec)
                        # F3b: at the instant ``tobytes`` runs, BOTH the source
                        # ndarray and its freshly packed bytes are resident, so the
                        # honest in-flight footprint is ``frame.nbytes + len(raw)``
                        # (plus the chunk the reader holds while draining).  Counting
                        # only ``len(raw)`` would understate the peak by a whole
                        # frame and could let a frame slip past the ceiling.
                        frame_bytes_held = int(frame.nbytes)
                        raw = frame.tobytes()
                        held = frame_bytes_held + len(raw)
                        inflight = held + _CHUNK_BYTES
                        if inflight > self.max_buffer_bytes:
                            raise MemoryBudgetExceededError(
                                f"in-flight frame {frame_bytes_held} B + packed {len(raw)} B "
                                f"+ read chunk {_CHUNK_BYTES} B exceeds max_buffer_bytes "
                                f"{self.max_buffer_bytes}"
                            )
                        with state_lock:
                            live_frame[0] = held
                            peak_inflight[0] = max(peak_inflight[0], held)
                        stdin.write(raw)
                        with state_lock:
                            live_frame[0] = 0
                        mark_activity()
            except BaseException as exc:  # noqa: BLE001 - relayed to the main thread
                writer_error.append(exc)
            finally:
                with state_lock:
                    live_frame[0] = 0
                try:
                    stdin.close()
                except OSError:
                    pass

        def drain_stderr() -> None:
            while stderr_pipe.read(_CHUNK_BYTES):
                mark_activity()

        writer = threading.Thread(target=feed, daemon=True)
        stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
        watchdog_thread = threading.Thread(target=watchdog, daemon=True)
        # A short, bounded window for the *daemon* writer to notice a closed pipe.
        # The writer can be wedged inside a caller-supplied generator that never
        # returns, so every join on it must be bounded by this — never by the
        # longer child-kill window, which would otherwise stall the caller.
        writer_join = max(min(self.timeout_seconds, 5.0), 0.01)
        writer.start()
        stderr_thread.start()
        watchdog_thread.start()
        container_hasher = hashlib.sha256()
        total = 0
        try:
            while True:
                chunk = stdout.read(_CHUNK_BYTES)
                if not chunk:
                    break
                # The writer's live frame and this chunk are resident together.
                with state_lock:
                    peak_inflight[0] = max(peak_inflight[0], live_frame[0] + len(chunk))
                if sink is not None:
                    sink.write(chunk)
                container_hasher.update(chunk)
                total += len(chunk)
                mark_activity()
            # F2: the writer must never block the caller forever.  Closing stdin
            # first unblocks a writer parked on ``stdin.write`` (it raises
            # ``BrokenPipeError``/``ValueError`` and the thread exits), then the
            # join is bounded.
            with contextlib.suppress(OSError, ValueError):
                stdin.close()
            writer.join(timeout=writer_join)
            if writer.is_alive():
                # A producer that ignores the closed pipe (e.g. a generator
                # blocked forever inside its own code) cannot be unblocked by
                # EOF alone.  Kill the child *now* so no subprocess outlives the
                # call, give the writer one more bounded window to observe the
                # broken pipe, then fail hard — never hang the caller.  The
                # writer thread is a daemon, so even a producer wedged outside
                # ``stdin.write`` cannot keep the process alive.
                if proc.poll() is None:
                    with contextlib.suppress(OSError):
                        proc.kill()
                    with contextlib.suppress(subprocess.TimeoutExpired):
                        proc.wait(timeout=_KILL_TIMEOUT_SECONDS)
                writer.join(timeout=writer_join)
                raise PipeExecutionError("writer thread timed out")
            stderr_thread.join()
            returncode = proc.wait()
        finally:
            finished.set()
            # Never let a wedged child or a stuck writer outlive the call.
            if proc.poll() is None:
                proc.kill()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=_KILL_TIMEOUT_SECONDS)
            with contextlib.suppress(OSError, ValueError):
                stdin.close()
            # Bounded by ``writer_join`` (not the longer child-kill window): a
            # writer wedged in a caller generator must not stall the caller.
            writer.join(timeout=writer_join)
            stderr_thread.join(timeout=_KILL_TIMEOUT_SECONDS)
            watchdog_thread.join(timeout=_KILL_TIMEOUT_SECONDS)
            for pipe in (stdout, stderr_pipe):
                try:
                    pipe.close()
                except OSError:
                    pass
        if timed_out[0]:
            raise PipeExecutionError("FFmpeg subprocess timed out")
        if writer_error:
            writer_exc = writer_error[0]
            if isinstance(writer_exc, MemoryBudgetExceededError):
                raise writer_exc
            if isinstance(writer_exc, PipeExecutionError):
                raise writer_exc
            raise PipeExecutionError(f"ffmpeg pipe writer failed: {writer_exc}") from writer_exc
        if returncode != 0:
            raise PipeExecutionError(f"ffmpeg pipe exited {returncode}")
        frame_size = frame_bytes(spec)
        if total % frame_size != 0:
            raise PipeExecutionError(
                f"ffmpeg emitted {total} bytes, not a whole number of {frame_size}-byte frames"
            )
        emitted_frames = total // frame_size
        expected = spec.expected_frame_count
        if expected is not None and emitted_frames != expected:
            raise PipeExecutionError(
                f"ffmpeg emitted {emitted_frames} frames, expected {expected} "
                f"(trim range {spec.trim_start}..{spec.trim_end})"
            )
        return StreamOutcome(
            output_digest=container_hasher.hexdigest(),
            body_digest=container_hasher.hexdigest(),
            body_size=total,
            container_size=total,
            frames_processed=emitted_frames,
            peak_buffer_bytes=peak_inflight[0],
            max_buffer_bytes=self.max_buffer_bytes,
            mode=MuxMode.FFMPEG_SUBPROCESS_PIPE,
            container_path=None,
        )

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
