"""Bounded-memory pipe runner tests: streaming, backpressure and receipts."""

from __future__ import annotations

import dataclasses
import hashlib
import io
import subprocess
from collections.abc import Iterator
from pathlib import Path

import numpy as np
import pytest
from numpy.typing import NDArray

from nexus_ai_agent.creative.hyper_compiler import bounded_memory_pipe_runner as bmpr
from nexus_ai_agent.creative.hyper_compiler.bounded_memory_pipe_runner import (
    DEFAULT_MAX_BUFFER_BYTES,
    BoundedMemoryPipeRunner,
    CompileExecutionReceipt,
    FramePipelineSpec,
    MuxMode,
    RingBufferFrameStore,
    apply_tone,
    dissolve,
)
from nexus_ai_agent.creative.hyper_compiler.errors import (
    MemoryBudgetExceededError,
    PipeExecutionError,
)

Frame = NDArray[np.uint8]


def frames(count: int, *, value: int = 120, size: int = 8) -> Iterator[Frame]:
    for _ in range(count):
        yield np.full((size, size, 3), value, dtype=np.uint8)


def spec(**kwargs: object) -> FramePipelineSpec:
    base: dict[str, object] = {"width": 8, "height": 8, "fps": 24.0}
    base.update(kwargs)
    return FramePipelineSpec(**base)  # type: ignore[arg-type]


# ── FramePipelineSpec validation ────────────────────────────────────────────
@pytest.mark.parametrize(
    "kwargs",
    [
        {"width": 0},
        {"height": -1},
        {"fps": 0.0},
        {"fps": float("nan")},
        {"brightness": 2.0},
        {"brightness": float("inf")},
        {"contrast": -0.1},
        {"contrast": float("nan")},
        {"dissolve": 1.5},
        {"dissolve": float("nan")},
        {"trim_start": -1},
        {"trim_start": 5, "trim_end": 5},
    ],
)
def test_spec_rejects_invalid(kwargs: dict[str, object]) -> None:
    with pytest.raises(PipeExecutionError):
        spec(**kwargs)


@pytest.mark.parametrize("field", ["width", "height", "trim_start", "trim_end"])
def test_spec_rejects_non_int_dimensions(field: str) -> None:
    # A bool or a float must be refused: only a true int is a frame dimension.
    bad: object = True if field != "trim_end" else 2.0
    kwargs: dict[str, object] = {field: bad}
    if field == "trim_end":
        kwargs["trim_start"] = 0
    with pytest.raises(PipeExecutionError):
        spec(**kwargs)


def test_spec_frame_shape_and_expected_count() -> None:
    s = spec(trim_start=2, trim_end=6)
    assert s.frame_shape == (8, 8, 3)
    assert s.expected_frame_count == 4
    assert spec().expected_frame_count is None


def test_spec_fingerprint_changes_with_brightness() -> None:
    assert spec().fingerprint() == spec().fingerprint()
    assert spec().fingerprint() != spec(brightness=0.2).fingerprint()


# ── pure helpers ────────────────────────────────────────────────────────────
def test_apply_tone_brightens() -> None:
    frame = np.full((2, 2, 3), 100, dtype=np.uint8)
    out = apply_tone(frame, brightness=0.2, contrast=1.0)
    assert int(out[0, 0, 0]) > 100


def test_apply_tone_clamps() -> None:
    frame = np.full((2, 2, 3), 250, dtype=np.uint8)
    out = apply_tone(frame, brightness=0.5, contrast=1.0)
    assert int(out.max()) == 255


def test_dissolve_blends() -> None:
    a = np.zeros((2, 2, 3), dtype=np.uint8)
    b = np.full((2, 2, 3), 200, dtype=np.uint8)
    out = dissolve(a, b, 0.5)
    assert int(out[0, 0, 0]) == 100


def test_dissolve_rejects_shape_mismatch() -> None:
    a = np.zeros((2, 2, 3), dtype=np.uint8)
    b = np.zeros((3, 3, 3), dtype=np.uint8)
    with pytest.raises(PipeExecutionError):
        dissolve(a, b, 0.5)


# ── RingBufferFrameStore ────────────────────────────────────────────────────
def test_ring_buffer_rejects_zero_capacity() -> None:
    with pytest.raises(MemoryBudgetExceededError):
        RingBufferFrameStore(0)


def test_ring_buffer_rejects_oversized_frame() -> None:
    store = RingBufferFrameStore(10)
    with pytest.raises(MemoryBudgetExceededError):
        store.try_push(np.zeros((4, 4, 3), dtype=np.uint8))


def test_ring_buffer_applies_backpressure() -> None:
    store = RingBufferFrameStore(48)
    frame = np.zeros((4, 4, 3), dtype=np.uint8)  # 48 bytes
    assert store.try_push(frame) is True
    assert store.try_push(frame) is False
    assert store.buffered_bytes == 48
    assert len(store) == 1


def test_ring_buffer_pop_tracks_bytes_and_peak() -> None:
    store = RingBufferFrameStore(96)
    frame = np.zeros((4, 4, 3), dtype=np.uint8)
    store.try_push(frame)
    store.try_push(frame)
    assert store.peak_bytes == 96
    assert store.pop() is not None
    assert store.buffered_bytes == 48
    assert store.pop() is not None
    assert store.pop() is None


# ── runner construction ─────────────────────────────────────────────────────
def test_runner_rejects_zero_budget() -> None:
    with pytest.raises(MemoryBudgetExceededError):
        BoundedMemoryPipeRunner(max_buffer_bytes=0)


def test_default_budget_is_32_mib() -> None:
    assert DEFAULT_MAX_BUFFER_BYTES == 32 * 1024 * 1024
    assert BoundedMemoryPipeRunner().max_buffer_bytes == DEFAULT_MAX_BUFFER_BYTES


# ── frame validation ────────────────────────────────────────────────────────
def test_validate_frame_rejects_wrong_shape() -> None:
    runner = BoundedMemoryPipeRunner()
    with pytest.raises(PipeExecutionError):
        runner.validate_frame(np.zeros((4, 4, 3), dtype=np.uint8), spec())


def test_validate_frame_rejects_wrong_dtype() -> None:
    runner = BoundedMemoryPipeRunner()
    with pytest.raises(PipeExecutionError):
        runner.validate_frame(np.zeros((8, 8, 3), dtype=np.float32), spec())


def test_stream_rejects_bad_frame_shape() -> None:
    runner = BoundedMemoryPipeRunner(max_buffer_bytes=1024 * 1024)
    with pytest.raises(PipeExecutionError):
        runner.stream(iter([np.zeros((4, 4, 3), dtype=np.uint8)]), spec())


# ── streaming CPU mux ───────────────────────────────────────────────────────
def test_stream_is_deterministic() -> None:
    runner = BoundedMemoryPipeRunner(max_buffer_bytes=1024 * 1024)
    first = runner.stream(frames(5), spec())
    second = runner.stream(frames(5), spec())
    assert first.output_digest == second.output_digest
    assert first.body_digest == second.body_digest
    assert first.container_size == second.container_size


def test_stream_body_is_written_to_file_not_memory() -> None:
    runner = BoundedMemoryPipeRunner(max_buffer_bytes=1024 * 1024)
    outcome = runner.stream(frames(3), spec())
    assert outcome.container_path is not None
    assert outcome.container_path.stat().st_size == outcome.container_size
    assert outcome.container_path.read_bytes().startswith(bmpr._MUX_MAGIC)
    outcome.container_path.unlink()


def test_stream_to_sink_avoids_tempfile() -> None:
    runner = BoundedMemoryPipeRunner(max_buffer_bytes=1024 * 1024)
    sink = io.BytesIO()
    outcome = runner.stream(frames(3), spec(), sink=sink)
    assert outcome.container_path is None
    assert len(sink.getvalue()) == outcome.container_size


def test_stream_to_path_cleans_temp_on_failure(tmp_path: Path) -> None:
    # A frame that cannot fit must fail closed and leave no temp file behind.
    runner = BoundedMemoryPipeRunner(max_buffer_bytes=64)
    target = tmp_path / "clip.nagarmux"
    with pytest.raises(MemoryBudgetExceededError):
        runner.stream_to_path([frames(1, size=8)], spec(), target)
    assert not target.exists()
    assert list(tmp_path.glob("*.tmp")) == []


def test_stream_to_path_is_atomic(tmp_path: Path) -> None:
    runner = BoundedMemoryPipeRunner(max_buffer_bytes=1024 * 1024)
    target = tmp_path / "clip.nagarmux"
    outcome = runner.stream_to_path([frames(3)], spec(), target)
    assert target.read_bytes().startswith(bmpr._MUX_MAGIC)
    assert list(tmp_path.glob("*.tmp")) == []
    assert outcome.container_size == target.stat().st_size


def test_stream_trims_frames() -> None:
    runner = BoundedMemoryPipeRunner(max_buffer_bytes=1024 * 1024)
    outcome = runner.stream(frames(10), spec(trim_start=2, trim_end=5))
    assert outcome.frames_processed == 3
    outcome.container_path.unlink()  # type: ignore[union-attr]


def test_stream_refuses_short_closed_trim() -> None:
    # A closed trim range that the source cannot satisfy must fail closed.
    runner = BoundedMemoryPipeRunner(max_buffer_bytes=1024 * 1024)
    with pytest.raises(PipeExecutionError):
        runner.stream(frames(3), spec(trim_start=0, trim_end=5))


def test_stream_open_ended_trim() -> None:
    runner = BoundedMemoryPipeRunner(max_buffer_bytes=1024 * 1024)
    outcome = runner.stream(frames(10), spec(trim_start=7))
    assert outcome.frames_processed == 3
    outcome.container_path.unlink()  # type: ignore[union-attr]


def test_stream_applies_dissolve() -> None:
    runner = BoundedMemoryPipeRunner(max_buffer_bytes=1024 * 1024)
    outcome = runner.stream(frames(4, value=50), spec(dissolve=0.5))
    assert outcome.frames_processed == 4
    outcome.container_path.unlink()  # type: ignore[union-attr]


def test_stream_peak_never_exceeds_budget() -> None:
    # 12 frames of 8x8x3 = 192 bytes each.  The budget must cover the CPU
    # working set plus the buffered frames, and the reported peak must include
    # that working set while still never crossing the budget.
    runner = BoundedMemoryPipeRunner(max_buffer_bytes=4096)
    outcome = runner.stream(frames(12), spec())
    assert outcome.peak_buffer_bytes <= outcome.max_buffer_bytes
    assert outcome.peak_buffer_bytes <= 4096
    assert outcome.frames_processed == 12
    outcome.container_path.unlink()  # type: ignore[union-attr]


def test_stream_refuses_frame_larger_than_budget() -> None:
    runner = BoundedMemoryPipeRunner(max_buffer_bytes=64)
    with pytest.raises(MemoryBudgetExceededError):
        runner.stream(frames(1, size=8), spec())


def test_stream_budget_must_cover_cpu_working_set() -> None:
    # P1-C: max_buffer_bytes is a *total* ceiling, so a budget that cannot hold
    # the CPU working set plus one frame is refused up front, not silently
    # exceeded by the transient float32/tone buffers.
    working_set = bmpr.cpu_working_set_bytes(spec())
    assert working_set > 0
    runner = BoundedMemoryPipeRunner(max_buffer_bytes=working_set)
    with pytest.raises(MemoryBudgetExceededError):
        runner.stream(frames(1), spec())


def test_cpu_working_set_grows_with_dissolve() -> None:
    plain = bmpr.cpu_working_set_bytes(spec())
    blended = bmpr.cpu_working_set_bytes(spec(dissolve=0.5))
    assert blended > plain


def test_stream_peak_accounts_for_working_set() -> None:
    # The reported peak includes the CPU working set, so it is a true ceiling
    # on the resident payload rather than on the ring buffer alone.
    runner = BoundedMemoryPipeRunner(max_buffer_bytes=8192)
    outcome = runner.stream(frames(3), spec())
    assert outcome.peak_buffer_bytes >= bmpr.cpu_working_set_bytes(spec())
    assert outcome.peak_buffer_bytes <= outcome.max_buffer_bytes
    outcome.container_path.unlink()  # type: ignore[union-attr]


def test_stream_applies_backpressure_when_ring_is_small() -> None:
    # A ring that holds only ~2 frames forces the producer to drain on push, so
    # the backpressure branch (consume-on-full) is exercised and the peak stays
    # inside the budget.
    working_set = bmpr.cpu_working_set_bytes(spec())
    runner = BoundedMemoryPipeRunner(max_buffer_bytes=working_set + 192 * 2)
    outcome = runner.stream(frames(8), spec())
    assert outcome.frames_processed == 8
    assert outcome.peak_buffer_bytes <= outcome.max_buffer_bytes
    outcome.container_path.unlink()  # type: ignore[union-attr]


def test_stream_many_across_segments() -> None:
    runner = BoundedMemoryPipeRunner(max_buffer_bytes=4096)
    outcome = runner.stream_many([frames(5), frames(5)], spec())
    assert outcome.frames_processed == 10
    assert outcome.peak_buffer_bytes <= outcome.max_buffer_bytes
    outcome.container_path.unlink()  # type: ignore[union-attr]


def test_stream_many_empty_is_allowed() -> None:
    runner = BoundedMemoryPipeRunner(max_buffer_bytes=1024)
    outcome = runner.stream_many([], spec())
    assert outcome.frames_processed == 0
    outcome.container_path.unlink()  # type: ignore[union-attr]


def test_stream_many_applies_trim_across_segments() -> None:
    runner = BoundedMemoryPipeRunner(max_buffer_bytes=1024 * 1024)
    outcome = runner.stream_many([frames(4), frames(4)], spec(trim_start=2, trim_end=6))
    assert outcome.frames_processed == 4
    outcome.container_path.unlink()  # type: ignore[union-attr]


# ── ffmpeg subprocess pipe ──────────────────────────────────────────────────
def test_build_ffmpeg_command_with_trim() -> None:
    runner = BoundedMemoryPipeRunner(ffmpeg_bin="ffmpeg")
    command = runner.build_ffmpeg_command(spec(trim_start=0, trim_end=10))
    assert command[0] == "ffmpeg"
    assert "-filter_complex" in command
    assert command[command.index("-f") + 1] == "rawvideo"
    assert "-" in command


def test_build_ffmpeg_command_without_trim() -> None:
    runner = BoundedMemoryPipeRunner()
    command = runner.build_ffmpeg_command(spec())
    assert "trim=" not in " ".join(command)
    assert "eq=" in " ".join(command)


def test_build_ffmpeg_command_with_open_ended_trim() -> None:
    # P1-B: trim_start > 0 with trim_end=None must still emit a trim filter,
    # matching the CPU engine instead of silently dropping the offset.
    runner = BoundedMemoryPipeRunner()
    command = runner.build_ffmpeg_command(spec(trim_start=3))
    joined = " ".join(command)
    assert "trim=start_frame=3" in joined
    assert "setpts=PTS-STARTPTS" in joined
    assert "end_frame" not in joined


def test_build_ffmpeg_command_with_closed_trim() -> None:
    runner = BoundedMemoryPipeRunner()
    command = runner.build_ffmpeg_command(spec(trim_start=2, trim_end=9))
    assert "trim=start_frame=2:end_frame=9" in " ".join(command)


def test_build_ffmpeg_command_refuses_dissolve() -> None:
    # P1-B: no byte-equivalent FFmpeg graph exists for the CPU dissolve, so the
    # FFmpeg path fails closed rather than producing a different result.
    runner = BoundedMemoryPipeRunner()
    with pytest.raises(PipeExecutionError, match="dissolve"):
        runner.build_ffmpeg_command(spec(dissolve=0.5))


def test_run_ffmpeg_stream_refuses_dissolve_before_spawn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*args: object, **kwargs: object) -> object:
        raise AssertionError("Popen must not be reached for a dissolve spec")

    monkeypatch.setattr(subprocess, "Popen", boom)
    runner = BoundedMemoryPipeRunner()
    with pytest.raises(PipeExecutionError, match="dissolve"):
        runner.run_ffmpeg_stream([frames(1)], spec(dissolve=0.5))


def test_run_ffmpeg_stream_enforces_memory_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    # P1-C: a frame whose in-flight size exceeds max_buffer_bytes must fail closed
    # on the FFmpeg path, not merely be recorded in the peak.
    class FakeProc:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self.stdin = io.BytesIO()
            self.stdout = io.BytesIO(b"")
            self.stderr = io.BytesIO(b"")

        def poll(self) -> int:
            return 0

        def wait(self, timeout: float | None = None) -> int:
            return 0

    monkeypatch.setattr(subprocess, "Popen", FakeProc)
    runner = BoundedMemoryPipeRunner(max_buffer_bytes=1024)
    with pytest.raises(MemoryBudgetExceededError):
        runner.run_ffmpeg_stream([frames(1, size=8)], spec())


def test_run_ffmpeg_stream_rejects_partial_frame(monkeypatch: pytest.MonkeyPatch) -> None:
    # P1-B: a stdout byte count that is not a whole number of frames is refused.
    class FakeProc:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self.stdin = io.BytesIO()
            self.stdout = io.BytesIO(b"\x00" * 100)  # 100 is not a multiple of 192
            self.stderr = io.BytesIO(b"")

        def poll(self) -> int:
            return 0

        def wait(self, timeout: float | None = None) -> int:
            return 0

    monkeypatch.setattr(subprocess, "Popen", FakeProc)
    runner = BoundedMemoryPipeRunner()
    with pytest.raises(PipeExecutionError, match="whole number"):
        runner.run_ffmpeg_stream([frames(1)], spec())


def test_run_ffmpeg_stream_rejects_wrong_frame_count(monkeypatch: pytest.MonkeyPatch) -> None:
    # P1-B: a closed trim range whose output frame count disagrees is refused.
    class FakeProc:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self.stdin = io.BytesIO()
            self.stdout = io.BytesIO(b"\x00" * 192)  # exactly one frame, but 3 expected
            self.stderr = io.BytesIO(b"")

        def poll(self) -> int:
            return 0

        def wait(self, timeout: float | None = None) -> int:
            return 0

    monkeypatch.setattr(subprocess, "Popen", FakeProc)
    runner = BoundedMemoryPipeRunner()
    with pytest.raises(PipeExecutionError, match="expected"):
        runner.run_ffmpeg_stream([frames(1)], spec(trim_start=0, trim_end=3))


def test_build_ffmpeg_command_rejects_nan() -> None:
    runner = BoundedMemoryPipeRunner()
    with pytest.raises(PipeExecutionError):
        runner.build_ffmpeg_command(spec(brightness=float("nan")))


def test_run_ffmpeg_stream_reports_missing_binary() -> None:
    runner = BoundedMemoryPipeRunner(ffmpeg_bin="/nonexistent/ffmpeg-binary-xyz")
    with pytest.raises(PipeExecutionError):
        runner.run_ffmpeg_stream([frames(1)], spec())


def test_run_ffmpeg_stream_kills_wedged_child(monkeypatch: pytest.MonkeyPatch) -> None:
    # P1-D: if the child is still alive when we finish, it must be killed and
    # reaped rather than left running.
    killed = {"kill": False}

    class WedgedProc:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self.stdin = io.BytesIO()
            self.stdout = io.BytesIO(b"")
            self.stderr = io.BytesIO(b"")

        def poll(self) -> None:
            return None

        def kill(self) -> None:
            killed["kill"] = True

        def wait(self, timeout: float | None = None) -> int:
            return 0

    monkeypatch.setattr(subprocess, "Popen", WedgedProc)
    runner = BoundedMemoryPipeRunner()
    outcome = runner.run_ffmpeg_stream([], spec())
    assert killed["kill"] is True
    assert outcome.frames_processed == 0


def test_run_ffmpeg_stream_reports_nonzero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeProc:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self.stdin = io.BytesIO()
            self.stdout = io.BytesIO(b"")
            self.stderr = io.BytesIO(b"boom")

        def poll(self) -> int:
            return 1

        def wait(self, timeout: float | None = None) -> int:
            return 1

    monkeypatch.setattr(subprocess, "Popen", FakeProc)
    runner = BoundedMemoryPipeRunner()
    with pytest.raises(PipeExecutionError):
        runner.run_ffmpeg_stream([frames(1)], spec())


def test_run_ffmpeg_stream_success(monkeypatch: pytest.MonkeyPatch) -> None:
    class CloseOncePipe(io.BytesIO):
        def __init__(self, data: bytes) -> None:
            super().__init__(data)
            self._closed_once = False

        def close(self) -> None:
            if not self._closed_once:
                self._closed_once = True
                raise OSError("already closed")

    class FakeProc:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self.stdin = io.BytesIO()
            # Two whole 8x8x3 frames so the output frame-count check passes.
            self.stdout = CloseOncePipe(b"rawout" * (384 // 6))
            self.stderr = CloseOncePipe(b"")

        def poll(self) -> int:
            return 0

        def wait(self, timeout: float | None = None) -> int:
            return 0

    monkeypatch.setattr(subprocess, "Popen", FakeProc)
    runner = BoundedMemoryPipeRunner()
    outcome = runner.run_ffmpeg_stream([frames(1)], spec())
    assert outcome.mode is MuxMode.FFMPEG_SUBPROCESS_PIPE
    assert outcome.body_size == 384
    assert outcome.frames_processed == 2


def test_run_ffmpeg_stream_relays_writer_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeProc:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self.stdin = io.BytesIO()
            self.stdout = io.BytesIO(b"")
            self.stderr = io.BytesIO(b"")

        def poll(self) -> int:
            return 0

        def wait(self, timeout: float | None = None) -> int:
            return 0

    monkeypatch.setattr(subprocess, "Popen", FakeProc)
    runner = BoundedMemoryPipeRunner()
    # A wrong-shape frame is rejected in the writer thread and relayed out.
    with pytest.raises(PipeExecutionError):
        runner.run_ffmpeg_stream([iter([np.zeros((4, 4, 3), dtype=np.uint8)])], spec())


def test_run_ffmpeg_stream_relays_plain_oserror(monkeypatch: pytest.MonkeyPatch) -> None:
    class ExplodingStdin:
        def __init__(self) -> None:
            self._closed_once = False

        def write(self, data: bytes, /) -> int:
            raise OSError("pipe broken")

        def close(self) -> None:
            # Raise once (exercising the writer's close guard); the later GC
            # close is a no-op so it cannot raise an unraisable warning.
            if not self._closed_once:
                self._closed_once = True
                raise OSError("already closed")

    class FakeProc:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self.stdin = ExplodingStdin()
            self.stdout = io.BytesIO(b"")
            self.stderr = io.BytesIO(b"")

        def poll(self) -> int:
            return 0

        def wait(self, timeout: float | None = None) -> int:
            return 0

    monkeypatch.setattr(subprocess, "Popen", FakeProc)
    runner = BoundedMemoryPipeRunner()
    with pytest.raises(PipeExecutionError, match="writer failed"):
        runner.run_ffmpeg_stream([frames(1)], spec())


# ── receipts ────────────────────────────────────────────────────────────────
def test_build_receipt_computes_ratio() -> None:
    runner = BoundedMemoryPipeRunner()
    receipt = runner.build_receipt(
        timeline_merkle_root="r" * 64,
        total_segments=20,
        cache_hits=19,
        rendered_segments=1,
        peak_buffer_bytes=1234,
        output_digest="d" * 64,
        mode=MuxMode.DETERMINISTIC_CPU_MUX,
    )
    assert isinstance(receipt, CompileExecutionReceipt)
    assert receipt.cache_hit_ratio == 0.95
    assert receipt.mode == "deterministic_cpu_mux"
    assert receipt.as_dict()["cache_hits"] == 19


def test_build_receipt_zero_segments() -> None:
    runner = BoundedMemoryPipeRunner()
    receipt = runner.build_receipt(
        timeline_merkle_root="r" * 64,
        total_segments=0,
        cache_hits=0,
        rendered_segments=0,
        peak_buffer_bytes=0,
        output_digest="d" * 64,
        mode=MuxMode.FFMPEG_SUBPROCESS_PIPE,
    )
    assert receipt.cache_hit_ratio == 0.0


def test_build_receipt_rejects_negative_total() -> None:
    runner = BoundedMemoryPipeRunner()
    with pytest.raises(PipeExecutionError):
        runner.build_receipt(
            timeline_merkle_root="r" * 64,
            total_segments=-1,
            cache_hits=0,
            rendered_segments=0,
            peak_buffer_bytes=0,
            output_digest="d" * 64,
            mode=MuxMode.DETERMINISTIC_CPU_MUX,
        )


def test_receipt_is_frozen() -> None:
    runner = BoundedMemoryPipeRunner()
    receipt = runner.build_receipt(
        timeline_merkle_root="r" * 64,
        total_segments=1,
        cache_hits=0,
        rendered_segments=1,
        peak_buffer_bytes=0,
        output_digest="d" * 64,
        mode=MuxMode.DETERMINISTIC_CPU_MUX,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        receipt.cache_hits = 5  # type: ignore[misc]


def test_container_header_embeds_frame_digest() -> None:
    runner = BoundedMemoryPipeRunner(max_buffer_bytes=1024 * 1024)
    outcome = runner.stream(frames(2, value=10), spec())
    assert outcome.container_path is not None
    raw = outcome.container_path.read_bytes()
    assert raw.startswith(bmpr._MUX_MAGIC)
    assert outcome.output_digest == hashlib.sha256(raw).hexdigest()
    assert outcome.body_digest == hashlib.sha256(raw[bmpr._MUX_HEADER_LEN :]).hexdigest()
    outcome.container_path.unlink()
