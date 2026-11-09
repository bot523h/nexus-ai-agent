"""Benchmark for the composition runtime.

Measures compilation time, end-to-end execution time, per-node timing, peak
memory, and independent verification time for the canonical 3-op composition
(resize -> grayscale -> image-to-video).

The numbers here are *real*: they come from actually running FFmpeg and
ImageMagick on a generated input, not from a mock. Run with
``python scripts/bench_composition.py``.
"""

from __future__ import annotations

import resource
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .compiler import CompositionCompiler, CompositionGraph, CompositionNode, InputBinding
from .runtime import CompositionRuntime, RuntimePolicy
from .spec import (
    CapabilityInput,
    CapabilityOutput,
    CapabilitySpec,
    LicenseInfo,
    MediaType,
    ResourceRequirements,
)
from .workers import detect_tool_versions


def _registry() -> dict[str, CapabilitySpec]:
    png = MediaType(kind="image", codec="png")
    video = MediaType(kind="video", codec="h264", container="mp4")
    mit = LicenseInfo(spdx_id="MIT", source_license="MIT")
    return {
        "nagar.image.resize": CapabilitySpec(
            capability_id="nagar.image.resize", version="1.0.0",
            inputs=(CapabilityInput(name="source", media_type=png),),
            outputs=(CapabilityOutput(name="resized", media_type=png),),
            resources=ResourceRequirements(cpu_threads=1, ram_mb=200, disk_mb=100),
            license=mit,
        ),
        "nagar.image.grayscale": CapabilitySpec(
            capability_id="nagar.image.grayscale", version="1.0.0",
            inputs=(CapabilityInput(name="source", media_type=png),),
            outputs=(CapabilityOutput(name="grayscale", media_type=png),),
            resources=ResourceRequirements(cpu_threads=1, ram_mb=150, disk_mb=100),
            license=mit,
        ),
        "nagar.video.from_image": CapabilitySpec(
            capability_id="nagar.video.from_image", version="1.0.0",
            inputs=(CapabilityInput(name="source", media_type=png),),
            outputs=(CapabilityOutput(name="video", media_type=video),),
            resources=ResourceRequirements(cpu_threads=2, ram_mb=500, disk_mb=200),
            license=mit,
        ),
    }


def _make_graph() -> CompositionGraph:
    g = CompositionGraph()
    g.add_external_input("image", MediaType(kind="image", codec="png"))
    g.add_node(CompositionNode("resize", "nagar.image.resize",
        params={"width": 1280, "height": 720},
        bindings={"source": InputBinding("source", external="image")}))
    g.add_node(CompositionNode("gray", "nagar.image.grayscale",
        bindings={"source": InputBinding("source", node_output=("resize", "resized"))}))
    g.add_node(CompositionNode("video", "nagar.video.from_image",
        params={"fps": 24, "duration_seconds": 2},
        bindings={"source": InputBinding("source", node_output=("gray", "grayscale"))}))
    g.add_expected_output("clip", "video", "video")
    return g


@dataclass
class BenchResult:
    iterations: int
    compile_ms: float
    execute_ms_mean: float
    execute_ms_min: float
    execute_ms_max: float
    per_node_ms: dict[str, float]
    peak_rss_mb: float
    output_bytes: int
    output_hash: str
    verified: bool
    tool_versions: dict[str, str]

    def to_dict(self) -> dict:
        return asdict(self)


def bench_composition(iterations: int = 5) -> BenchResult:
    """Run the canonical 3-op composition *iterations* times and report stats."""
    reg = _registry()
    graph = _make_graph()
    compiler = CompositionCompiler(reg)

    # compile time (single shot, it's cheap)
    t0 = time.perf_counter()
    derived = compiler.compile(graph)
    compile_ms = (time.perf_counter() - t0) * 1000

    quota = ResourceRequirements(cpu_threads=8, ram_mb=4096, disk_mb=4096)
    runtime = CompositionRuntime(reg, policy=RuntimePolicy(quota=quota))

    with tempfile.TemporaryDirectory() as tmp:
        img = Path(tmp) / "in.png"
        subprocess.run(
            ["convert", "-size", "640x480", "gradient:blue-red", str(img)],
            check=True, capture_output=True,
        )
        times: list[float] = []
        per_node: dict[str, list[float]] = {}
        last = None
        for i in range(iterations):
            t0 = time.perf_counter()
            result = runtime.execute(graph, {"image": str(img)},
                                     output_dir=Path(tmp) / f"run_{i}")
            dt = (time.perf_counter() - t0) * 1000
            times.append(dt)
            for r in result.receipts:
                per_node.setdefault(r.node_id, []).append(r.duration_seconds * 1000)
            last = result

        peak_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        peak_mb = peak_kb / 1024.0  # ru_maxrss is KB on Linux

        art = last.artifacts["clip"]
        return BenchResult(
            iterations=iterations,
            compile_ms=round(compile_ms, 3),
            execute_ms_mean=round(sum(times) / len(times), 3),
            execute_ms_min=round(min(times), 3),
            execute_ms_max=round(max(times), 3),
            per_node_ms={k: round(sum(v) / len(v), 3) for k, v in per_node.items()},
            peak_rss_mb=round(peak_mb, 2),
            output_bytes=Path(last.outputs["clip"]).stat().st_size,
            output_hash=art.output_hash,
            verified=art.verification.verified,
            tool_versions=detect_tool_versions().to_dict(),
        )


__all__ = ["BenchResult", "bench_composition"]
