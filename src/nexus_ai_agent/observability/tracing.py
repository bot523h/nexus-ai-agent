"""Opt-in LangSmith tracing — fail-closed, never fatal.

The runtime already uses LangGraph, and LangGraph/LangChain trace through the
``LANGSMITH_*`` environment variables that the ``langsmith`` SDK reads at
import time.  This module is the single place that decides whether that tracing
is on, so the decision is explicit, testable, and impossible to enable by
accident:

* **default off** — nothing is exported unless ``NEXUS_TRACING_ENABLED=true``;
* **fail closed, never fatal** — an enabled-but-incomplete configuration (no
  SDK, no key) degrades to a typed, logged no-op; the caller's work continues
  exactly as if tracing were off.  Observability must never be able to break a
  request (the same rule ``logging.py`` follows);
* **no silent egress** — enabling tracing exports user prompts to a third
  party, so the switch is explicit and the outcome is always reported.

The returned :class:`TracingState` is the honest, inspectable result — the
caller can log or surface it, and tests assert on it instead of on process
globals.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

from nexus_ai_agent.observability.logging import get_logger

logger = get_logger(__name__)

#: Default collector endpoint; overridable with ``NEXUS_TRACING_ENDPOINT``.
DEFAULT_ENDPOINT = "https://api.smith.langchain.com"

#: Why tracing is or is not active — a closed set so callers can branch on it.
TracingReason = Literal[
    "disabled",
    "enabled",
    "sdk_missing",
    "key_missing",
]


@dataclass(frozen=True)
class TracingState:
    """The honest outcome of a tracing bootstrap attempt."""

    active: bool
    reason: TracingReason
    project: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {"active": self.active, "reason": self.reason, "project": self.project}


def _langsmith_available() -> bool:
    """True when the optional ``langsmith`` SDK is importable."""
    import importlib.util

    try:
        return importlib.util.find_spec("langsmith") is not None
    except (ImportError, ValueError):  # pragma: no cover - defensive
        return False


def configure_tracing(
    *,
    enabled: bool,
    api_key: str | None,
    project: str,
    endpoint: str = DEFAULT_ENDPOINT,
) -> TracingState:
    """Export the ``LANGSMITH_*`` environment when tracing is fully configured.

    Pure with respect to its arguments and side-effect-only-on-success: the
    environment is mutated only on the ``active`` path, so a partial
    configuration can never leave a half-enabled tracing state behind.  This
    function never raises.
    """
    if not enabled:
        return TracingState(active=False, reason="disabled")

    if not _langsmith_available():
        logger.info(
            "tracing_skipped",
            reason="sdk_missing",
            hint="pip install 'nexus-ai-agent[tracing]'",
        )
        return TracingState(active=False, reason="sdk_missing", project=project)

    if not api_key:
        logger.info("tracing_skipped", reason="key_missing", hint="set NEXUS_TRACING_API_KEY")
        return TracingState(active=False, reason="key_missing", project=project)

    # The SDK reads these names (LANGSMITH_* is the current spelling; the
    # legacy LANGCHAIN_* names are the same variables under the old prefix).
    os.environ["LANGSMITH_TRACING"] = "true"
    os.environ["LANGSMITH_API_KEY"] = api_key
    os.environ["LANGSMITH_PROJECT"] = project
    os.environ["LANGSMITH_ENDPOINT"] = endpoint
    logger.info("tracing_enabled", project=project, endpoint=endpoint)
    return TracingState(active=True, reason="enabled", project=project)


def bootstrap_tracing(settings: object) -> TracingState:
    """Bootstrap tracing from a :class:`Settings` object, never raising.

    A tracing failure is an observability failure, and observability failures
    are never fatal here: any unexpected error degrades to ``disabled`` with a
    logged warning rather than propagating into the caller's startup path.
    """
    try:
        return configure_tracing(
            enabled=bool(getattr(settings, "tracing_enabled", False)),
            api_key=getattr(settings, "tracing_api_key", None),
            project=str(getattr(settings, "tracing_project", "") or "nexus-ai-agent"),
            endpoint=str(getattr(settings, "tracing_endpoint", "") or DEFAULT_ENDPOINT),
        )
    except Exception as exc:  # noqa: BLE001 - observability must never break startup
        logger.warning("tracing_bootstrap_failed", error=type(exc).__name__)
        return TracingState(active=False, reason="disabled")


__all__ = [
    "DEFAULT_ENDPOINT",
    "TracingReason",
    "TracingState",
    "bootstrap_tracing",
    "configure_tracing",
]
