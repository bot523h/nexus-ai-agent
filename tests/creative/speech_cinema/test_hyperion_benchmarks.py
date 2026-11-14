"""Hyperion N = 3 measured benchmarks.

Each workload runs three times.  Wall time is taken with ``time.perf_counter_ns`` and the
peak Python-heap allocation with ``tracemalloc``.  Results are printed (run with ``-s``)
and written to ``HYPERION_BENCH_OUT`` when that variable is set.  The assertions are
generous ceilings that catch pathological regressions, not tight performance contracts.
"""

from __future__ import annotations

import json
import os
import statistics
import time
import tracemalloc
from collections.abc import Callable
from pathlib import Path

import numpy as np
import pytest

from nexus_ai_agent.creative.speech_cinema import color_imaging as ci
from nexus_ai_agent.creative.speech_cinema import forced_aligner as fa
from nexus_ai_agent.creative.speech_cinema import isochronous_dubbing as iso
from nexus_ai_agent.creative.speech_cinema import subtitle_compiler as sc
from nexus_ai_agent.creative.speech_cinema import vad_detector as vd
from nexus_ai_agent.storage.artifact_vault import cdc_chunker as cdc
from nexus_ai_agent.storage.artifact_vault import vault_manager as vm

N_RUNS = 3
RESULTS: dict[str, dict[str, float]] = {}


def _measure(name: str, workload: Callable[[Path], None], tmp_path: Path) -> None:
    times_ms: list[float] = []
    peak_kib: list[float] = []
    for run in range(N_RUNS):
        scratch = tmp_path / f"{name}-{run}"
        scratch.mkdir()
        tracemalloc.start()
        start = time.perf_counter_ns()
        workload(scratch)
        elapsed = (time.perf_counter_ns() - start) / 1e6
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        times_ms.append(elapsed)
        peak_kib.append(peak / 1024)
    stats = {
        "runs": N_RUNS,
        "median_ms": statistics.median(times_ms),
        "max_ms": max(times_ms),
        "peak_heap_kib": max(peak_kib),
    }
    RESULTS[name] = stats
    print(f"\n[bench] {name}: {stats}")
    out = os.environ.get("HYPERION_BENCH_OUT")
    if out:
        Path(out).write_text(json.dumps(RESULTS, indent=2), encoding="utf-8")
    assert stats["max_ms"] < 60_000, f"{name} exceeded the 60 s ceiling"


def _vault_workload(scratch: Path) -> None:
    data = np.random.default_rng(101).integers(0, 256, 256 * 1024, dtype=np.uint8).tobytes()
    chunks = cdc.chunk_bytes(data)
    assert b"".join(c.data for c in chunks) == data
    vault = vm.ArtifactVault(scratch)
    vault.store_artifact("bench", data, 8, 4)
    artifact_dir = vault._dir("bench")
    shards = sorted(p for p in artifact_dir.iterdir() if p.is_file() and p.suffix != ".json")
    for victim in shards[:4]:  # four erasures, the maximum m = 4 tolerates
        victim.write_bytes(b"\x00" * victim.stat().st_size)
    vault.verify_and_heal_artifact("bench")
    assert vault.read_artifact("bench") == data
    leaves = [c.data for c in chunks]
    root = vm.merkle_root(leaves)
    assert vm.verify_merkle_proof(leaves[0], 0, vm.merkle_proof(leaves, 0), root)


def _speech_workload(scratch: Path) -> None:
    rate = 16000
    t = np.arange(5 * rate) / rate
    envelope = (np.sin(2 * np.pi * 3 * t) > 0).astype(np.float32)
    audio = (envelope * sum(0.2 / k * np.sin(2 * np.pi * 150 * k * t) for k in range(1, 6))).astype(
        np.float32
    )
    result = vd.detect_voice(audio, rate)
    assert result.speech_mask.size > 0
    cost = np.abs(np.subtract.outer(np.linspace(0, 1, 200), np.linspace(0, 1, 200)))
    _, path = fa.banded_dtw(cost, band=20)
    assert path[-1] == (199, 199)
    scaled = iso.time_scale(audio, int(audio.size * 0.75), rate).audio
    assert scaled.size == int(audio.size * 0.75)
    words = [sc.AlignedWord(text=f"w{i}", start=i * 0.2, end=i * 0.2 + 0.15) for i in range(60)]
    cues = sc.partition_cues(words)
    # Serialisers keep millisecond precision by design, so compare at that resolution.
    for reparsed in (sc.parse_srt(sc.serialize_srt(cues)), sc.parse_vtt(sc.serialize_vtt(cues))):
        assert [c.text for c in reparsed] == [c.text for c in cues]
        assert [round(c.start, 3) for c in reparsed] == [round(c.start, 3) for c in cues]
        assert [round(c.end, 3) for c in reparsed] == [round(c.end, 3) for c in cues]
    assert "[Events]" in sc.serialize_ass(cues)
    (scratch / "cues.srt").write_text(sc.serialize_srt(cues), encoding="utf-8")


def _color_workload(scratch: Path) -> None:
    rng = np.random.default_rng(202)
    img = rng.random((128, 128, 3)) * 0.8 + 0.1
    adapted = ci.white_balance_chromatic_adaptation(img, kelvin=3200.0)
    # Bradford can produce small negative channels; negative radiance is refused by design,
    # so the HDR input is clipped to non-negative before the tonemap.
    hdr = np.clip(adapted, 0.0, None) * 40.0
    sdr = ci.hdr_to_sdr_filmic_tonemap(hdr)
    assert float(sdr.max()) <= 1.0
    ref = rng.random((128, 128, 3)) * 0.5 + 0.25
    matched = ci.shot_to_shot_color_match_ot(sdr, ref)
    assert matched.shape == (128, 128, 3)
    ci.save_color_png(np.clip(matched, 0.0, 1.0), scratch / "match.png")
    assert ci.verify_color_png(scratch / "match.png")


@pytest.mark.slow
@pytest.mark.parametrize(
    ("name", "workload"),
    [
        ("artifact_vault_cdc_rs8_4_heal_merkle", _vault_workload),
        ("speech_cinema_vad_dtw_wsola_subtitles_5s_16k", _speech_workload),
        ("color_imaging_bradford_filmic_ot_128x128x3", _color_workload),
    ],
)
def test_hyperion_benchmark_n3(name: str, workload: Callable[[Path], None], tmp_path: Path) -> None:
    _measure(name, workload, tmp_path)
    assert RESULTS[name]["runs"] == N_RUNS
