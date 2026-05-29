"""Instrumentation decorators — structured logging with latency tracking.

Wrap any async function with `@instrumented("operation_name")` to automatically
log start, success/failure, and latency. Builds on existing structlog setup.

Usage:
    @instrumented("knowledge.learn")
    async def learn(query: str) -> dict:
        ...

Output (success):
    event=op_started op=knowledge.learn
    event=op_completed op=knowledge.learn latency_ms=234.5

Output (failure):
    event=op_failed op=knowledge.learn error=ValueError latency_ms=12.3

Why this exists:
- Without consistent latency/error logging, you can't tell which subsystem is slow.
- Without structured fields, you can't grep/aggregate logs in production.
- One decorator = consistent observability across knowledge/, integrations/, agent/.
"""

from __future__ import annotations

import functools
import time
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from nexus_ai_agent.observability.logging import get_logger

T = TypeVar("T")


def instrumented(
    op_name: str,
    *,
    log_args: bool = False,
    slow_threshold_ms: float = 1000.0,
) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    """Decorator that adds structured logging + latency tracking.

    Args:
        op_name: Logical operation name, e.g. "knowledge.learn".
        log_args: If True, log positional args (be careful with PII).
        slow_threshold_ms: Operations slower than this log a warning.
    """

    def decorator(func: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        logger = get_logger(func.__module__)

        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> T:
            start = time.monotonic()
            log_kwargs: dict[str, Any] = {"op": op_name}
            if log_args:
                # Skip self/cls for methods
                visible_args = args[1:] if args and hasattr(args[0], "__class__") else args
                log_kwargs["args"] = repr(visible_args)[:200]

            logger.debug("op_started", **log_kwargs)
            try:
                result = await func(*args, **kwargs)
            except Exception as exc:
                latency_ms = (time.monotonic() - start) * 1000
                logger.error(
                    "op_failed",
                    op=op_name,
                    error=type(exc).__name__,
                    error_msg=str(exc)[:200],
                    latency_ms=round(latency_ms, 1),
                )
                raise

            latency_ms = (time.monotonic() - start) * 1000
            level_logger = logger.warning if latency_ms > slow_threshold_ms else logger.info
            level_logger(
                "op_completed",
                op=op_name,
                latency_ms=round(latency_ms, 1),
            )
            return result

        return wrapper

    return decorator
