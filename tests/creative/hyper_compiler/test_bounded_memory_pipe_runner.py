"""Bounded-memory pipe runner tests: streaming, backpressure and receipts."""

from __future__ import annotations

import dataclasses
import hashlib
import subprocess
from collections.abc import Iterator

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


# ── streaming CPU mux ───────────────────────────────────────────────────────
def test_stream_is_deterministic() -> None:
    runner = BoundedMemoryPipeRunner(max_buffer_bytes=1024 * 1024)
    first = runner.stream(frames(5), spec())
    second = runner.stream(frames(5), spec())
    assert first.container == second.container
    assert first.output_digest == second.output_digest


def test_stream_trims_frames() -> None:
    runner = BoundedMemoryPipeRunner(max_buffer_bytes=1024 * 1024)
    outcome = runner.stream(frames(10), spec(trim_start=2, trim_end=5))
    assert outcome.frames_processed == 3


def test_stream_open_ended_trim() -> None:
    runner = BoundedMemoryPipeRunner(max_buffer_bytes=1024 * 1024)
    outcome = runner.stream(frames(10), spec(trim_start=7))
    assert outcome.frames_processed == 3


def test_stream_applies_dissolve() -> None:
    runner = BoundedMemoryPipeRunner(max_buffer_bytes=1024 * 1024)
    outcome = runner.stream(frames(4, value=50), spec(dissolve=0.5))
    assert outcome.frames_processed == 4


def test_stream_peak_never_exceeds_budget() -> None:
    # 12 frames of 8x8x3 = 192 bytes each; a 3-frame budget must never be crossed.
    runner = BoundedMemoryPipeRunner(max_buffer_bytes=192 * 3)
    outcome = runner.stream(frames(12), spec())
    assert outcome.peak_buffer_bytes <= outcome.max_buffer_bytes
    assert outcome.peak_buffer_bytes <= 192 * 3
    assert outcome.frames_processed == 12


def test_stream_refuses_frame_larger_than_budget() -> None:
    runner = BoundedMemoryPipeRunner(max_buffer_bytes=64)
    with pytest.raises(MemoryBudgetExceededError):
        runner.stream(frames(1, size=8), spec())


def test_stream_many_across_segments() -> None:
    runner = BoundedMemoryPipeRunner(max_buffer_bytes=192 * 3)
    outcome = runner.stream_many([frames(5), frames(5)], spec())
    assert outcome.frames_processed == 10
    assert outcome.peak_buffer_bytes <= outcome.max_buffer_bytes


def test_stream_many_empty_is_allowed() -> None:
    runner = BoundedMemoryPipeRunner(max_buffer_bytes=1024)
    outcome = runner.stream_many([], spec())
    assert outcome.frames_processed == 0


def test_stream_many_applies_trim_across_segments() -> None:
    runner = BoundedMemoryPipeRunner(max_buffer_bytes=1024 * 1024)
    outcome = runner.stream_many([frames(4), frames(4)], spec(trim_start=2, trim_end=6))
    assert outcome.frames_processed == 4


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


def test_build_ffmpeg_command_rejects_nan() -> None:
    runner = BoundedMemoryPipeRunner()
    with pytest.raises(PipeExecutionError):
        runner.build_ffmpeg_command(spec(brightness=float("nan")))


def test_run_ffmpeg_pipe_reports_missing_binary() -> None:
    runner = BoundedMemoryPipeRunner(ffmpeg_bin="/nonexistent/ffmpeg-binary-xyz")
    with pytest.raises(PipeExecutionError):
        runner.run_ffmpeg_pipe(b"\x00" * (8 * 8 * 3), spec())


def test_run_ffmpeg_pipe_reports_nonzero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(args=[], returncode=1, stdout=b"", stderr=b"boom")

    monkeypatch.setattr(subprocess, "run", fake_run)
    runner = BoundedMemoryPipeRunner()
    with pytest.raises(PipeExecutionError):
        runner.run_ffmpeg_pipe(b"", spec())


def test_run_ffmpeg_pipe_success(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(args=[], returncode=0, stdout=b"rawout", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    runner = BoundedMemoryPipeRunner()
    assert runner.run_ffmpeg_pipe(b"", spec()) == b"rawout"


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
    assert outcome.container.startswith(bmpr._MUX_MAGIC)
    assert outcome.output_digest == hashlib.sha256(outcome.container).hexdigest()
