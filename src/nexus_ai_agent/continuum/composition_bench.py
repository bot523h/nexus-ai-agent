"""The measurement core of the composition benchmark.

It lives in the *package* (not in ``scripts/``) because
``tests/architecture/test_scripts_import_boundary.py`` keeps the ``scripts/``
namespace out of the installed distribution — a benchmark the CI cannot import
is a benchmark that silently stops running.

It lives in ``nexus_ai_agent.continuum`` — the repo-truth tooling package —
for the same reason ``continuum/pack_coverage.py`` does: the benchmark
**imports the canonical queue** to measure it, and the composition package
itself must not import a queue at all (ADR 0014).  A measurement harness is
not an owner.

The measurement is deliberately **end to end**: compile, queue, worker
start-up, media execution and independent verification are timed separately, so
a regression can be attributed instead of merely observed.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import statistics
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from nexus_ai_agent.creative.composition.compiler import CompositionCompiler
from nexus_ai_agent.creative.composition.contracts import (
    CompositionPlan,
    ExpectedOutput,
    ExternalInput,
    PlanNode,
    PortBinding,
)
from nexus_ai_agent.creative.composition.executor import CompositionExecutor
from nexus_ai_agent.creative.composition.proof import verify_media_semantics
from nexus_ai_agent.creative.packs.runtime import build_runtime_registry

RESIZE = "media.resize_image"
GRAYSCALE = "media.grayscale_image"
IMAGE_TO_VIDEO = "media.image_to_video"


def _plan(*, width: int, height: int, duration_us: int, fps: int) -> CompositionPlan:
    registry = build_runtime_registry()

    def pin(operation: str) -> tuple[str, str, int]:
        descriptor = registry.describe(operation)
        return descriptor.capability_id, descriptor.version, descriptor.operation_schema_version

    rc, rv, rs = pin(RESIZE)
    gc, gv, gs = pin(GRAYSCALE)
    vc, vv, vs = pin(IMAGE_TO_VIDEO)
    return CompositionPlan(
        nodes=(
            PlanNode(
                node_id="resize",
                operation=RESIZE,
                capability_id=rc,
                capability_version=rv,
                operation_schema_version=rs,
                params={"width": width, "height": height},
                bindings=(PortBinding(input_port="source", source="external", external="src"),),
            ),
            PlanNode(
                node_id="gray",
                operation=GRAYSCALE,
                capability_id=gc,
                capability_version=gv,
                operation_schema_version=gs,
                params={},
                bindings=(
                    PortBinding(
                        input_port="source",
                        source="node_output",
                        node_id="resize",
                        output_port="resized",
                    ),
                ),
            ),
            PlanNode(
                node_id="video",
                operation=IMAGE_TO_VIDEO,
                capability_id=vc,
                capability_version=vv,
                operation_schema_version=vs,
                params={
                    "width": width,
                    "height": height,
                    "duration_us": duration_us,
                    "fps": fps,
                    "encoder": "libx264",
                },
                bindings=(
                    PortBinding(
                        input_port="source",
                        source="node_output",
                        node_id="gray",
                        output_port="grayscale",
                    ),
                ),
            ),
        ),
        external_inputs=(ExternalInput(name="src", media_kind="image"),),
        expected_outputs=(ExpectedOutput(name="final", node_id="video", output_port="video"),),
    )


def _source_image(path: Path, size: tuple[int, int] = (320, 240)) -> Path:
    from PIL import Image

    Image.new("RGB", size, (200, 60, 30)).save(path)
    return path


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
    return ordered[index]


@contextmanager
def _creative_root(root: Path) -> Iterator[None]:
    """Point the canonical creative root at *root* for the duration.

    The benchmark must use the **canonical** root check, so it configures the
    root rather than bypassing it.  The setting is cached, so the cache is
    cleared on both sides of the block.
    """
    from nexus_ai_agent.config.settings import get_settings

    previous = os.environ.get("CREATIVE_TEMP_DIR")
    os.environ["CREATIVE_TEMP_DIR"] = str(root)
    get_settings.cache_clear()
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("CREATIVE_TEMP_DIR", None)
        else:
            os.environ["CREATIVE_TEMP_DIR"] = previous
        get_settings.cache_clear()


def _summarise(name: str, values: list[float]) -> dict[str, float]:
    if not values:
        return {"mean_ms": 0.0, "median_ms": 0.0, "p95_ms": 0.0, "min_ms": 0.0, "max_ms": 0.0}
    return {
        "mean_ms": round(statistics.fmean(values), 3),
        "median_ms": round(statistics.median(values), 3),
        "p95_ms": round(_percentile(values, 0.95), 3),
        "min_ms": round(min(values), 3),
        "max_ms": round(max(values), 3),
    }


def bench_composition(
    *,
    runs: int = 10,
    width: int = 1280,
    height: int = 720,
    duration_us: int = 1_000_000,
    fps: int = 25,
) -> dict[str, Any]:
    """Measure the executor path (compile → execute → verify) ``runs`` times."""
    compile_ms: list[float] = []
    execute_ms: list[float] = []
    verify_ms: list[float] = []
    total_ms: list[float] = []
    output_sizes: list[int] = []
    fingerprints: list[str] = []
    failures = 0

    with tempfile.TemporaryDirectory(prefix="nagar-composition-bench-") as raw:
        root = Path(raw)
        source = _source_image(root / "source.png")
        plan = _plan(width=width, height=height, duration_us=duration_us, fps=fps)

        for index in range(runs):
            workspace = root / f"run_{index}"
            started = time.monotonic()
            try:
                compile_started = time.monotonic()
                compiled = CompositionCompiler().compile(plan)
                compile_ms.append((time.monotonic() - compile_started) * 1000.0)
                fingerprints.append(compiled.fingerprint)

                execute_started = time.monotonic()
                result = CompositionExecutor(confirmed=True).execute(
                    compiled,
                    external_inputs={"src": source},
                    workspace=workspace,
                )
                execute_ms.append((time.monotonic() - execute_started) * 1000.0)

                verify_started = time.monotonic()
                artifact = result.artifacts["final"]
                report = verify_media_semantics(
                    Path(artifact.output_path),
                    compiled.spec("video").media_output("video").contract,
                    expected_width=width,
                    expected_height=height,
                    expected_duration_us=duration_us,
                    expected_fps=fps,
                )
                verify_ms.append((time.monotonic() - verify_started) * 1000.0)
                if not report.verified:
                    failures += 1
                output_sizes.append(artifact.output_size_bytes)
            except Exception:  # noqa: BLE001 - a failure is a measured fact
                failures += 1
            finally:
                total_ms.append((time.monotonic() - started) * 1000.0)

    return {
        "runs": runs,
        "failures": failures,
        "failure_rate": round(failures / max(1, runs), 4),
        "node_count": 3,
        "compile": _summarise("compile", compile_ms),
        "execute": _summarise("execute", execute_ms),
        "verify": _summarise("verify", verify_ms),
        "total": _summarise("total", total_ms),
        "output_size_bytes": {
            "mean": round(statistics.fmean(output_sizes), 1) if output_sizes else 0.0,
            "max": max(output_sizes) if output_sizes else 0,
        },
        "reproducible_fingerprint": len(set(fingerprints)) == 1,
        "plan_fingerprint": fingerprints[0] if fingerprints else "",
    }


def bench_composition_through_queue(
    *,
    runs: int = 10,
    width: int = 1280,
    height: int = 720,
    duration_us: int = 1_000_000,
    fps: int = 25,
) -> dict[str, Any]:
    """Measure the **canonical** path: queue → worker → artifact → verification."""
    from nexus_ai_agent.adapters.in_process_job_queue import InProcessJobQueue
    from nexus_ai_agent.application.ports.job_queue import JobStatus
    from nexus_ai_agent.creative.composition.job import (
        COMPOSITION_JOB_TYPE,
        plan_to_dict,
    )
    from nexus_ai_agent.worker import default_job_handlers

    compile_ms: list[float] = []
    queue_ms: list[float] = []
    worker_ms: list[float] = []
    verify_ms: list[float] = []
    total_ms: list[float] = []
    output_sizes: list[int] = []
    failures = 0
    fingerprints: list[str] = []

    async def _run() -> dict[str, Any]:
        with tempfile.TemporaryDirectory(prefix="nagar-composition-queue-") as raw:
            root = Path(raw)
            creative_root = root / "creative"
            creative_root.mkdir()
            workspace = creative_root / "creative_bench"
            workspace.mkdir()
            source = _source_image(workspace / "source.png")
            digest = hashlib.sha256(source.read_bytes()).hexdigest()

            # The canonical worker re-checks the workspace against the
            # **configured** creative root, so the benchmark must point the
            # configuration at the same root it uses.  This is the canonical
            # root, not a private one: the worker still validates it.
            with _creative_root(creative_root):
                compile_started = time.monotonic()
                plan = _plan(width=width, height=height, duration_us=duration_us, fps=fps)
                compiled = CompositionCompiler().compile(plan)
                compile_ms.append((time.monotonic() - compile_started) * 1000.0)
                fingerprints.append(compiled.fingerprint)

                queue = InProcessJobQueue(root / "jobs.sqlite3")
                for job_type, handler in default_job_handlers().items():
                    queue.register_handler(job_type, handler)

                payload: dict[str, Any] = {
                    "plan": plan_to_dict(plan),
                    "external_inputs": {"src": {"path": str(source), "sha256": digest}},
                    "workspace_dir": str(workspace),
                    "idempotency_key": "composition:bench",
                }
                enqueue_started = time.monotonic()
                job_id = await queue.enqueue(
                    job_type=COMPOSITION_JOB_TYPE,
                    idempotency_key="composition:bench",
                    payload=payload,
                )
                queue_ms.append((time.monotonic() - enqueue_started) * 1000.0)

                worker_started = time.monotonic()
                terminal = {
                    JobStatus.COMPLETED,
                    JobStatus.FAILED_RETRYABLE,
                    JobStatus.FAILED_TERMINAL,
                }
                for _ in range(1200):
                    status = await queue.get_status(job_id)
                    if status in terminal:
                        break
                    await asyncio.sleep(0.02)
                worker_ms.append((time.monotonic() - worker_started) * 1000.0)
                result = await queue.get_result(job_id)
                if result is None or result.get("success") is not True:
                    return {"ok": False}
                output_sizes.append(int(str(result["size_bytes"])))

                # The verification stage is measured **independently** of the
                # worker: this harness re-measures the produced artifact with
                # its own decoder, exactly as the queue's registered verifier
                # does.  It is not the worker's own report.
                verify_started = time.monotonic()
                report = verify_media_semantics(
                    Path(str(result["artifact_path"])),
                    compiled.spec("video").media_output("video").contract,
                    expected_width=width,
                    expected_height=height,
                    expected_duration_us=duration_us,
                    expected_fps=fps,
                )
                verify_ms.append((time.monotonic() - verify_started) * 1000.0)
                if not report.verified:
                    return {"ok": False}
                return {"ok": True}

    for _ in range(runs):
        started = time.monotonic()
        outcome = asyncio.run(_run())
        if not outcome["ok"]:
            failures += 1
        total_ms.append((time.monotonic() - started) * 1000.0)

    return {
        "runs": runs,
        "failures": failures,
        "failure_rate": round(failures / max(1, runs), 4),
        "node_count": 3,
        "compile": _summarise("compile", compile_ms),
        "queue": _summarise("queue", queue_ms),
        "worker": _summarise("worker", worker_ms),
        "verify": _summarise("verify", verify_ms),
        "total": _summarise("total", total_ms),
        "output_size_bytes": {
            "mean": round(statistics.fmean(output_sizes), 1) if output_sizes else 0.0,
            "max": max(output_sizes) if output_sizes else 0,
        },
        "reproducible_fingerprint": len(set(fingerprints)) == 1,
        "plan_fingerprint": fingerprints[0] if fingerprints else "",
    }


__all__ = ["bench_composition", "bench_composition_through_queue"]
