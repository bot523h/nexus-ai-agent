"""Reproducible, uncached wall-clock/RSS benchmark; writes only JSON to stdout.

PYTHONPATH=src OPENBLAS_NUM_THREADS=1 python \
    tests/creative/world_backlot/benchmark_world_backlot.py --runs 9
Add --maximum --runs 1 for all public recipe caps and two streamed frames.
Imports are excluded; catalogue construction, every artifact and sealing are not.
"""

from __future__ import annotations

import argparse
import json
import resource
import statistics
import sys
import time
from collections.abc import Sequence
from pathlib import Path

import numpy as np

from nexus_ai_agent.creative.world_backlot import RecipeQuality, resolve_director_scene_recipe


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=9, choices=range(1, 51))
    parser.add_argument("--maximum", action="store_true")
    args = parser.parse_args(argv)
    quality = RecipeQuality(32768, 32768, 33, 5) if args.maximum else RecipeQuality()
    description = (
        "midnight modern urban thunderstorm rain"
        if args.maximum
        else "نیمه‌شب کویری ایران با آسمان پرستاره، باد و گردوغبار ملایم، "
        "معماری کاروانسرای سنتی و نور آبی مهتاب"
    )
    elapsed: list[float] = []
    final_sha = ""
    for run in range(args.runs):
        start = time.perf_counter_ns()
        recipe = resolve_director_scene_recipe(description, seed=run, quality=quality)
        wav = recipe.wav_bytes
        elapsed.append((time.perf_counter_ns() - start) / 1e6)
        assert recipe.verify() and wav.startswith(b"RIFF")
        final_sha = recipe.sha256
        if args.maximum:
            for frame in recipe.iter_frames(2):
                assert frame.scene_dna_sha256 == recipe.layout.seal.sha256
            for obj in recipe.layout.objects:
                assert obj.mesh().vertices
    peak_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    peak_mib = peak_rss / (1024 * 1024 if sys.platform == "darwin" else 1024)
    if sys.platform.startswith("linux"):
        # ru_maxrss can retain the parent's high-water mark across fork/exec.
        # VmHWM belongs to this executable's mm and measures its actual RSS peak.
        status = Path("/proc/self/status").read_text(encoding="ascii")
        peak_mib = (
            float(
                next(line.split()[1] for line in status.splitlines() if line.startswith("VmHWM:"))
            )
            / 1024
        )
    print(
        json.dumps(
            {
                "schema": "world-backlot-benchmark-v1",
                "python": sys.version.split()[0],
                "numpy": np.__version__,
                "runs": args.runs,
                "maximum": args.maximum,
                "stars": quality.star_count,
                "particles": quality.particle_count,
                "cube_size": quality.cube_size,
                "audio_seconds": quality.audio_seconds,
                "min_resolution_ms": min(elapsed),
                "median_resolution_ms": statistics.median(elapsed),
                "max_resolution_ms": max(elapsed),
                "peak_rss_mib": peak_mib,
                "final_recipe_sha256": final_sha,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
