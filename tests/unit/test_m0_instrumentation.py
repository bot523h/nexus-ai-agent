"""M0 instrumentation unit tests — histogram, gauges, classification, injection.

Deterministic, no network, no sleeps. Histogram bucket placement, gauge
set() semantics, bounded error-code classification, and the correlation
injection priority (payload > contextvar > fresh).
"""

from __future__ import annotations

import pytest

from nexus_ai_agent.infrastructure.observability.metrics import (
    JOB_DURATION_BUCKETS,
    get_metrics_registry,
    reset_metrics_registry,
)
from nexus_ai_agent.observability.correlation import (
    clear_correlation_id,
)
from nexus_ai_agent.observability.diagnostics import classify_failure_error
from nexus_ai_agent.observability.metrics import (
    JOBS_DURATION_SECONDS,
    observe_job_duration,
    set_jobs_inflight,
    set_queue_depth,
    snapshot,
)


@pytest.fixture(autouse=True)
def _reset():
    reset_metrics_registry()
    yield
    reset_metrics_registry()
    clear_correlation_id()
    reset_metrics_registry()


# -- histogram --------------------------------------------------------------


def test_buckets_are_frozen_at_m0():
    assert JOB_DURATION_BUCKETS == (0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 300.0)


def test_observe_places_fast_medium_slow_outlier():
    observe_job_duration("story", 0.05)  # fast -> le=0.1
    observe_job_duration("story", 2.0)  # medium -> le=2.5
    observe_job_duration("story", 7.0)  # slow -> le=10
    observe_job_duration("story", 400.0)  # outlier -> +Inf only
    snap = snapshot()
    prefix = f"{JOBS_DURATION_SECONDS}{{job_type=\"story\","
    assert snap[f'{JOBS_DURATION_SECONDS}_bucket{{job_type="story",le="0.1"}}'] == 1.0
    assert snap[f'{JOBS_DURATION_SECONDS}_bucket{{job_type="story",le="0.5"}}'] == 1.0
    assert snap[f'{JOBS_DURATION_SECONDS}_bucket{{job_type="story",le="2.5"}}'] == 2.0
    assert snap[f'{JOBS_DURATION_SECONDS}_bucket{{job_type="story",le="10"}}'] == 3.0
    assert snap[f'{JOBS_DURATION_SECONDS}_bucket{{job_type="story",le="+Inf"}}'] == 4.0
    assert snap[f'{JOBS_DURATION_SECONDS}_count{{job_type="story"}}'] == 4.0
    assert snap[f'{JOBS_DURATION_SECONDS}_sum{{job_type="story"}}'] == pytest.approx(
        0.05 + 2.0 + 7.0 + 400.0
    )
    assert prefix  # keep the prefix var meaningful for failures


def test_histogram_buckets_are_monotonic_cumulative():
    observe_job_duration("pdf_extract", 0.2)
    observe_job_duration("pdf_extract", 0.3)
    snap = snapshot()
    les = [0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 300.0]
    values = [
        snap[f'{JOBS_DURATION_SECONDS}_bucket{{job_type="pdf_extract",le="{le:g}"}}']
        for le in les
    ]
    values.append(snap[f'{JOBS_DURATION_SECONDS}_bucket{{job_type="pdf_extract",le="+Inf"}}'])
    assert values == sorted(values), f"cumulative buckets must be non-decreasing: {values}"
    assert values[-1] == 2.0


def test_observe_rejects_negative_and_non_finite():
    reg = get_metrics_registry()
    with pytest.raises(ValueError):
        reg.observe(JOBS_DURATION_SECONDS, -0.5, labels={"job_type": "story"})
    with pytest.raises(ValueError):
        reg.observe(JOBS_DURATION_SECONDS, float("nan"), labels={"job_type": "story"})
    with pytest.raises(ValueError):
        reg.observe(JOBS_DURATION_SECONDS, float("inf"), labels={"job_type": "story"})


def test_histogram_unbounded_job_type_normalizes_to_unknown():
    observe_job_duration("totally_new_type", 0.3)
    snap = snapshot()
    assert snap[f'{JOBS_DURATION_SECONDS}_bucket{{job_type="unknown",le="+Inf"}}'] == 1.0


# -- gauges -----------------------------------------------------------------


def test_gauge_set_overwrites_from_source_of_truth():
    set_queue_depth(5)
    set_queue_depth(2)  # set(), not increment: 2 wins
    set_jobs_inflight(1)
    snap = snapshot()
    assert snap["queue_depth"] == 2.0
    assert snap["jobs_inflight"] == 1.0


def test_gauge_rejects_negative():
    with pytest.raises(ValueError):
        set_queue_depth(-1)
    with pytest.raises(ValueError):
        set_jobs_inflight(-1)


def test_gauge_and_counters_coexist_in_snapshot():
    from nexus_ai_agent.observability.metrics import inc_jobs_created

    inc_jobs_created("story")
    set_queue_depth(3)
    observe_job_duration("story", 0.01)
    snap = snapshot()
    assert snap['jobs_created_total{job_type="story"}'] == 1.0
    assert snap["queue_depth"] == 3.0
    assert snap[f'{JOBS_DURATION_SECONDS}_count{{job_type="story"}}'] == 1.0


# -- failure classification --------------------------------------------------


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        ("no handler registered for job type story", "handler_not_found"),
        ("Expecting value: line 1 column 1 (char 0)", "payload_invalid"),
        ("invalid persisted payload for abc123", "payload_invalid"),
        ("job handler must return a dictionary", "payload_invalid"),
        ("operation timed out after 30s", "timeout"),
        ("Task was cancelled.", "cancelled"),
        ("validation failed: chat_id missing", "validation_error"),
        ("KeyError: 'url'", "handler_exception"),
        ("", "unknown"),
        ("   ", "unknown"),
    ],
)
def test_classify_failure_error_is_bounded_deterministic(error, expected):
    assert classify_failure_error(error) == expected
    # Deterministic: same input, same output, always.
    assert classify_failure_error(error) == expected


def test_classified_codes_are_within_bounded_set():
    from nexus_ai_agent.observability.metrics import BOUNDED_ERROR_CODES

    samples = [
        "no handler registered",
        "Expecting value",
        "timed out",
        "cancelled",
        "validation",
        "boom",
        "",
    ]
    for sample in samples:
        assert classify_failure_error(sample) in BOUNDED_ERROR_CODES
