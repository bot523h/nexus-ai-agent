"""N=3 micro-benchmarks for the NAGAR OLYMPUS waves (21-30).

These are wall-clock samples on synthetic workloads, not production-throughput or
tail-latency claims. Peak memory is measured with ``tracemalloc`` around each
operation (``ru_maxrss`` is a whole-process high-water mark and would include the
interpreter, so it is reported only as a coarse process figure).

Workloads are small / medium / large synthetic inputs per operator.
"""

from __future__ import annotations

import statistics
import time
import tracemalloc
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

import numpy as np
import pytest

from nexus_ai_agent.creative.atlas import (
    aces_hald_film_optic as film,
)
from nexus_ai_agent.creative.atlas import (
    botanical_modal_wind as wind,
)
from nexus_ai_agent.creative.atlas import (
    diegetic_modal_foley as foley,
)
from nexus_ai_agent.creative.atlas import (
    media_sine as media,
)
from nexus_ai_agent.creative.atlas import (
    olympus_master as master,
)
from nexus_ai_agent.creative.atlas import (
    sdf_global_illumination as sdf,
)
from nexus_ai_agent.creative.atlas import (
    showrunner_bible as bible,
)
from nexus_ai_agent.creative.atlas import (
    spectral_ocean_swe as ocean,
)
from nexus_ai_agent.tools.filesystem_policy import WorkspaceFilesystem

N = 3
T = TypeVar("T")


def _sample(fn: Callable[[], T]) -> tuple[float, float, int]:
    """Return (median_ms, max_ms, peak_bytes) over ``N`` runs."""
    times: list[float] = []
    peak = 0
    for _ in range(N):
        tracemalloc.start()
        start = time.perf_counter()
        fn()
        times.append((time.perf_counter() - start) * 1000.0)
        _, pk = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        peak = max(peak, pk)
    return statistics.median(times), max(times), peak


def _report(name: str, med: float, mx: float, peak: int) -> None:
    print(f"[bench] {name:34s} median={med:8.2f} ms  max={mx:8.2f} ms  peak={peak / 1024:9.1f} KiB")


def test_bench_sdf_ambient_occlusion() -> None:
    a = sdf.Sphere(np.array([-1.1, 0.0, 0.0]), 1.0)
    b = sdf.Sphere(np.array([1.1, 0.0, 0.0]), 1.0)

    class Union:
        def distance(self, point: np.ndarray) -> float:
            return min(a.distance(point), b.distance(point))

    u = Union()
    p = np.array([-0.1, 0.0, 0.0])
    n = sdf.sdf_normal(u, p)
    for samples in (8, 32, 128):
        med, mx, peak = _sample(
            lambda samples=samples: sdf.sdf_ambient_occlusion(u, p, n, samples=samples)
        )
        _report(f"sdf_ao samples={samples}", med, mx, peak)
        assert med < 2000.0


def test_bench_spectral_ocean() -> None:
    for n in (32, 64, 128):
        kx, ky = np.meshgrid(np.linspace(-0.5, 0.5, n), np.linspace(-0.5, 0.5, n))
        med, mx, peak = _sample(
            lambda kx=kx, ky=ky: ocean.spectral_height_field(ocean.jonswap_spectrum(kx, ky), seed=1)
        )
        _report(f"ocean_height n={n}", med, mx, peak)
        assert med < 3000.0


def test_bench_botanical_wind() -> None:
    stem = wind.ModalStem(2.0, 1e9, 1e-8, 700.0, 1e-4)
    for n in (64, 256, 1024):
        g = wind.davenport_gusts(n_samples=n, dt=0.05, mean_speed=5.0, seed=1)
        med, mx, peak = _sample(
            lambda g=g: wind.modal_wind_response(stem, g, modes=3, integration="semi_implicit")
        )
        _report(f"modal_wind n={n}", med, mx, peak)
        assert med < 2000.0


def test_bench_film_optic() -> None:
    for side in (32, 128, 256):
        img = np.random.default_rng(0).random((side, side, 3))
        clut = film.hald_identity_clut(8)
        med, mx, peak = _sample(lambda img=img, clut=clut: film.apply_hald_clut(img, clut, 8))
        _report(f"hald_clut {side}x{side}", med, mx, peak)
        assert med < 2000.0


def test_bench_modal_foley() -> None:
    for count in (1, 16, 64):
        events = [
            foley.DiegeticEvent(f"e{i}", i * 0.01, i * 1.0, "wood", seed=i) for i in range(count)
        ]
        med, mx, peak = _sample(
            lambda events=events: foley.place_foley_events(events, duration=2.0)
        )
        _report(f"foley events={count}", med, mx, peak)
        assert med < 3000.0


def test_bench_showrunner_bible() -> None:
    for episodes in (10, 100, 500):
        b = bible.ShowrunnerBible()
        b.add_character(bible.Character("a", "Alice"))
        b.add_prop(bible.Prop("p", "Prop"))
        for i in range(1, episodes + 1):
            b.add_episode(
                bible.Episode(i, f"E{i}", transfers=(("p", "a"),), facts=((f"f{i}", "a"),))
            )
        med, mx, peak = _sample(b.check_continuity)
        _report(f"bible episodes={episodes}", med, mx, peak)
        assert med < 2000.0


def test_bench_master_render(tmp_path: Path) -> None:
    b = bible.ShowrunnerBible()
    b.add_character(bible.Character("a", "Alice"))
    b.add_prop(bible.Prop("p", "Prop"))
    b.add_episode(bible.Episode(1, "Pilot", transfers=(("p", "a"),)))
    for side, frames in ((16, 4), (32, 8), (64, 16)):
        fs = WorkspaceFilesystem(tmp_path / f"m{side}")
        fs.root.mkdir(parents=True, exist_ok=True)
        med, mx, peak = _sample(
            lambda fs=fs, side=side, frames=frames: master.render_master(
                b, fs, side=side, frames=frames, fps=8
            )
        )
        _report(f"master {side}x{side}x{frames}", med, mx, peak)
        assert med < 8000.0


@pytest.mark.skipif(not media.ffmpeg_available(), reason="ffmpeg/ffprobe not on PATH")
def test_bench_real_encode(tmp_path: Path) -> None:
    fs = WorkspaceFilesystem(tmp_path)
    for frames in (12, 48, 120):
        med, mx, peak = _sample(
            lambda frames=frames: media.synthesise_test_clip(
                fs, f"c{frames}.mp4", width=128, height=72, frames=frames, fps=24
            )
        )
        _report(f"ffmpeg encode frames={frames}", med, mx, peak)
        assert med < 30000.0
