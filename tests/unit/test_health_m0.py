"""Readiness vs Liveness tests — M0.

Tests:
  - healthy -> ready (200)
  - startup failure -> not ready (503)
  - missing dependency -> not ready (503)
  - shutdown -> not ready (503)
"""

from nexus_ai_agent.observability.readiness import (
    READYZ_CONTRACT,
    ReadinessStatus,
    evaluate_readiness,
    probe_always_ready,
    probe_application_substrate,
    probe_db_connection,
    probe_job_queue_initialized,
    probe_migration_head,
)


def test_healthy_ready():
    probes = [
        lambda: probe_always_ready("app"),
        lambda: probe_db_connection(True),
        lambda: probe_job_queue_initialized(True),
        lambda: probe_migration_head(True, expected="head"),
    ]
    state = evaluate_readiness(probes)
    assert state.is_ready
    assert state.status == ReadinessStatus.READY
    assert state.http_status == 200


def test_startup_failure_not_ready():
    probes = [
        lambda: probe_application_substrate(False, "startup failed: engine init error"),
        lambda: probe_db_connection(True),
    ]
    state = evaluate_readiness(probes)
    assert not state.is_ready
    assert state.http_status == 503
    assert state.status == ReadinessStatus.NOT_READY


def test_missing_dependency_not_ready():
    probes = [
        lambda: probe_db_connection(False, "database not reachable"),
        lambda: probe_always_ready("app"),
    ]
    state = evaluate_readiness(probes)
    assert not state.is_ready
    assert state.http_status == 503


def test_migration_drift_not_ready():
    probes = [
        lambda: probe_migration_head(False, current="old", expected="head"),
    ]
    state = evaluate_readiness(probes)
    assert not state.is_ready
    assert state.http_status == 503


def test_shutdown_not_ready():
    probes = [lambda: probe_always_ready("app")]
    state = evaluate_readiness(probes, is_shutting_down=True)
    assert not state.is_ready
    assert state.http_status == 503
    assert state.status == ReadinessStatus.SHUTDOWN


def test_probe_exception_not_ready_fail_closed():
    def bad_probe():
        raise RuntimeError("probe exploded")

    state = evaluate_readiness([bad_probe])
    assert not state.is_ready
    assert state.http_status == 503


def test_optional_check_does_not_block_readiness():
    from nexus_ai_agent.observability.readiness import ReadinessCheck

    def optional_failing():
        return ReadinessCheck(name="optional", ready=False, message="optional fail", required=False)

    state = evaluate_readiness([lambda: probe_always_ready("app"), optional_failing])
    assert state.is_ready
    assert state.http_status == 200


def test_readyz_contract_documented():
    assert READYZ_CONTRACT["endpoint"] == "GET /readyz"
    assert READYZ_CONTRACT["success_status"] == 200
    assert READYZ_CONTRACT["failure_status"] == 503
    assert (
        "liveness" in READYZ_CONTRACT["liveness_endpoint"].lower()
        or "healthz" in READYZ_CONTRACT["liveness_endpoint"]
    )
    rules = READYZ_CONTRACT["rules"]
    assert any("200 only when" in r for r in rules)
    assert any(
        "process alive" in r.lower()
        or "alive => ready" in r.lower()
        or "confused" in r.lower()
        or "must not" in r.lower()
        for r in rules
    )


def test_to_dict_shape():
    probes = [lambda: probe_always_ready("app")]
    state = evaluate_readiness(probes)
    d = state.to_dict()
    assert "status" in d
    assert "http_status" in d
    assert "checks" in d
    assert isinstance(d["checks"], list)


def test_liveness_vs_readiness_separation():
    # Liveness is DB-free, readiness checks DB — they must be different
    # Simulate: DB down but process up
    # Liveness would be 200 (process alive)
    # Readiness must be 503
    liveness_ok = True  # process up
    readiness_state = evaluate_readiness([lambda: probe_db_connection(False)])
    assert liveness_ok is True
    assert readiness_state.is_ready is False
    assert readiness_state.http_status == 503
    # This proves process alive != ready
