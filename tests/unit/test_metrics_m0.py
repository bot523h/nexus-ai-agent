"""M0 metrics contract tests — deterministic, no network, no sleep."""

import pytest

from nexus_ai_agent.infrastructure.observability.metrics import (
    BOUNDED_JOB_TYPES,
    get_metrics_registry,
    reset_metrics_registry,
)
from nexus_ai_agent.observability.metrics import (
    ALL_JOB_METRICS,
    BOUNDED_ERROR_CODES,
    BOUNDED_REASONS,
    JOBS_CLAIMED_TOTAL,
    JOBS_COMPLETED_TOTAL,
    JOBS_CREATED_TOTAL,
    JOBS_FAILED_TOTAL,
    JOBS_RECOVERED_TOTAL,
    METRIC_CONTRACTS,
    inc_jobs_claimed,
    inc_jobs_completed,
    inc_jobs_created,
    inc_jobs_failed,
    inc_jobs_recovered,
)


@pytest.fixture(autouse=True)
def _reset():
    reset_metrics_registry()
    yield
    reset_metrics_registry()


def test_canonical_metrics_defined():
    assert JOBS_CREATED_TOTAL == "jobs_created_total"
    assert JOBS_CLAIMED_TOTAL == "jobs_claimed_total"
    assert JOBS_COMPLETED_TOTAL == "jobs_completed_total"
    assert JOBS_FAILED_TOTAL == "jobs_failed_total"
    assert JOBS_RECOVERED_TOTAL == "jobs_recovered_total"
    assert len(ALL_JOB_METRICS) == 5
    for name in ALL_JOB_METRICS:
        assert name in METRIC_CONTRACTS


def test_metric_contracts_have_required_fields():
    for name, contract in METRIC_CONTRACTS.items():
        assert contract.name == name
        assert contract.description
        assert contract.labels
        assert contract.cardinality_policy
        assert contract.source_event
        assert contract.lifecycle
        # No high-cardinality hints
        low = contract.cardinality_policy.lower()
        assert "telegram_id" not in low or "no telegram_id" in low
        assert "no high-cardinality" in low or "bounded" in low or "no telegram_id" in low


def test_jobs_created_increment():
    inc_jobs_created("pdf_extract")
    reg = get_metrics_registry()
    assert reg.get(JOBS_CREATED_TOTAL, {"job_type": "pdf_extract"}) == 1
    inc_jobs_created("pdf_extract")
    assert reg.get(JOBS_CREATED_TOTAL, {"job_type": "pdf_extract"}) == 2


def test_jobs_claimed_increment():
    inc_jobs_claimed("slideshow_render")
    reg = get_metrics_registry()
    assert reg.get(JOBS_CLAIMED_TOTAL, {"job_type": "slideshow_render"}) == 1


def test_jobs_completed_increment():
    inc_jobs_completed("story")
    reg = get_metrics_registry()
    assert reg.get(JOBS_COMPLETED_TOTAL, {"job_type": "story"}) == 1


def test_jobs_failed_increment_with_error_code():
    inc_jobs_failed("pdf_extract", error_code="handler_exception")
    reg = get_metrics_registry()
    assert (
        reg.get(JOBS_FAILED_TOTAL, {"job_type": "pdf_extract", "error_code": "handler_exception"})
        == 1
    )


def test_jobs_failed_normalizes_unknown_error_code():
    inc_jobs_failed("pdf_extract", error_code="some_weird_random_code_12345")
    reg = get_metrics_registry()
    # Unknown error_code normalized to "unknown"
    assert reg.get(JOBS_FAILED_TOTAL, {"job_type": "pdf_extract", "error_code": "unknown"}) == 1


def test_jobs_recovered_increment():
    inc_jobs_recovered("expired_lease")
    reg = get_metrics_registry()
    assert reg.get(JOBS_RECOVERED_TOTAL, {"reason": "expired_lease"}) == 1


def test_jobs_recovered_normalizes_unknown_reason():
    inc_jobs_recovered("some_random_reason")
    reg = get_metrics_registry()
    assert reg.get(JOBS_RECOVERED_TOTAL, {"reason": "unknown"}) == 1


def test_bounded_job_type_normalization():
    inc_jobs_created("totally_unknown_job_type_xyz")
    reg = get_metrics_registry()
    # Unknown job_type normalized to "unknown" to cap cardinality
    assert reg.get(JOBS_CREATED_TOTAL, {"job_type": "unknown"}) == 1


def test_high_cardinality_label_rejected():
    reg = get_metrics_registry()
    with pytest.raises(ValueError):
        reg.increment("jobs_created_total", labels={"job_type": "https://evil.com/steal"})
    with pytest.raises(ValueError):
        reg.increment("jobs_created_total", labels={"job_type": "@someone"})
    with pytest.raises(ValueError):
        reg.increment("jobs_created_total", labels={"job_type": "a" * 65})


def test_unsupported_label_rejected():
    reg = get_metrics_registry()
    with pytest.raises(ValueError):
        reg.increment("jobs_created_total", labels={"telegram_id": "12345"})
    with pytest.raises(ValueError):
        reg.increment("jobs_created_total", labels={"url": "https://example.com"})
    with pytest.raises(ValueError):
        reg.increment("jobs_created_total", labels={"random_uuid": "abc-123"})


def test_snapshot_contains_all_metrics():
    inc_jobs_created("pdf_extract")
    inc_jobs_claimed("pdf_extract")
    inc_jobs_completed("pdf_extract")
    inc_jobs_failed("pdf_extract", "timeout")
    inc_jobs_recovered("startup_recovery")
    snap = get_metrics_registry().snapshot()
    # Snapshot keys are formatted as name{labels}
    assert any("jobs_created_total" in k for k in snap)
    assert any("jobs_claimed_total" in k for k in snap)
    assert any("jobs_completed_total" in k for k in snap)
    assert any("jobs_failed_total" in k for k in snap)
    assert any("jobs_recovered_total" in k for k in snap)


def test_metrics_lifecycle_monotonic():
    inc_jobs_created("pdf_extract")
    inc_jobs_created("pdf_extract")
    reg = get_metrics_registry()
    first = reg.get(JOBS_CREATED_TOTAL, {"job_type": "pdf_extract"})
    inc_jobs_created("pdf_extract")
    second = reg.get(JOBS_CREATED_TOTAL, {"job_type": "pdf_extract"})
    assert second == first + 1
    assert second > first


def test_no_secret_in_label():
    # Ensure we never allow secret-like labels
    reg = get_metrics_registry()
    with pytest.raises(ValueError):
        reg.increment("test", labels={"api_key": "secret123"})
    with pytest.raises(ValueError):
        reg.increment("test", labels={"token": "abc"})


def test_bounded_sets_are_low_cardinality():
    assert len(BOUNDED_JOB_TYPES) <= 20
    assert len(BOUNDED_ERROR_CODES) <= 20
    assert len(BOUNDED_REASONS) <= 20
    # No UUID, no URL, no telegram_id in bounded sets
    for v in BOUNDED_JOB_TYPES | BOUNDED_ERROR_CODES | BOUNDED_REASONS:
        assert "://" not in v
        assert len(v) <= 64
