"""Bench regression gate — wave-4 step6.

Deterministic, GPU-free, offline: p50 vs committed baseline.
"""

from __future__ import annotations

import json
from pathlib import Path

BASELINE = Path(__file__).parent / "baseline_render.json"
BASELINE_CAPTION = Path(__file__).parent / "baseline_caption.json"


def test_baseline_files_exist() -> None:
    assert BASELINE.is_file(), (
        "baseline_render.json missing — run scripts/bench_render.py --baseline"
    )
    assert BASELINE_CAPTION.is_file(), "baseline_caption.json missing"
    # Must be valid JSON with p50
    data = json.loads(BASELINE.read_text(encoding="utf-8"))
    assert "p50_ms" in data
    data2 = json.loads(BASELINE_CAPTION.read_text(encoding="utf-8"))
    assert "p50_ms" in data2


def test_render_bench_within_tolerance() -> None:
    from scripts.bench_render import bench_render_ir_compile

    stats = bench_render_ir_compile(iterations=10)
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    # Allow 50 % slack in this deterministic unit-test run (CI will use 15 %)
    assert stats["p50_ms"] < baseline["p50_ms"] * 1.5, (
        f"render bench p50 {stats['p50_ms']:.3f} > baseline {baseline['p50_ms']:.3f} *1.5"
    )


def test_caption_bench_within_tolerance() -> None:
    from scripts.bench_caption import bench_caption_format

    stats = bench_caption_format(iterations=10)
    baseline = json.loads(BASELINE_CAPTION.read_text(encoding="utf-8"))
    assert stats["p50_ms"] < baseline["p50_ms"] * 1.5, (
        f"caption bench p50 {stats['p50_ms']:.4f} > baseline {baseline['p50_ms']:.4f} *1.5"
    )
