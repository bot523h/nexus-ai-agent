"""Correlation ID lifecycle tests — M0."""

import pytest

from nexus_ai_agent.observability.correlation import (
    CORRELATION_ID_KEY,
    LIFECYCLE_DOC,
    bind_correlation_id,
    clear_correlation_id,
    deterministic_correlation_id,
    ensure_correlation_id,
    extract_from_payload,
    get_correlation_id,
    inject_into_payload,
    new_correlation_id,
)


@pytest.fixture(autouse=True)
def _clear():
    try:
        clear_correlation_id()
    except Exception:
        pass
    yield
    try:
        clear_correlation_id()
    except Exception:
        pass


def test_new_correlation_id_unique():
    a = new_correlation_id()
    b = new_correlation_id()
    assert a != b
    assert len(a) == 32
    assert all(c in "0123456789abcdef" for c in a)


def test_deterministic_correlation_id_stable():
    seed = "12345"
    a = deterministic_correlation_id(seed)
    b = deterministic_correlation_id(seed)
    assert a == b
    assert len(a) == 32


def test_deterministic_different_seeds_different_ids():
    a = deterministic_correlation_id("100")
    b = deterministic_correlation_id("101")
    assert a != b


def test_deterministic_int_seed():
    a = deterministic_correlation_id(12345)
    b = deterministic_correlation_id("12345")
    assert a == b


def test_bind_and_get_correlation_id():
    cid = new_correlation_id()
    bind_correlation_id(cid)
    assert get_correlation_id() == cid


def test_clear_correlation_id():
    cid = new_correlation_id()
    bind_correlation_id(cid)
    clear_correlation_id()
    assert get_correlation_id() is None


def test_inject_and_extract_payload():
    payload = {"job_type": "pdf_extract", "file_id": "abc"}
    cid = new_correlation_id()
    new_payload = inject_into_payload(payload, cid)
    # Original unchanged
    assert CORRELATION_ID_KEY not in payload
    assert new_payload[CORRELATION_ID_KEY] == cid
    assert extract_from_payload(new_payload) == cid


def test_ensure_correlation_id_generates_if_missing():
    payload = {"a": 1}
    new_payload, cid = ensure_correlation_id(payload)
    assert cid
    assert new_payload[CORRELATION_ID_KEY] == cid


def test_ensure_correlation_id_preserves_existing():
    cid = new_correlation_id()
    payload = {CORRELATION_ID_KEY: cid, "a": 1}
    new_payload, returned = ensure_correlation_id(payload)
    assert returned == cid
    assert new_payload is payload  # same dict when exists


def test_correlation_id_never_secret():
    # Correlation ID is hex, not a token, not an API key, not a bot token shape
    cid = new_correlation_id()
    assert ":" not in cid
    assert len(cid) == 32
    # Should not look like telegram bot token
    assert not cid.startswith("123456")


def test_correlation_id_never_high_cardinality_metric_label():
    # This test guards that correlation_id is NOT allowed as metric label
    from nexus_ai_agent.infrastructure.observability.metrics import get_metrics_registry

    reg = get_metrics_registry()
    cid = new_correlation_id()
    with pytest.raises(ValueError):
        reg.increment("some_metric", labels={"correlation_id": cid})
    with pytest.raises(ValueError):
        reg.increment("some_metric", labels={CORRELATION_ID_KEY: cid})


def test_lifecycle_doc_exists():
    assert "per_update" in LIFECYCLE_DOC
    assert "per_job" in LIFECYCLE_DOC
    assert "per_external_effect" in LIFECYCLE_DOC
    assert "never_secret" in LIFECYCLE_DOC
    assert "never_pii" in LIFECYCLE_DOC
    assert "never_metric_label" in LIFECYCLE_DOC


def test_propagation_via_contextvars():
    cid = new_correlation_id()
    bind_correlation_id(cid)
    # Simulate job creation with correlation_id from context
    payload = {"job_type": "test"}
    current = get_correlation_id()
    assert current == cid
    injected = inject_into_payload(payload, current) if current else payload
    assert injected[CORRELATION_ID_KEY] == cid
