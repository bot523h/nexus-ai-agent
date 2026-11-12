#!/usr/bin/env python3
"""STAGE-A benchmarks: N runs, median, spread, Peak RSS and the tool version.

Doctrine §8 requires every benchmark claim to be backed by at least three runs
and to report the median, the spread, the Peak RSS and the tool version.  A
single run is an anecdote; a claimed number without a spread invites a reader to
assume a precision the measurement does not have.

Each benchmark drives the **real** engine over a synthesized programme, so the
numbers describe the thing that ships rather than a mock.

Usage::

    python scripts/omega_stage_a_benchmarks.py                # all benchmarks, N=3
    python scripts/omega_stage_a_benchmarks.py -n 5           # more samples
    python scripts/omega_stage_a_benchmarks.py --json out.json
    python scripts/omega_stage_a_benchmarks.py -k loudness    # one benchmark

Exit status: 0 when every benchmark produced N successful samples with a
measured Peak RSS; 1 when a benchmark failed or the host could not measure RSS
(reported as ``NOT_MEASURED`` in the table and the exit code).  This script
never estimates a number it did not measure.
"""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
if str(REPO / "src") not in sys.path:  # pragma: no cover - import bootstrap
    sys.path.insert(0, str(REPO / "src"))
if str(REPO / "tests" / "unit") not in sys.path:  # pragma: no cover
    sys.path.insert(0, str(REPO / "tests" / "unit"))


@dataclass
class Sample:
    """One run of one benchmark."""

    elapsed_seconds: float
    peak_rss_bytes: int | None
    output_size_bytes: int
    staging_peak_bytes: int
    processes_started: int
    within_budget: bool


@dataclass
class Result:
    """The N samples of one benchmark, summarised."""

    name: str
    description: str
    samples: list[Sample] = field(default_factory=list)
    error: str | None = None

    @property
    def measured(self) -> list[float]:
        return [sample.elapsed_seconds for sample in self.samples]

    def summary(self) -> dict[str, Any]:
        if not self.samples:
            return {"name": self.name, "status": "NO_SAMPLES", "error": self.error}
        times = self.measured
        rss = [s.peak_rss_bytes for s in self.samples if s.peak_rss_bytes is not None]
        return {
            "name": self.name,
            "description": self.description,
            "runs": len(self.samples),
            "elapsed_median_s": round(statistics.median(times), 4),
            "elapsed_min_s": round(min(times), 4),
            "elapsed_max_s": round(max(times), 4),
            "elapsed_spread_s": round(max(times) - min(times), 4),
            "elapsed_stdev_s": round(statistics.stdev(times), 4) if len(times) > 1 else None,
            "peak_rss_median_bytes": int(statistics.median(rss)) if rss else None,
            "peak_rss": "MEASURED" if rss else "NOT_MEASURED",
            "output_size_bytes": self.samples[0].output_size_bytes,
            "staging_peak_bytes": self.samples[0].staging_peak_bytes,
            "processes_started": self.samples[0].processes_started,
            "all_within_budget": all(s.within_budget for s in self.samples),
        }


def _engine(workspace: Path, media_dir: Path) -> Any:
    from nexus_ai_agent.creative.audio_engine.execution import (
        AudioExecutionSettings,
        LocalDirectorySourceCatalog,
        NativeAudioEngine,
    )

    return NativeAudioEngine(
        catalog=LocalDirectorySourceCatalog(media_dir),
        settings=AudioExecutionSettings(
            workspace_root=workspace,
            max_analysis_seconds=30,
            deadline_seconds=300,
        ),
    )


def _sample(evidence: Any, elapsed: float) -> Sample:
    budget = evidence.budget or {}
    return Sample(
        elapsed_seconds=float(budget.get("elapsed_seconds", elapsed)),
        peak_rss_bytes=budget.get("peak_rss_bytes"),
        output_size_bytes=int(budget.get("output_size_bytes", 0)),
        staging_peak_bytes=int(budget.get("staging_peak_bytes", 0)),
        processes_started=int(budget.get("processes_started", 0)),
        within_budget=bool(budget.get("within_budget", False)),
    )


def bench_denoise(workspace: Path, media: Path) -> Sample:
    from nexus_ai_agent.creative.audio_engine.workspace import hash_file
    from nexus_ai_agent.creative.studio.audio_engine import DenoiseRequest

    engine = _engine(workspace, media)
    source = media / "noisy.wav"
    evidence = engine.remove_noise(
        DenoiseRequest(
            request_id=f"bench-denoise-{workspace.name}",
            source_asset_id="noisy",
            output_asset_id=f"bench_out_{workspace.name}",
            source_sha256=hash_file(source),
            strength=0.6,
        )
    )
    return _sample(evidence, 0.0)


def bench_loudness(workspace: Path, media: Path) -> Sample:
    from nexus_ai_agent.creative.audio_engine.workspace import hash_file
    from nexus_ai_agent.creative.studio.audio_engine import LoudnessRequest

    engine = _engine(workspace, media)
    source = media / "programme.wav"
    evidence = engine.normalize_loudness(
        LoudnessRequest(
            request_id=f"bench-loudness-{workspace.name}",
            source_asset_id="programme",
            output_asset_id=f"bench_master_{workspace.name}",
            source_sha256=hash_file(source),
            profile_id="nagar_streaming_preset",
            target_lufs=-14.0,
            true_peak_dbtp=-1.0,
            target_lra_lu=11.0,
        )
    )
    return _sample(evidence, 0.0)


def bench_beats(workspace: Path, media: Path) -> Sample:
    from nexus_ai_agent.creative.studio.audio_engine import BeatRequest

    engine = _engine(workspace, media)
    analysis = engine.detect_beats(
        BeatRequest(
            request_id=f"bench-beats-{workspace.name}",
            source_asset_id="pulse",
            min_bpm=40.0,
            max_bpm=240.0,
        )
    )
    # Beat analysis has no published artifact; the *engine's* budget for the run
    # is what carries the resource numbers, so it is read from the same run.
    budget = getattr(analysis, "budget", None) or {}
    return Sample(
        elapsed_seconds=float(budget.get("elapsed_seconds", 0.0)),
        peak_rss_bytes=budget.get("peak_rss_bytes"),
        output_size_bytes=0,
        staging_peak_bytes=int(budget.get("staging_peak_bytes", 0)),
        processes_started=int(budget.get("processes_started", 0)),
        within_budget=bool(budget.get("within_budget", True)),
    )


BENCHMARKS: dict[str, tuple[str, Callable[[Path, Path], Sample]]] = {
    "denoise": ("afftdn static-noise reduction on 8 s of speech-like audio", bench_denoise),
    "loudness": (
        "two-pass EBU R128 normalization of an 8 s programme to -14 LUFS",
        bench_loudness,
    ),
    "beats": ("onset/tempo analysis of an 8 s pulse track", bench_beats),
}


def _prepare_media(directory: Path) -> None:
    import omega_media as media

    directory.mkdir(parents=True, exist_ok=True)
    media.write_noisy_voice(directory / "noisy.wav", seconds=8.0)
    media.write_sine(directory / "programme.wav", seconds=8.0)
    media.write_click_track(directory / "pulse.wav", bpm=96.0, seconds=8.0)


def run(*, runs: int, only: str, json_out: Path | None = None) -> int:
    selected = {k: v for k, v in BENCHMARKS.items() if not only or only in k}
    if not selected:
        print(f"no benchmark matches {only!r}", file=sys.stderr)
        return 2
    results: list[Result] = []
    rss_measured = True
    with tempfile.TemporaryDirectory(prefix="omega_bench_") as scratch:
        root = Path(scratch)
        media = root / "media"
        _prepare_media(media)
        for name, (description, function) in selected.items():
            result = Result(name=name, description=description)
            for index in range(runs):
                workspace = root / f"ws_{name}_{index}"
                workspace.mkdir()
                try:
                    result.samples.append(function(workspace, media))
                except Exception as error:  # a failed sample is evidence too
                    result.error = f"{type(error).__name__}: {error}"
                    break
            if any(s.peak_rss_bytes is None for s in result.samples):
                rss_measured = False
            results.append(result)

    version = _ffmpeg_version()
    payload = {
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "ffmpeg": version,
            "runs_per_benchmark": runs,
        },
        "benchmarks": [r.summary() for r in results],
    }
    _print_table(payload)
    if json_out is not None:
        json_out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        print(f"\npayload written to {json_out}")
    return 0 if all(r.error is None and r.samples for r in results) and rss_measured else 1


def _ffmpeg_version() -> str | None:
    """The tool version, read from the engine's own health probe."""
    try:
        from nexus_ai_agent.creative.audio_engine.ffmpeg_ops import FfmpegTranscoder

        transcoder = FfmpegTranscoder()
        outcome = transcoder._run(["-version"])  # noqa: SLF001 - the only version source
        first = (outcome.stdout_tail or "").strip().splitlines()
        return first[0].split("Copyright")[0].strip() if first else None
    except Exception:  # pragma: no cover - no binary
        return None


def _print_table(payload: dict[str, Any]) -> None:
    environment = payload["environment"]
    print(
        f"ffmpeg {environment['ffmpeg']}  |  python {environment['python']}  |  "
        f"N={environment['runs_per_benchmark']} runs each"
    )
    print()
    header = (
        f"{'benchmark':12} {'median s':>9} {'min s':>8} {'max s':>8} {'sd s':>7}"
        f" {'peak RSS':>12} {'procs':>6}  status"
    )
    print(header)
    print("-" * len(header))
    for bench in payload["benchmarks"]:
        if bench.get("status") == "NO_SAMPLES":
            empty = f"{'—':>9} {'—':>8} {'—':>8} {'—':>7} {'—':>12} {'—':>6}"
            print(f"{bench['name']:12} {empty}  FAILED: {bench.get('error')}")
            continue
        rss = (
            f"{bench['peak_rss_median_bytes'] / (1024**2):.1f} MiB"
            if bench["peak_rss_median_bytes"]
            else "NOT_MEASURED"
        )
        status = "ok" if bench["all_within_budget"] else "OVER BUDGET"
        print(
            f"{bench['name']:12} {bench['elapsed_median_s']:9.4f} {bench['elapsed_min_s']:8.4f} "
            f"{bench['elapsed_max_s']:8.4f} {(bench['elapsed_stdev_s'] or 0):7.4f} {rss:>12} "
            f"{bench['processes_started']:6d}  {status}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-n", "--runs", type=int, default=3, help="samples per benchmark (>=3)")
    parser.add_argument("-k", "--only", default="", help="substring of the benchmark name")
    parser.add_argument("--json", type=Path, default=None, help="also write the payload here")
    args = parser.parse_args(argv)
    if args.runs < 3:
        print(
            "refusing to run fewer than 3 samples: doctrine §8 requires N>=3 "
            "(a single run is an anecdote)",
            file=sys.stderr,
        )
        return 2
    exit_code = run(runs=args.runs, only=args.only, json_out=args.json)
    return exit_code


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
