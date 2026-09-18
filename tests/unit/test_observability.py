import pytest

from nexus_ai_agent.infrastructure.observability.metrics import MetricsRegistry
from nexus_ai_agent.infrastructure.observability.redaction import redact


def test_redaction_removes_credentials() -> None:
    value = (
        "Bearer abc123 password=secret https://u:p@example.com/x "
        "123456789:abcdefghijklmnopqrstuvwxyzABCDEFGHIJK"
    )
    result = redact(value)
    assert "abc123" not in result
    assert "secret" not in result
    assert "u:p" not in result
    assert "REDACTED" in result


def test_metrics_allow_only_low_cardinality_labels() -> None:
    metrics = MetricsRegistry()
    metrics.increment("nexus_touch_failures_total", labels={"backend": "sqlite"})
    assert metrics.snapshot()['nexus_touch_failures_total{backend="sqlite"}'] == 1
    with pytest.raises(ValueError):
        metrics.increment("bad", labels={"thread_id": "secret"})
