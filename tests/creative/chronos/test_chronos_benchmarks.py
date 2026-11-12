"""Small repeatable CPU benchmark smoke test; values are environment-specific."""

from __future__ import annotations

import resource
import time
import wave
from collections.abc import Callable
from fractions import Fraction
from pathlib import Path

import numpy as np

from nexus_ai_agent.creative.chronos.acoustics_lipsync import shoebox_rir
from nexus_ai_agent.creative.chronos.color_science import (
    read_rgb8_png,
    srgb_decode,
    write_rgb8_png,
)
from nexus_ai_agent.creative.chronos.contracts import RationalTimestamp
from nexus_ai_agent.creative.chronos.geometry import intrinsics, project
from nexus_ai_agent.creative.chronos.timeline import assemble_pcm_wav_stream_copy
from nexus_ai_agent.creative.chronos.verifier import verify_persisted_relighting
from nexus_ai_agent.creative.studio.chronos_port import ChronosCommand


def test_chronos_benchmarks_n3(tmp_path: Path) -> None:
    def timeline_case() -> Fraction:
        return RationalTimestamp(30000, Fraction(1, 30000)).seconds

    def geometry_case() -> np.ndarray:
        matrix = intrinsics(50, 640, 480, 36, 24)
        return project(np.array([[0.1, 0.2, 2.0]]), np.eye(3), np.zeros(3), matrix)

    def color_case() -> np.ndarray:
        return srgb_decode(np.full((24, 24, 3), 0.4))

    def acoustics_case() -> np.ndarray:
        return shoebox_rir(
            np.array([1.0, 1.0, 1.0]),
            np.array([2.0, 1.0, 1.0]),
            np.array([5.0, 4.0, 3.0]),
            0.3,
            4000,
            0,
        )

    shot_paths = [tmp_path / f"bench-shot-{index}.wav" for index in range(3)]
    for path in shot_paths:
        with wave.open(str(path), "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(48000)
            writer.writeframes(np.arange(256, dtype="<i2").tobytes())

    def wav_assembly_case() -> np.ndarray:
        output = tmp_path / f"assembled-{len(list(tmp_path.glob('assembled-*.wav')))}.wav"
        receipt = assemble_pcm_wav_stream_copy(shot_paths, output, tmp_path / "stage")
        return np.asarray(
            [receipt.total_sample_frames, receipt.splice_boundaries_verified], dtype=float
        )

    grid_y, grid_x = np.mgrid[0:24, 0:24]
    target_rgb = np.repeat(
        (0.4 + 0.2 * np.sin(grid_x / 4) * np.cos(grid_y / 5))[:, :, None], 3, axis=2
    )
    source_rgb = target_rgb * 0.8 + 0.05

    def png_roundtrip_case() -> np.ndarray:
        source_path, target_path, relit_path = (
            tmp_path / "benchmark-source.png",
            tmp_path / "benchmark-target.png",
            tmp_path / "benchmark-relit.png",
        )
        write_rgb8_png(source_path, np.rint(source_rgb * 255).astype(np.uint8))
        write_rgb8_png(target_path, np.rint(target_rgb * 255).astype(np.uint8))
        write_rgb8_png(relit_path, np.rint(target_rgb * 255).astype(np.uint8))
        verify_persisted_relighting(
            source_rgb, read_rgb8_png(relit_path) / 255.0, target_rgb, tmp_path / "verified.png"
        )
        return read_rgb8_png(tmp_path / "verified.png").astype(np.float64)

    cases = {
        "pcm_wav_real_disk": wav_assembly_case,
        "png_real_disk": png_roundtrip_case,
        "timeline": timeline_case,
        "geometry": geometry_case,
        "color": color_case,
        "acoustics": acoustics_case,
    }
    report: dict[str, tuple[float, float]] = {}
    staging_bytes = 0
    for name, operation in cases.items():
        durations: list[float] = []
        for index in range(3):
            start = time.perf_counter()
            result = operation()
            elapsed = (time.perf_counter() - start) * 1000
            assert np.isfinite(np.asarray(result, dtype=np.float64)).all()
            durations.append(elapsed)
            staging_bytes += len(str(result)) if index == 0 else 0
        report[name] = (float(np.median(durations)), max(durations))
    command = ChronosCommand("bench", ("synthetic",), {})
    assert command.deterministic and not command.external_api_allowed
    peak_rss_kib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    assert peak_rss_kib > 0 and staging_bytes > 0
    assert all(np.isfinite(metrics).all() for metrics in report.values())
    print(
        {
            "median_max_ms": report,
            "peak_rss_kib": peak_rss_kib,
            "staging_bytes": staging_bytes,
        }
    )


def test_wave2_benchmarks_n3(tmp_path: Path) -> None:
    from nexus_ai_agent.creative.chronos.audio_continuity import declick_pcm_wav_splices
    from nexus_ai_agent.creative.chronos.compositor_25d import render_25d_multiplane_composite
    from nexus_ai_agent.creative.chronos.identity_lock import verify_pose_invariant_identity
    from nexus_ai_agent.creative.chronos.merkle_ledger import (
        build_inclusion_proof,
        merkle_root,
        verify_inclusion_proof,
    )
    from nexus_ai_agent.creative.chronos.scene_portrait_ops import (
        inpaint_masked_region_pde,
        retouch_portrait_frequency_separation,
        tonemap_scene_linear_filmic,
    )

    rate = 8000
    samples = np.tile(np.sin(np.arange(256) * 0.1), (1,)).astype(np.float64)
    wav_in = tmp_path / "wave2.wav"
    with wave.open(str(wav_in), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes((samples * 12000).astype("<i2").tobytes())
    points = np.array([[0.0, 0.0, 0.0], [0.04, 0.0, 0.0], [0.0, 0.04, 0.0], [0.02, 0.03, 0.0]])
    k = np.array([[800.0, 0.0, 16.0], [0.0, 800.0, 16.0], [0.0, 0.0, 1.0]])
    observed = np.column_stack((16 + points[:, 0] * 400, 16 + points[:, 1] * 400))
    img = np.full((16, 16, 3), 0.4)
    mask = np.zeros((16, 16), bool)
    mask[6:10, 6:10] = True
    receipts = [{"shot": i, "hash": str(i)} for i in range(4)]
    root = merkle_root(receipts)
    proof = build_inclusion_proof(receipts, 2)
    focus = (3.0, 50.0, 2.8, 36.0, 16)
    alpha = np.ones((16, 16))
    cases: dict[str, Callable[[int], object]] = {
        "declick": lambda i: declick_pcm_wav_splices(
            wav_in, tmp_path / f"declick-{i}.wav", [128], bridge_frames=16
        ),
        "merkle": lambda i: verify_inclusion_proof(receipts[2], proof, root),
        "identity": lambda i: verify_pose_invariant_identity(points, observed, k, 2.0),
        "tiled_compositor": lambda i: render_25d_multiplane_composite(
            [(img, alpha, 4.0)],
            img,
            tmp_path / f"tile-{i}.png",
            beta=0.01,
            focus=focus,
            tile_size_px=8,
        ),
        "pde_retouch_tonemap": lambda i: tonemap_scene_linear_filmic(
            retouch_portrait_frequency_separation(inpaint_masked_region_pde(img, mask)[0], mask)
        ),
    }
    report = {}
    for name, operation in cases.items():
        times = []
        for i in range(3):
            start = time.perf_counter()
            result = operation(i)
            times.append((time.perf_counter() - start) * 1000)
            assert result is not None
        report[name] = (float(np.median(times)), max(times))
    print(
        {
            "wave2_median_max_ms": report,
            "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            "staging_bytes": sum(p.stat().st_size for p in tmp_path.iterdir() if p.is_file()),
        }
    )
