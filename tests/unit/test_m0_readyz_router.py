"""`/readyz` router tests — real FastAPI TestClient, M0 contract cases.

Proves the router factory against the exact semantics from the M0 contract:
healthy 200; startup failure / missing required dep / migration drift /
shutdown -> 503; optional dep failure -> 200 degraded; raising probe ->
fail-closed 503. api/app.py stays fenced — this mounts the factory on a
throwaway app instead.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus_ai_agent.observability.readiness import ReadinessCheck
from nexus_ai_agent.observability.readyz_router import (
    create_readiness_probes,
    create_readyz_router,
    readiness_probe_from_saturation,
)


def _client(*, flag: dict, probes) -> TestClient:
    app = FastAPI()
    app.include_router(create_readyz_router(probes, is_shutting_down=lambda: bool(flag["down"])))
    return TestClient(app)


def _standard_probes(flag: dict, *, optional_ok: bool = True):
    return create_readiness_probes(
        substrate_ready=lambda: not flag["substrate_down"],
        db_connected=lambda: not flag["db_down"],
        migrations_at_head=lambda: not flag["migrations_drift"],
        job_queue_initialized=lambda: not flag["queue_down"],
        optional_cache_ready=lambda: optional_ok,
    )


def _flag(**over):
    flag = {
        "down": False,
        "substrate_down": False,
        "db_down": False,
        "migrations_drift": False,
        "queue_down": False,
    }
    flag.update(over)
    return flag


def test_healthy_returns_200_with_contract_shape():
    flag = _flag()
    client = _client(flag=flag, probes=_standard_probes(flag))
    resp = client.get("/readyz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ready"
    assert body["http_status"] == 200
    names = {c["name"] for c in body["checks"]}
    assert names == {
        "application_substrate",
        "database",
        "migrations",
        "job_queue",
        "optional_cache",
    }
    for check in body["checks"]:
        assert set(check) == {"name", "ready", "message", "required"}


def test_startup_failure_returns_503():
    flag = _flag(substrate_down=True)
    client = _client(flag=flag, probes=_standard_probes(flag))
    resp = client.get("/readyz")
    assert resp.status_code == 503
    assert resp.json()["status"] == "not_ready"


def test_missing_required_dependency_returns_503():
    flag = _flag(db_down=True)
    client = _client(flag=flag, probes=_standard_probes(flag))
    resp = client.get("/readyz")
    assert resp.status_code == 503
    body = resp.json()
    db_check = next(c for c in body["checks"] if c["name"] == "database")
    assert db_check["ready"] is False and db_check["required"] is True


def test_migration_drift_returns_503():
    flag = _flag(migrations_drift=True)
    client = _client(flag=flag, probes=_standard_probes(flag))
    assert client.get("/readyz").status_code == 503


def test_shutdown_returns_503_and_flips_without_restart():
    flag = _flag()
    client = _client(flag=flag, probes=_standard_probes(flag))
    assert client.get("/readyz").status_code == 200
    flag["down"] = True  # flip BEFORE drain -> immediately 503
    resp = client.get("/readyz")
    assert resp.status_code == 503
    assert resp.json()["status"] == "shutdown"


def test_optional_dependency_failure_stays_200_degraded():
    flag = _flag()
    client = _client(flag=flag, probes=_standard_probes(flag, optional_ok=False))
    resp = client.get("/readyz")
    assert resp.status_code == 200
    body = resp.json()
    cache = next(c for c in body["checks"] if c["name"] == "optional_cache")
    assert cache["ready"] is False and cache["required"] is False


def test_raising_probe_fails_closed_503():
    flag = _flag()

    def _boom() -> ReadinessCheck:
        raise RuntimeError("probe exploded")

    probes = [_boom, *_standard_probes(flag)]
    client = _client(flag=flag, probes=probes)
    resp = client.get("/readyz")
    assert resp.status_code == 503
    body = resp.json()
    boom = next(c for c in body["checks"] if c["name"] == "_boom")
    assert boom["ready"] is False
    assert "RuntimeError" in boom["message"]


def test_response_contains_no_secrets_or_pi():
    flag = _flag()
    client = _client(flag=flag, probes=_standard_probes(flag))
    body = client.get("/readyz").json()
    flat = str(body).lower()
    for needle in ("token", "secret", "password", "api_key", "telegram_id"):
        assert needle not in flat


# -- saturation-backed probe (real queue report) ----------------------------


class _FakeReport:
    def __init__(self, pending, processing, oldest=0.0):
        self.pending = pending
        self.processing = processing
        self.oldest_pending_age_seconds = oldest


def test_saturation_probe_ready_when_queue_healthy():
    probe = readiness_probe_from_saturation(
        lambda: _FakeReport(pending=3, processing=1, oldest=1.0)
    )
    check = probe()
    assert check.ready is True and check.required is True
    assert "pending=3" in check.message and "processing=1" in check.message


def test_saturation_probe_boundary_oldest_pending_age():
    probe = readiness_probe_from_saturation(
        lambda: _FakeReport(pending=1, processing=0, oldest=60.0),
        max_oldest_pending_age_seconds=60.0,
    )
    assert probe().ready is True  # exactly at threshold -> ready (strict >)
    probe_over = readiness_probe_from_saturation(
        lambda: _FakeReport(pending=1, processing=0, oldest=60.0001),
        max_oldest_pending_age_seconds=60.0,
    )
    assert probe_over().ready is False


def test_saturation_probe_read_failure_propagates_for_fail_closed():
    def _explode():
        raise OSError("db gone")

    probe = readiness_probe_from_saturation(_explode)
    with pytest.raises(OSError):
        probe()
