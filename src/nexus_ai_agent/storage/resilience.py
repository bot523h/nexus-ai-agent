"""Storage resilience: retry, idempotency, secret-safe logging (wave-4 step3).

Every cloud blob (R2/S3) and every checkpoint upload runs through this
module.  The core is three primitives that compose without any new
dependency:

* :func:`retry_with_backoff` — exponential backoff with full jitter for
  idempotent operations (upload, download, delete).  Non-idempotent ops
  must supply ``idempotent=False`` to disable retries.

* :func:`idempotency_key` — deterministic key for uploads (``sha256(user+key+bytes)``)
  so a retried upload never creates a duplicate object.

* :func:`redacted_log` — structured log helper that strips any
  ``NEXUS_*_KEY`` / ``TOKEN`` / ``SECRET`` material before the message
  reaches the log sink (the ``observability/logging.py`` redaction stage
  is the second line of defence; this helper is the first).

All knobs are env-driven so the module remains test-friendly:

* ``NEXUS_STORAGE_MAX_RETRIES`` (default ``3``)
* ``NEXUS_STORAGE_BACKOFF_BASE_MS`` (default ``200``)
* ``NEXUS_STORAGE_BACKOFF_MAX_MS`` (default ``5000``)

The retry loop is pure ``asyncio`` — no thread, no process — and the
backoff sleeps via ``asyncio.sleep`` so it yields the event loop.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from nexus_ai_agent.observability.logging import get_logger

logger = get_logger(__name__)

T = TypeVar("T")

# -- env tunables -----------------------------------------------------------


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)).strip() or default)
    except (ValueError, TypeError):
        return default


def max_retries() -> int:
    return _int_env("NEXUS_STORAGE_MAX_RETRIES", 3)


def backoff_base_ms() -> int:
    return _int_env("NEXUS_STORAGE_BACKOFF_BASE_MS", 200)


def backoff_max_ms() -> int:
    return _int_env("NEXUS_STORAGE_BACKOFF_MAX_MS", 5000)


# -- idempotency ------------------------------------------------------------


def idempotency_key(user_id: str | int, object_key: str, content: bytes | None = None) -> str:
    """Deterministic idempotency key for an upload.

    ``user_id`` + ``object_key`` are always mixed in; ``content`` (when
    provided) binds the key to the exact bytes so a changed file gets a
    fresh key.  The output is hex ``sha256`` — safe for header/metadata.
    """
    h = hashlib.sha256()
    h.update(str(user_id).encode("utf-8"))
    h.update(b"\x00")
    h.update(object_key.encode("utf-8"))
    if content is not None:
        h.update(b"\x00")
        h.update(content)
    return h.hexdigest()


# -- secret redaction -------------------------------------------------------


_REDACT_MARKER = "[REDACTED]"


def redact_secrets(message: str) -> str:
    """Strip obvious secret material from a log message.

    This is a *first* line of defence; the structlog processor in
    ``observability/logging.py`` is the second.  The function never
    raises — on any internal error the original message is returned with
    ``[REDACTED?]`` appended so the log still shows the operation was
    redacted.
    """
    try:
        import re

        # Replace key=value / key: value patterns for known secret markers
        # e.g. ``token=secret123`` or ``password: hunter2`` → ``[REDACTED]``
        pattern = re.compile(r"(?i)(api_key|secret|token|password|signing_key)\s*[:=]\s*\S+")
        message = pattern.sub(_REDACT_MARKER, message)
        # Also scrub any bare 32-byte hex that looks like a signing key (64 hex)
        message = re.sub(r"\b[0-9a-fA-F]{64}\b", _REDACT_MARKER, message)
        return message
    except Exception:
        return message + " [REDACTED?]"


def redacted_log(level: str, msg: str, **kwargs: Any) -> None:
    safe = redact_secrets(msg)
    # Structured logger already redacts, but we pre-scrub for defence in depth.
    log_fn = getattr(logger, level, logger.info)
    log_fn(safe, **kwargs)


# -- retry core -------------------------------------------------------------


@dataclass(frozen=True)
class RetryOutcome:
    attempts: int
    elapsed_ms: int


class RetryExhausted(RuntimeError):
    """Raised when all retry attempts failed; carries the last exception."""

    def __init__(self, last_exc: BaseException, attempts: int, elapsed_ms: int) -> None:
        super().__init__(f"retry exhausted after {attempts} attempts: {last_exc}")
        self.last_exc = last_exc
        self.attempts = attempts
        self.elapsed_ms = elapsed_ms


# Canonical retryable errors: transport hiccups, 429, 5xx.
# Callers may broaden/narrow by passing ``retry_on``.
_DEFAULT_RETRYABLE = (ConnectionError, TimeoutError, asyncio.TimeoutError)


def _should_retry(exc: BaseException, retry_on: tuple[type[BaseException], ...]) -> bool:
    return isinstance(exc, retry_on)


async def retry_with_backoff(
    func: Callable[[], Awaitable[T]],
    *,
    max_attempts: int | None = None,
    retry_on: tuple[type[BaseException], ...] = _DEFAULT_RETRYABLE,
    idempotent: bool = True,
    base_ms: int | None = None,
    max_ms: int | None = None,
    jitter: bool = True,
) -> tuple[T, RetryOutcome]:
    """Execute ``func`` with exponential backoff + jitter.

    Args:
        func: Zero-arg ``async`` callable (the operation).
        max_attempts: Total attempts (first try + retries).  Defaults to
            ``max_retries() + 1`` (so 3 retries → 4 attempts).
        retry_on: Exception types that are retryable.  Default covers
            transport errors; callers should pass e.g.
            ``(StorageTransientError,)`` for storage-specific codes.
        idempotent: When ``False`` retries are disabled — a non-idempotent
            write that failed midway must not be retried blindly.
        base_ms / max_ms: Backoff knobs (env defaults).
        jitter: When ``True`` (default) apply full jitter
            (``sleep = random.uniform(0, backoff)``).

    Returns:
        ``(result, outcome)`` — ``outcome`` reports attempts and wall time.

    Raises:
        ``RetryExhausted`` when the retry budget is exhausted — carries
        the last exception as ``.last_exc``.
        Non-retryable exceptions are raised immediately without wrapping.
    """
    if not idempotent:
        # No retry for non-idempotent writes — fail fast
        result = await func()
        return result, RetryOutcome(attempts=1, elapsed_ms=0)

    attempts = max_attempts if max_attempts is not None else max_retries() + 1
    b_base = base_ms if base_ms is not None else backoff_base_ms()
    b_max = max_ms if max_ms is not None else backoff_max_ms()

    start = time.monotonic()
    last_exc: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            result = await func()
            elapsed = int((time.monotonic() - start) * 1000)
            return result, RetryOutcome(attempts=attempt, elapsed_ms=elapsed)
        except BaseException as exc:  # noqa: BLE001
            last_exc = exc
            if not _should_retry(exc, retry_on):
                raise
            if attempt == attempts:
                break
            # Backoff: base * 2**(attempt-1), capped, with optional jitter
            backoff = min(b_base * (2 ** (attempt - 1)), b_max)
            sleep_ms = random.uniform(0, backoff) if jitter else backoff
            redacted_log("warning", f"storage retry {attempt}/{attempts} — {exc!r}")
            await asyncio.sleep(sleep_ms / 1000.0)

    assert last_exc is not None
    elapsed = int((time.monotonic() - start) * 1000)
    raise RetryExhausted(last_exc, attempts=attempts, elapsed_ms=elapsed)
