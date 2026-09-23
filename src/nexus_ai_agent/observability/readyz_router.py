"""`/readyz` router factory — M0 readiness over HTTP without stealing `api/`.

The endpoint itself lives in `api/app.py` (core-security zone, fenced). This
module provides the complete, tested handler as a FastAPI router factory so
wiring the real app is a single ``app.include_router(...)`` call once that
zone frees — the contract, not a stub.

Semantics (M0 contract, proven in ``tests/unit/test_m0_readyz_router.py``):

* healthy substrate + required deps present + migrations at head → **200**
* startup failure (substrate not ready) → **503**
* missing required dependency → **503**
* migration drift → **503**
* shutdown flag set → **503** (set the flag *before* draining; probes are
  re-evaluated per request so the flip is immediate, no caching)
* optional dependency failing → **200 degraded** (``required=false`` check
  reported in the body; liveness must not be deep — readiness stays shallow)
* a probe that raises → fail-closed **503** (``evaluate_readiness`` contract)

fastapi is imported lazily inside the factory: importing this module in a
non-HTTP context (CLI, tests of the pure evaluators) must not require the
web stack. Response body is exactly ``READYZ_CONTRACT["response_shape"]`` —
no secrets, no PII, bounded messages.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from nexus_ai_agent.observability.readiness import READYZ_CONTRACT, ProbeFunc, evaluate_readiness


def create_readyz_router(
    probes: Sequence[ProbeFunc],
    *,
    is_shutting_down: Callable[[], bool],
) -> Any:
    """Build a FastAPI APIRouter exposing ``GET /readyz``.

    Args:
        probes: readiness probe callables (pure, cheap, no network). Each
            returns a :class:`ReadinessCheck`; ``required=False`` checks
            degrade without failing readiness.
        is_shutting_down: consulted on **every** request — flip it before
            starting the drain so load balancers see 503 immediately.
    """
    from fastapi import APIRouter
    from fastapi.responses import JSONResponse

    router = APIRouter()

    @router.get("/readyz")
    async def readyz() -> JSONResponse:
        state = evaluate_readiness(list(probes), is_shutting_down=is_shutting_down())
        return JSONResponse(status_code=state.http_status, content=state.to_dict())

    return router


def create_readiness_probes(
    *,
    substrate_ready: Callable[[], bool],
    db_connected: Callable[[], bool],
    migrations_at_head: Callable[[], bool],
    job_queue_initialized: Callable[[], bool],
    optional_cache_ready: Callable[[], bool] | None = None,
) -> list[ProbeFunc]:
    """The standard M0 probe set: substrate, db, migrations, queue (+ optional).

    All callables are evaluated lazily per request; keep them shallow
    (SELECT-1 style) — liveness stays DB-free, readiness carries the deps.
    """
    from nexus_ai_agent.observability.readiness import (
        probe_application_substrate,
        probe_db_connection,
        probe_job_queue_initialized,
        probe_migration_head,
    )

    probes: list[ProbeFunc] = [
        lambda: probe_application_substrate(substrate_ready()),
        lambda: probe_db_connection(db_connected()),
        lambda: probe_migration_head(migrations_at_head()),
        lambda: probe_job_queue_initialized(job_queue_initialized()),
    ]
    if optional_cache_ready is not None:
        from nexus_ai_agent.observability.readiness import ReadinessCheck

        def _cache_probe() -> ReadinessCheck:
            ready = optional_cache_ready()
            return ReadinessCheck(
                name="optional_cache",
                ready=ready,
                message="cache warm" if ready else "cache cold (optional)",
                required=False,
            )

        probes.append(_cache_probe)
    return probes


def readiness_probe_from_saturation(
    read_saturation: Callable[[], Any],
    *,
    max_oldest_pending_age_seconds: float | None = None,
    now: Callable[[], Any] | None = None,
) -> ProbeFunc:
    """Readiness probe backed by the **real** queue saturation report.

    Ready while the saturation source is readable and (optionally) the oldest
    pending row is younger than ``max_oldest_pending_age_seconds``. A read
    that raises fails closed (503) via ``evaluate_readiness``.
    """
    from nexus_ai_agent.observability.readiness import ReadinessCheck

    def _probe() -> ReadinessCheck:
        report = read_saturation()  # may raise -> fail closed upstream
        if max_oldest_pending_age_seconds is not None:
            age = float(getattr(report, "oldest_pending_age_seconds", 0.0))
            if age > max_oldest_pending_age_seconds:
                return ReadinessCheck(
                    name="queue_saturation",
                    ready=False,
                    message=f"oldest pending job {age:.0f}s exceeds "
                    f"{max_oldest_pending_age_seconds:.0f}s",
                    required=True,
                )
        pending = getattr(report, "pending", "?")
        processing = getattr(report, "processing", "?")
        return ReadinessCheck(
            name="queue_saturation",
            ready=True,
            message=f"pending={pending} processing={processing}",
            required=True,
        )

    _probe.__name__ = "probe_queue_saturation"
    return _probe


def describe_readyz_contract() -> dict[str, object]:
    """The frozen contract for docs/tests — endpoint, statuses, shape, rules."""
    return dict(READYZ_CONTRACT)


__all__ = [
    "create_readyz_router",
    "create_readiness_probes",
    "describe_readyz_contract",
    "readiness_probe_from_saturation",
]
