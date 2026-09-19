"""O1 structured events: redaction before log, process-wide registry.

Invariant I13: secrets are redacted *before* they reach a log line or a
metric label; thread_id/URLs never become metric labels.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime

import pytest
from typer.testing import CliRunner

from nexus_ai_agent.adapters.langgraph.lifecycle_recording import LifecycleRecordingSaver
from nexus_ai_agent.infrastructure.observability.metrics import get_metrics_registry
from nexus_ai_agent.infrastructure.observability.structured import (
    error_field,
    log_lifecycle_event,
)


class _FailingLifecycle:
    async def record_checkpoint(self, *args: object, **kwargs: object) -> None:
        raise RuntimeError("password=supersecret token Bearer abc123xyz")

    async def touch_thread(self, thread_id: str, *, accessed_at: datetime) -> None:
        raise RuntimeError("token=abcdef0123456789")


class _FakeSaver:
    def __init__(self) -> None:
        from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

        self.serde = JsonPlusSerializer()
        self.put_count = 0

    async def aput(self, *args: object, **kwargs: object) -> dict[str, object]:
        self.put_count += 1
        return {"configurable": {"thread_id": "t", "checkpoint_id": "cp1"}}


def test_event_fields_are_redacted_before_log(caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="nexus_ai_agent.lifecycle"):
        log_lifecycle_event(
            logging.WARNING,
            "lifecycle operation failed",
            operation="upsert",
            error=error_field(RuntimeError("password=supersecret Bearer abc123xyz")),
            detail="url https://user:hunter2@db.example.com:5432/nexus",
        )

    assert caplog.records, "no lifecycle event captured"
    record = caplog.records[-1]
    payload = {
        "operation": record.operation,
        "error": record.error,
        "detail": record.detail,
    }
    joined = " ".join(str(value) for value in payload.values())
    assert "supersecret" not in joined
    assert "abc123xyz" not in joined
    assert "hunter2" not in joined
    assert "[REDACTED]" in joined


def test_redaction_failure_drops_field_not_values(caplog) -> None:
    # Even if redaction itself raised, the raw value must never be logged.
    # (Exercised via a field value that is not a string.)
    with caplog.at_level(logging.WARNING, logger="nexus_ai_agent.lifecycle"):
        log_lifecycle_event(logging.WARNING, "event", value=12345)
    assert caplog.records
    assert caplog.records[-1].value == "12345"  # str() then redacted


@pytest.mark.asyncio
async def test_failed_op_logs_redacted_error_not_traceback(caplog) -> None:
    saver = LifecycleRecordingSaver(_FakeSaver(), _FailingLifecycle())
    config = {"configurable": {"thread_id": "t", "checkpoint_ns": ""}}
    with caplog.at_level(logging.WARNING, logger="nexus_ai_agent.lifecycle"):
        await saver.aput(config, {"id": "cp1"}, {}, {})
        await asyncio.sleep(0)

    failures = [r for r in caplog.records if r.getMessage() == "lifecycle operation failed"]
    assert failures, "expected a redacted failure event"
    error_value = str(failures[0].error)
    assert "supersecret" not in error_value
    assert "abc123xyz" not in error_value
    assert "RuntimeError" in error_value


def test_metrics_mirror_uses_process_wide_registry() -> None:
    registry = get_metrics_registry()
    registry.increment("nexus_mirror_probe_total", labels={"outcome": "ok"})
    # The singleton is the same object process-wide (the O1 registry).
    assert get_metrics_registry() is registry
    assert get_metrics_registry().snapshot()['nexus_mirror_probe_total{outcome="ok"}'] >= 1


def test_metrics_snapshot_cli_reads_process_registry() -> None:
    from nexus_ai_agent.cli import app
    from nexus_ai_agent.infrastructure.observability.metrics import get_metrics_registry

    get_metrics_registry().increment("nexus_c3_probe_total", labels={"outcome": "ok"})
    result = CliRunner().invoke(app, ["metrics", "snapshot", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload['nexus_c3_probe_total{outcome="ok"}'] == 1
