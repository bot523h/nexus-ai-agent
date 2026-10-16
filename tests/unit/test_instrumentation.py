"""Behaviour of the ``instrumented`` decorator (audit 2026-10-16, module ``core/instrumentation``).

The module is reachable from ``bot/app.py`` through ``knowledge/*`` and
``integrations/*`` and had no dedicated test.  Every assertion below reads the
observable contract: the structured events the decorator emits, the value (or
exception) that passes through it, and the fields a log consumer would grep for
in production — not the decorator's source text.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from nexus_ai_agent.core import instrumentation


class RecordingLogger:
    """Minimal structlog-shaped recorder: ``(level, event, kwargs)`` tuples."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def _record(self, level: str) -> Any:
        def log(event: str, **kwargs: Any) -> None:
            self.calls.append((level, event, kwargs))

        return log

    def __getattr__(self, name: str) -> Any:
        return self._record(name)

    def events(self) -> list[str]:
        return [event for _, event, _ in self.calls]

    def levels(self) -> list[str]:
        return [level for level, _, _ in self.calls]

    def kwargs_for(self, event: str) -> dict[str, Any]:
        return next(kwargs for _, name, kwargs in self.calls if name == event)


@pytest.fixture()
def recorded(monkeypatch: pytest.MonkeyPatch) -> RecordingLogger:
    logger = RecordingLogger()
    monkeypatch.setattr(instrumentation, "get_logger", lambda _name: logger)
    return logger


async def test_success_returns_the_value_and_reports_latency(recorded: RecordingLogger) -> None:
    @instrumentation.instrumented("knowledge.learn")
    async def learn(query: str) -> str:
        return f"learned {query}"

    assert await learn("cats") == "learned cats"
    assert recorded.events() == ["op_started", "op_completed"]
    assert recorded.kwargs_for("op_started")["op"] == "knowledge.learn"
    completed = recorded.kwargs_for("op_completed")
    assert completed["op"] == "knowledge.learn"
    assert isinstance(completed["latency_ms"], float)
    assert completed["latency_ms"] >= 0.0
    assert "info" in recorded.levels()


async def test_failure_is_reported_and_the_original_exception_escapes(
    recorded: RecordingLogger,
) -> None:
    boom = ValueError("upstream said no")

    @instrumentation.instrumented("knowledge.learn")
    async def learn(query: str) -> str:
        raise boom

    with pytest.raises(ValueError) as excinfo:
        await learn("cats")

    assert excinfo.value is boom, "the decorator must not wrap or replace the failure"
    failed = recorded.kwargs_for("op_failed")
    assert failed["error"] == "ValueError"
    assert failed["error_msg"] == "upstream said no"
    assert failed["op"] == "knowledge.learn"
    assert "op_completed" not in recorded.events()


async def test_slow_operation_warns_instead_of_informing(recorded: RecordingLogger) -> None:
    @instrumentation.instrumented("slow.op", slow_threshold_ms=0.0)
    async def slow() -> str:
        await asyncio.sleep(0)
        return "done"

    assert await slow() == "done"
    assert "warning" in recorded.levels()
    assert "info" not in recorded.levels()


async def test_log_args_is_bounded_and_never_echoes_the_whole_payload(
    recorded: RecordingLogger,
) -> None:
    payload = "x" * 5_000

    @instrumentation.instrumented("op", log_args=True)
    async def work(value: str) -> int:
        return len(value)

    assert await work(payload) == 5_000
    logged = recorded.kwargs_for("op_started")["args"]
    assert isinstance(logged, str)
    assert len(logged) <= 200
    assert payload not in logged


async def test_log_args_is_off_by_default(recorded: RecordingLogger) -> None:
    @instrumentation.instrumented("op")
    async def work(value: str) -> int:
        return len(value)

    await work("secret-ish")
    assert "args" not in recorded.kwargs_for("op_started")


async def test_wrapper_preserves_identity_and_is_awaitable(recorded: RecordingLogger) -> None:
    async def inner(x: int) -> int:
        """Docstring of inner."""
        return x + 1

    wrapped = instrumentation.instrumented("op")(inner)
    assert wrapped.__name__ == "inner"
    assert wrapped.__doc__ == "Docstring of inner."
    assert await wrapped(1) == 2
