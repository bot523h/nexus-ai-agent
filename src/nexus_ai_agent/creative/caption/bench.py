"""Caption-format benchmark core — pure SRT formatting, no Whisper.

Wave-4 step6 harness; ``scripts/bench_caption.py`` is a thin CLI over
:func:`bench_caption_format`.  Kept inside the installed package so tests
never import the unpackaged ``scripts/`` namespace (console-script
``pytest`` in CI does not put the repository root on ``sys.path``).

Deterministic, GPU-free, offline: formats a fixed three-segment transcript
through the pure caption pack formatter and reports timing stats in ms.
The pack imports stay function-local exactly like the original harness —
module import cost must not sit inside the timed loop, and the CLI's
"bench skipped" fallback keeps working when the pack is unavailable.
"""

from __future__ import annotations

import time


def bench_caption_format(iterations: int = 200) -> dict[str, float]:
    from nexus_ai_agent.creative.packs.caption.formatters import format_srt
    from nexus_ai_agent.creative.packs.caption.models import TranscriptSegment

    segs = [
        TranscriptSegment(start_us=0, end_us=500_000, text="سلام"),
        TranscriptSegment(start_us=500_000, end_us=1_000_000, text="دنیا"),
        TranscriptSegment(start_us=1_000_000, end_us=1_500_000, text="نگار"),
    ]
    # Warm up
    format_srt(segs)

    times: list[float] = []
    for _ in range(iterations):
        t0 = time.perf_counter()
        out = format_srt(segs)
        _ = len(out)
        times.append((time.perf_counter() - t0) * 1000.0)

    times.sort()
    return {
        "iterations": float(iterations),
        "p50_ms": times[len(times) // 2],
        "p95_ms": times[int(len(times) * 0.95)],
        "mean_ms": sum(times) / len(times),
        "min_ms": times[0],
        "max_ms": times[-1],
    }
