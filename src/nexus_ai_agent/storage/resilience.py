"""Storage resilience: retry, idempotency, secret-safe logging (wave-4 step3).

Opt-in primitives for cloud blob and checkpoint adapters. Provider wiring
is separate: importing this module does not enable retries globally.
The core is three primitives that compose without any new dependency:

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
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from nexus_ai_agent.observability.logging import get_logger
from nexus_ai_agent.observability.logging import redact_secrets as _redact_text

logger = get_logger(__name__)

T = TypeVar("T")

# -- env tunables -----------------------------------------------------------


def _int_env(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)).strip() or default)
        return value if value >= 0 else default
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
    user = str(user_id)
    if "\x00" in user or "\x00" in object_key:
        raise ValueError("idempotency components must not contain NUL")
    h = hashlib.sha256()
    h.update(user.encode("utf-8"))
    h.update(b"\x00")
    h.update(object_key.encode("utf-8"))
    if content is not None:
        h.update(b"\x00")
        h.update(content)
    return h.hexdigest()


# -- secret redaction -------------------------------------------------------


_REDACT_MARKER = "[REDACTED]"
_SENSITIVE_KEY = re.compile(
    r"(?i)(token|secret|password|api[_-]?key|access[_-]?key|signing[_-]?key|"
    r"authorization|bearer|credential)"
)
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)(?:[\w-]*(?:api[_-]?key|secret|token|password|signing[_-]?key|credential)"
    r"[\w-]*)[\"']?\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;}]+)"
)
_HEX_SECRET = re.compile(r"\b[0-9a-fA-F]{64}\b")
_LOG_LEVELS = frozenset({"debug", "info", "warning", "error", "critical", "exception"})


def redact_secrets(message: str) -> str:
    """Best-effort known-shape redaction, failing closed on sanitizer errors.

    Not a general secret detector: callers must not log arbitrary credentials.
    """
    try:
        message = _SECRET_ASSIGNMENT.sub(_REDACT_MARKER, message)
        return _HEX_SECRET.sub(_REDACT_MARKER, _redact_text(message))
    except Exception:
        return _REDACT_MARKER


def _safe_field(value: Any, depth: int = 0) -> Any:
    """Copy JSON-like fields; bound recursion and never render opaque objects."""
    if depth >= 8:
        return _REDACT_MARKER
    if isinstance(value, str):
        return redact_secrets(value)
    if value is None or type(value) in (bool, int, float):
        return value
    if type(value) is dict:
        return {
            key: _REDACT_MARKER if _SENSITIVE_KEY.search(key) else _safe_field(item, depth + 1)
            for key, item in value.items()
            if isinstance(key, str)
        }
    if type(value) in (list, tuple):
        return [_safe_field(item, depth + 1) for item in value]
    return _REDACT_MARKER


def redacted_log(level: str, msg: str, **kwargs: Any) -> None:
    """Scrub event and fields before the sink, without mutating caller data.

    Raw traceback fields are suppressed; they bypass string redaction in many
    logging pipelines. Log exception *types*, not exception payloads.
    """
    safe = redact_secrets(msg)
    fields = _safe_field(kwargs)
    for key in ("exc_info", "stack_info"):
        if key in fields:
            fields[key] = False
    log_fn = getattr(logger, level if level in _LOG_LEVELS else "info")
    # logger.exception implicitly enables raw traceback output.
    if level == "exception":
        fields["exc_info"] = False
    log_fn(safe, **fields)


# -- retry core -------------------------------------------------------------


@dataclass(frozen=True)
class RetryOutcome:
    attempts: int
    elapsed_ms: int


class RetryExhausted(RuntimeError):
    """Raised when all retry attempts failed; carries the last exception."""

    def __init__(self, last_exc: BaseException, attempts: int, elapsed_ms: int) -> None:
        super().__init__(f"retry exhausted after {attempts} attempts ({type(last_exc).__name__})")
        self.last_exc = last_exc
        self.attempts = attempts
        self.elapsed_ms = elapsed_ms


# Default retryable errors: Python transport errors (not HTTP status codes).
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
        the last exception as ``.last_exc`` (sensitive; never log it).
        ``ValueError`` for invalid explicit retry counts/delays, before I/O.
        Cancellation and process-control exceptions always propagate.
        Non-retryable exceptions are raised immediately without wrapping.
    """
    start = time.monotonic()
    if not idempotent:
        # No retry for non-idempotent writes — fail fast
        result = await func()
        elapsed = int((time.monotonic() - start) * 1000)
        return result, RetryOutcome(attempts=1, elapsed_ms=elapsed)

    attempts = max_attempts if max_attempts is not None else max_retries() + 1
    b_base = base_ms if base_ms is not None else backoff_base_ms()
    b_max = max_ms if max_ms is not None else backoff_max_ms()

    for name, value, minimum in (
        ("max_attempts", attempts, 1),
        ("base_ms", b_base, 0),
        ("max_ms", b_max, 0),
    ):
        if type(value) is not int or value < minimum:
            raise ValueError(f"{name} must be an integer >= {minimum}")
    backoff = min(b_base, b_max)
    last_exc: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            result = await func()
            elapsed = int((time.monotonic() - start) * 1000)
            return result, RetryOutcome(attempts=attempt, elapsed_ms=elapsed)
        except Exception as exc:
            last_exc = exc
            if not _should_retry(exc, retry_on):
                raise
            if attempt == attempts:
                break
            # Saturating recurrence avoids constructing enormous powers of two.
            sleep_ms = random.uniform(0, backoff) if jitter else backoff
            redacted_log(
                "warning", f"storage retry {attempt}/{attempts}", error_type=type(exc).__name__
            )
            await asyncio.sleep(sleep_ms / 1000.0)
            backoff = min(backoff * 2, b_max)

    assert last_exc is not None
    elapsed = int((time.monotonic() - start) * 1000)
    raise RetryExhausted(last_exc, attempts=attempts, elapsed_ms=elapsed) from None
