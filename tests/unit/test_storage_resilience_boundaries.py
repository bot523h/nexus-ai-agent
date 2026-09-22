"""Offline adversarial contract checks: no database, network or real sleeps."""

from __future__ import annotations

import asyncio
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from nexus_ai_agent.storage import resilience as sut


@pytest.mark.parametrize("field", ["max_attempts", "base_ms", "max_ms"])
@pytest.mark.parametrize("value", [-1, True, 1.5, float("inf"), "2"])
async def test_invalid_policy_has_no_side_effects(field, value):
    operation = AsyncMock()
    with pytest.raises(ValueError, match=field):
        await sut.retry_with_backoff(operation, **{field: value})
    operation.assert_not_awaited()


async def test_zero_attempts_rejected():
    operation = AsyncMock()
    with pytest.raises(ValueError, match="max_attempts"):
        await sut.retry_with_backoff(operation, max_attempts=0)
    operation.assert_not_awaited()


@pytest.mark.parametrize(
    ("name", "getter", "default"),
    [
        ("NEXUS_STORAGE_MAX_RETRIES", sut.max_retries, 3),
        ("NEXUS_STORAGE_BACKOFF_BASE_MS", sut.backoff_base_ms, 200),
        ("NEXUS_STORAGE_BACKOFF_MAX_MS", sut.backoff_max_ms, 5000),
    ],
)
@pytest.mark.parametrize("value", ["-1", "nan", "", " "])
def test_invalid_env_uses_safe_defaults(monkeypatch, name, getter, default, value):
    monkeypatch.setenv(name, value)
    assert getter() == default


@pytest.mark.parametrize("error_type", [asyncio.CancelledError, KeyboardInterrupt, SystemExit])
async def test_control_flow_never_retried(monkeypatch, error_type):
    operation = AsyncMock(side_effect=error_type())
    sleep = AsyncMock()
    monkeypatch.setattr(sut.asyncio, "sleep", sleep)
    with pytest.raises(error_type):
        await sut.retry_with_backoff(operation, retry_on=(BaseException,))
    assert operation.await_count == 1
    sleep.assert_not_awaited()


async def test_cancellation_during_backoff_propagates(monkeypatch):
    operation = AsyncMock(side_effect=ConnectionError("offline"))
    monkeypatch.setattr(sut.asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError))
    with pytest.raises(asyncio.CancelledError):
        await sut.retry_with_backoff(operation)
    assert operation.await_count == 1


@pytest.mark.parametrize(
    ("base", "cap", "expected"), [(2, 5, [2, 4, 5]), (9, 3, [3] * 3), (0, 5, [0] * 3)]
)
async def test_backoff_schedule(monkeypatch, base, cap, expected):
    sleep = AsyncMock()
    monkeypatch.setattr(sut.asyncio, "sleep", sleep)
    operation = AsyncMock(side_effect=[ConnectionError(), ConnectionError(), ConnectionError(), 42])
    result, outcome = await sut.retry_with_backoff(
        operation, max_attempts=4, base_ms=base, max_ms=cap, jitter=False
    )
    assert result == 42 and outcome.attempts == 4
    assert [c.args[0] for c in sleep.await_args_list] == [n / 1000 for n in expected]


async def test_long_retry_budget_does_not_overflow(monkeypatch):
    monkeypatch.setattr(sut.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(sut, "redacted_log", Mock())
    operation = AsyncMock(side_effect=ConnectionError())
    with pytest.raises(sut.RetryExhausted) as caught:
        await sut.retry_with_backoff(operation, max_attempts=1100, base_ms=0, max_ms=0)
    assert caught.value.attempts == 1100


async def test_jitter_within_capped_budget(monkeypatch):
    uniform = Mock(return_value=2)
    sleep = AsyncMock()
    monkeypatch.setattr(sut.random, "uniform", uniform)
    monkeypatch.setattr(sut.asyncio, "sleep", sleep)
    await sut.retry_with_backoff(
        AsyncMock(side_effect=[ConnectionError(), 1]), base_ms=10, max_ms=4
    )
    uniform.assert_called_once_with(0, 4)
    sleep.assert_awaited_once_with(0.002)


async def test_non_idempotent_timing_is_measured(monkeypatch):
    clock = iter([10.0, 10.125])
    monkeypatch.setattr(sut, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    result, outcome = await sut.retry_with_backoff(AsyncMock(return_value=7), idempotent=False)
    assert result == 7 and outcome.elapsed_ms == 125 and outcome.attempts == 1


async def test_retry_errors_do_not_render_exception_payload(monkeypatch):
    class HostileError(ConnectionError):
        def __str__(self):
            raise AssertionError("must not render exception")

        def __repr__(self):
            raise AssertionError("must not render exception")

    error = HostileError()
    monkeypatch.setattr(sut.asyncio, "sleep", AsyncMock())
    with pytest.raises(sut.RetryExhausted) as caught:
        await sut.retry_with_backoff(AsyncMock(side_effect=error), max_attempts=2)
    assert caught.value.last_exc is error
    assert "HostileError" in str(caught.value)
    assert caught.value.__suppress_context__ is True


@pytest.mark.parametrize(
    "message",
    [
        'password="two word secret"',
        '{"NEXUS_SIGNING_KEY": "two word secret"}',
        "Bearer abcdefghijklmnop",
        "payload token=abcdefghijklmnop",
        'credential: "two word secret"',
    ],
)
def test_secret_text_shapes(message):
    safe = sut.redact_secrets(message)
    assert "two word secret" not in safe
    assert "abcdefghijklmnop" not in safe
    assert "[REDACTED]" in safe


def test_redaction_failure_is_closed(monkeypatch):
    monkeypatch.setattr(sut, "_redact_text", Mock(side_effect=RuntimeError("oops")))
    assert sut.redact_secrets("secret payload") == "[REDACTED]"


def test_structured_fields_are_scrubbed_without_mutation(monkeypatch):
    logger = Mock()
    monkeypatch.setattr(sut, "logger", logger)
    payload = {"nested": [{"password": "hidden"}], "message": "token=hidden", "count": 2}
    sut.redacted_log("warning", "ok", payload=payload, NEXUS_SIGNING_KEY=b"hidden")
    kwargs = logger.warning.call_args.kwargs
    assert "hidden" not in repr(kwargs)
    assert kwargs["payload"]["count"] == 2
    assert payload["nested"][0]["password"] == "hidden"


def test_cyclic_and_opaque_log_fields_fail_closed(monkeypatch):
    logger = Mock()
    monkeypatch.setattr(sut, "logger", logger)
    cycle = []
    cycle.append(cycle)
    sut.redacted_log("info", "ok", cyclic=cycle, opaque=object())
    assert "[REDACTED]" in repr(logger.info.call_args.kwargs)


def test_log_level_cannot_select_arbitrary_logger_method(monkeypatch):
    logger = Mock()
    monkeypatch.setattr(sut, "logger", logger)
    sut.redacted_log("bind", "ok")
    logger.bind.assert_not_called()
    logger.info.assert_called_once()


@pytest.mark.parametrize(("user", "key"), [("a\0b", "c"), ("a", "b\0c")])
def test_ambiguous_idempotency_components_rejected(user, key):
    with pytest.raises(ValueError, match="NUL"):
        sut.idempotency_key(user, key)


def test_idempotency_normal_keys_remain_compatible():
    assert sut.idempotency_key("u", "key", b"abc") == hashlib.sha256(b"u\0key\0abc").hexdigest()
    assert sut.idempotency_key("u", "key") != sut.idempotency_key("u", "key", b"")
    assert sut.idempotency_key(42, "key") == sut.idempotency_key("42", "key")


@pytest.mark.parametrize("level", ["warning", "exception"])
def test_raw_traceback_fields_are_disabled(monkeypatch, level):
    logger = Mock()
    monkeypatch.setattr(sut, "logger", logger)
    sut.redacted_log(level, "failed", exc_info=True, stack_info=True)
    fields = getattr(logger, level).call_args.kwargs
    assert fields["exc_info"] is False and fields["stack_info"] is False


async def test_exhaustion_has_no_final_sleep_and_retains_last_error(monkeypatch):
    sleep = AsyncMock()
    monkeypatch.setattr(sut.asyncio, "sleep", sleep)
    first, last = ConnectionError("first"), ConnectionError("password=hidden")
    with pytest.raises(sut.RetryExhausted) as caught:
        await sut.retry_with_backoff(AsyncMock(side_effect=[first, last]), max_attempts=2)
    assert caught.value.last_exc is last
    assert "hidden" not in str(caught.value)
    assert sleep.await_count == 1


async def test_non_idempotent_failure_retains_identity_and_never_sleeps(monkeypatch):
    error = ConnectionError("failed")
    operation = AsyncMock(side_effect=error)
    sleep = AsyncMock()
    monkeypatch.setattr(sut.asyncio, "sleep", sleep)
    with pytest.raises(ConnectionError) as caught:
        await sut.retry_with_backoff(operation, idempotent=False)
    assert caught.value is error
    assert operation.await_count == 1
    sleep.assert_not_awaited()


def test_zero_env_retries_is_valid(monkeypatch):
    monkeypatch.setenv("NEXUS_STORAGE_MAX_RETRIES", "0")
    assert sut.max_retries() == 0
