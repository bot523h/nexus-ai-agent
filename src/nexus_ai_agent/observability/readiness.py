"""Readiness vs Liveness contract — M0.

Liveness (/healthz): process is up and accepting requests, touches NO DB, NO engine.
Readiness (/readyz): application substrate is ready, dependencies present, migrations OK.

This module defines readiness checks that can be used by the API layer WITHOUT
stealing its zone. The actual HTTP endpoint lives in api/app.py (core-security zone),
but this module provides the pure domain logic and testable evaluators.

200 only when:
  - application substrate ready (engines initialized, job queue initialized)
  - required dependencies present (DB reachable, migrations at head if DB required)
  - migration/startup invariant holds (no pending migration that would break)

Otherwise 503 or contract existing repo.

Tests:
  - healthy -> ready (200)
  - startup failure -> not ready (503)
  - missing dependency -> not ready (503)
  - shutdown -> not ready (503)

If implementation needs bot/app.py or worker.py, we only provide contract+test+blocker.

Design: no network, no sleep, deterministic, hostile-clock safe.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum


class ReadinessStatus(str, Enum):
    READY = "ready"
    NOT_READY = "not_ready"
    SHUTDOWN = "shutdown"


@dataclass(frozen=True)
class ReadinessCheck:
    """Single readiness probe result."""

    name: str
    ready: bool
    message: str = ""
    # Optional: is this check required for readiness? If False, failure is warning only
    required: bool = True


@dataclass
class ReadinessState:
    """Aggregated readiness state."""

    status: ReadinessStatus
    checks: list[ReadinessCheck] = field(default_factory=list)
    http_status: int = 200

    @property
    def is_ready(self) -> bool:
        return self.status == ReadinessStatus.READY

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "http_status": self.http_status,
            "checks": [
                {"name": c.name, "ready": c.ready, "message": c.message, "required": c.required}
                for c in self.checks
            ],
        }


# Type for a readiness probe function: () -> ReadinessCheck
ProbeFunc = Callable[[], ReadinessCheck]


def evaluate_readiness(
    probes: list[ProbeFunc],
    *,
    is_shutting_down: bool = False,
) -> ReadinessState:
    """Evaluate readiness from a list of probe functions.

    Pure function, no I/O, deterministic.
    - If shutting down -> NOT_READY, 503
    - If any required probe fails -> NOT_READY, 503
    - Otherwise READY, 200
    """
    if is_shutting_down:
        return ReadinessState(
            status=ReadinessStatus.SHUTDOWN,
            checks=[
                ReadinessCheck(name="shutdown", ready=False, message="shutting down", required=True)
            ],
            http_status=503,
        )

    results: list[ReadinessCheck] = []
    for probe in probes:
        try:
            result = probe()
        except Exception as exc:
            # Probe failure = not ready, fail closed
            result = ReadinessCheck(
                name=getattr(probe, "__name__", "unknown_probe"),
                ready=False,
                message=f"probe raised {type(exc).__name__}: {exc}",
                required=True,
            )
        results.append(result)

    # Any required check failing -> not ready
    not_ready = [c for c in results if c.required and not c.ready]
    if not_ready:
        return ReadinessState(
            status=ReadinessStatus.NOT_READY,
            checks=results,
            http_status=503,
        )

    return ReadinessState(
        status=ReadinessStatus.READY,
        checks=results,
        http_status=200,
    )


# --- Built-in probe factories (pure, testable) ---


def probe_always_ready(name: str = "always_ready") -> ReadinessCheck:
    return ReadinessCheck(name=name, ready=True, message="ok")


def probe_always_not_ready(
    name: str = "always_not_ready", reason: str = "not ready"
) -> ReadinessCheck:
    return ReadinessCheck(name=name, ready=False, message=reason, required=True)


def probe_db_connection(is_connected: bool, message: str = "") -> ReadinessCheck:
    return ReadinessCheck(
        name="database",
        ready=is_connected,
        message=message or ("connected" if is_connected else "database not reachable"),
        required=True,
    )


def probe_migration_head(is_at_head: bool, current: str = "", expected: str = "") -> ReadinessCheck:
    if is_at_head:
        return ReadinessCheck(name="migrations", ready=True, message=f"at head {expected}")
    return ReadinessCheck(
        name="migrations",
        ready=False,
        message=f"migration drift: current {current!r} != expected {expected!r}",
        required=True,
    )


def probe_job_queue_initialized(is_initialized: bool) -> ReadinessCheck:
    return ReadinessCheck(
        name="job_queue",
        ready=is_initialized,
        message="initialized" if is_initialized else "job queue not initialized",
        required=True,
    )


def probe_application_substrate(is_ready: bool, message: str = "") -> ReadinessCheck:
    return ReadinessCheck(
        name="application_substrate",
        ready=is_ready,
        message=message or ("substrate ready" if is_ready else "substrate not ready"),
        required=True,
    )


# --- Contract for /readyz endpoint (to be implemented in api/app.py when zone free) ---

READYZ_CONTRACT: dict[str, object] = {
    "endpoint": "GET /readyz",
    "liveness_endpoint": "GET /healthz (DB-free, always 200 if process up)",
    "readiness_endpoint": "GET /readyz (checks dependencies, 200 only if ready)",
    "success_status": 200,
    "failure_status": 503,
    "response_shape": {
        "status": "ready | not_ready | shutdown",
        "http_status": "200 | 503",
        "checks": "[{name, ready, message, required}]",
    },
    "rules": [
        "200 only when substrate ready, deps present, migration holds",
        "503 when startup failure, missing dependency, or shutdown",
        "Must NOT be confused with liveness (alive=>ready is WRONG)",
        "DB check must be bounded (no hanging), fail closed on error",
        "No secret, no PII in response",
    ],
    "blocked_reason": (
        "api/app.py is in core-security zone, not owned by observability; "
        "this module provides pure contract and tests, endpoint needs "
        "coordination with core-security owner"
    ),
}


__all__ = [
    "ProbeFunc",
    "READYZ_CONTRACT",
    "ReadinessCheck",
    "ReadinessState",
    "ReadinessStatus",
    "evaluate_readiness",
    "probe_always_not_ready",
    "probe_always_ready",
    "probe_application_substrate",
    "probe_db_connection",
    "probe_job_queue_initialized",
    "probe_migration_head",
]
