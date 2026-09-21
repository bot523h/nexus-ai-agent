"""Storage resilience — wave-4 step3.

Chaos/fault-injection tests without any network: a fake transport that
fails N times then succeeds, and a secret-scrubbing log test.
"""

from __future__ import annotations

import pytest

from nexus_ai_agent.storage import resilience as sut

# -- idempotency key --------------------------------------------------------


def test_idempotency_key_is_deterministic() -> None:
    k1 = sut.idempotency_key("u1", "backups/db.sqlite", b"hello")
    k2 = sut.idempotency_key("u1", "backups/db.sqlite", b"hello")
    assert k1 == k2
    assert len(k1) == 64  # sha256 hex


def test_idempotency_key_changes_with_content() -> None:
    a = sut.idempotency_key("u1", "key", b"a")
    b = sut.idempotency_key("u1", "key", b"b")
    assert a != b


def test_idempotency_key_changes_with_object_key() -> None:
    a = sut.idempotency_key("u1", "a", b"x")
    b = sut.idempotency_key("u1", "b", b"x")
    assert a != b


# -- redaction --------------------------------------------------------------


def test_redact_secrets_strips_tokens() -> None:
    msg = "upload failed token=secret123 NEXUS_SIGNING_KEY=abcd password=hunter2"
    redacted = sut.redact_secrets(msg)
    assert "secret123" not in redacted
    assert "abcd" not in redacted
    assert "hunter2" not in redacted
    assert "[REDACTED]" in redacted


def test_redact_secrets_scrubs_64_hex() -> None:
    hex64 = "a" * 64
    assert sut.redact_secrets(f"key {hex64} end") == f"key {sut._REDACT_MARKER} end"  # noqa: SLF001


def test_redacted_log_never_raises() -> None:
    # Must not raise even on weird input
    sut.redacted_log("info", "ok", extra="value")


# -- retry core -------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_succeeds_after_transient_failures() -> None:
    calls = 0

    async def flaky() -> str:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise ConnectionError("flaky")
        return "ok"

    result, outcome = await sut.retry_with_backoff(flaky, max_attempts=4, base_ms=1, max_ms=5)
    assert result == "ok"
    assert outcome.attempts == 3
    assert calls == 3


@pytest.mark.asyncio
async def test_retry_exhausted_raises_typed() -> None:
    async def always_fail() -> str:
        raise ConnectionError("always")

    with pytest.raises(sut.RetryExhausted) as exc:
        await sut.retry_with_backoff(always_fail, max_attempts=2, base_ms=1, max_ms=5)
    assert isinstance(exc.value.last_exc, ConnectionError)
    assert exc.value.attempts == 2


@pytest.mark.asyncio
async def test_non_retryable_is_not_retried() -> None:
    calls = 0

    async def bad() -> str:
        nonlocal calls
        calls += 1
        raise ValueError("not retryable")

    with pytest.raises(ValueError):  # noqa: PT011 — direct raise is the contract
        await sut.retry_with_backoff(bad, max_attempts=5, base_ms=1, max_ms=5)
    assert calls == 1  # no retry


@pytest.mark.asyncio
async def test_non_idempotent_disables_retry() -> None:
    calls = 0

    async def flaky() -> str:
        nonlocal calls
        calls += 1
        raise ConnectionError("x")

    # Even though ConnectionError is retryable, idempotent=False disables it
    with pytest.raises(ConnectionError):  # noqa: PT011
        await sut.retry_with_backoff(flaky, idempotent=False, max_attempts=5, base_ms=1)
    assert calls == 1


@pytest.mark.asyncio
async def test_retry_respects_jitter_and_env_tunables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXUS_STORAGE_MAX_RETRIES", "2")
    monkeypatch.setenv("NEXUS_STORAGE_BACKOFF_BASE_MS", "1")
    monkeypatch.setenv("NEXUS_STORAGE_BACKOFF_MAX_MS", "2")
    assert sut.max_retries() == 2
    assert sut.backoff_base_ms() == 1
    assert sut.backoff_max_ms() == 2

    calls = 0

    async def flaky() -> int:
        nonlocal calls
        calls += 1
        if calls < 2:
            raise ConnectionError("x")
        return 42

    # Use env defaults (no explicit max_attempts)
    result, outcome = await sut.retry_with_backoff(flaky)
    assert result == 42
    assert outcome.attempts == 2


@pytest.mark.asyncio
async def test_retry_on_custom_exception_tuple() -> None:
    class MyTransient(RuntimeError):
        pass

    calls = 0

    async def flaky() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise MyTransient("custom")
        return "ok"

    result, _ = await sut.retry_with_backoff(
        flaky, retry_on=(MyTransient,), max_attempts=3, base_ms=1, max_ms=5
    )
    assert result == "ok"


def test_backoff_env_fallback_on_bad_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXUS_STORAGE_MAX_RETRIES", "not-an-int")
    assert sut.max_retries() == 3  # default
