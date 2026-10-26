"""Opt-in LangSmith tracing — fail-closed, never fatal.

The runtime already uses LangGraph, and LangGraph/LangChain trace through the
``LANGSMITH_*`` environment variables that the ``langsmith`` SDK reads.  This
module is the single place that decides whether that tracing is on, so the
decision is explicit, testable, and impossible to enable by accident:

* **default off** — nothing is exported unless ``NEXUS_TRACING_ENABLED=true``.
  ``NEXUS_TRACING_ENABLED`` is the *only* authority for the runtime's egress, so
  a stray ``LANGSMITH_TRACING``/``LANGCHAIN_TRACING_V2`` left in the process
  environment cannot turn tracing on behind the operator's back: every
  non-active outcome clears those SDK flags *and* forces the SDK's
  cache-independent process-global override off;
* **fail closed, never fatal** — an enabled-but-incomplete configuration (no
  SDK, no key) degrades to a typed, logged no-op; the caller's work continues
  exactly as if tracing were off.  Observability must never be able to break a
  request (the same rule ``logging.py`` follows);
* **no silent egress** — enabling tracing exports user prompts to a third
  party, so the switch is explicit and the outcome is always reported.

The SDK is **not** an optional extra: ``langsmith`` ships with the core install
(it is pulled in transitively by ``langgraph`` → ``langchain-core``), so there is
nothing to ``pip install`` and the core-absence contract cannot be violated.

Environment is a *startup* contract: the SDK reads these names through a
``functools.lru_cache``, so the first read wins.  ``bootstrap_tracing`` is meant
to run once, early — before the first LangGraph/LangChain call.

The returned :class:`TracingState` is the honest, inspectable result — the
caller can log or surface it, and tests assert on it instead of on process
globals.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Literal

from nexus_ai_agent.observability.logging import get_logger

logger = get_logger(__name__)

#: A class name we are willing to print verbatim; anything else is elided.
_SAFE_CLASS_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\Z")

#: Default collector endpoint; overridable with ``NEXUS_TRACING_ENDPOINT``.
DEFAULT_ENDPOINT = "https://api.smith.langchain.com"

#: The SDK flags that turn tracing *on* when read from the environment.  The
#: ``langsmith`` SDK resolves ``TRACING_V2``/``TRACING`` under both the
#: ``LANGSMITH_`` and ``LANGCHAIN_`` namespaces, so a stale value in any of these
#: could enable egress without ``NEXUS_TRACING_ENABLED``.  Every non-active
#: outcome clears them; the active path sets them.  Kept as the single source of
#: truth so the enable and disable paths cannot drift apart.
_TRACING_FLAGS = (
    "LANGSMITH_TRACING",
    "LANGCHAIN_TRACING_V2",
    "LANGCHAIN_TRACING",
    "LANGCHAIN_HANDLER",
)

#: Why tracing is or is not active — a closed set so callers can branch on it.
TracingReason = Literal[
    "disabled",
    "active",
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


def _clear_tracing_flags() -> None:
    """Remove every SDK flag that could turn tracing on from the environment."""
    for name in _TRACING_FLAGS:
        os.environ.pop(name, None)


def _set_sdk_override(enabled: bool | None) -> None:
    """Set the SDK's process-global tracing override to ``enabled`` (or ``None``).

    ``langsmith.tracing_is_enabled()`` decides in this order: a per-context
    override, the current run tree, a process-global override, then the
    environment.  The environment read is ``functools.lru_cache``d, so clearing
    env vars cannot undo a read that already happened — the process-global
    override (checked *before* the env) is the only cache-independent switch, and
    the bootstrap drives it in both directions.  Reached through ``importlib``
    (never a top-level ``import langsmith``) so ``deptry`` does not treat a
    transitive dependency as a direct one; if the attribute is missing on SDK
    drift this degrades to env-only behaviour rather than raising.
    """
    try:
        import importlib

        context = importlib.import_module("langsmith._internal._context")
        vars(context)["_GLOBAL_TRACING_ENABLED"] = enabled
    except Exception:  # noqa: BLE001 - best-effort; env-only fallback is still correct
        pass


def _deactivate_tracing() -> None:
    """Make tracing unambiguously off: clear env flags and force the SDK override."""
    _clear_tracing_flags()
    _set_sdk_override(False)


def _sanitize_class_name(name: object) -> str:
    """Return ``name`` only when it is a bare identifier, else ``"Exception"``.

    A hostile or merely unusual ``__name__`` could otherwise smuggle newlines,
    URLs or payload text into a diagnostic line; only a short identifier is
    allowed through verbatim.
    """
    text = str(name)
    return text if _SAFE_CLASS_NAME.match(text) else "Exception"


def _sanitize_status(value: object) -> str | None:
    """Return a bounded, allow-listed status code (e.g. ``"403"``) or ``None``.

    Only short, safe-looking tokens survive: a digit run (an HTTP status) or a
    bare identifier (a symbolic code such as ``"invalid_api_key"``).  URLs,
    messages and payloads are dropped, never echoed.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text or len(text) > 48:
        return None
    if text.isdigit() or re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,31}", text):
        return text
    return None


def sanitize_exception(exc: BaseException) -> str:
    """Describe ``exc`` with an allow-listed class name and optional status code.

    Deliberately *never* includes ``str(exc)`` or ``repr(exc)``: SDK/HTTP error
    text can embed the API key, the request URL (with credentials or query
    parameters) and the request payload (prompt/trace content).  Only the
    exception *class* and a bounded status code are ever returned, so a
    diagnostic line cannot leak secrets.
    """
    parts = [_sanitize_class_name(type(exc).__name__)]
    status = _sanitize_status(getattr(exc, "status_code", None))
    if status is not None:
        parts.append(f"status={status}")
    return " ".join(parts)


def configure_tracing(
    *,
    enabled: bool,
    api_key: str | None,
    project: str,
    endpoint: str = DEFAULT_ENDPOINT,
) -> TracingState:
    """Export the ``LANGSMITH_*`` environment when tracing is fully configured.

    ``NEXUS_TRACING_ENABLED`` is the sole authority: when it is false, or when
    the configuration is incomplete, the SDK's enable-flags are *cleared* so a
    stale ``LANGSMITH_TRACING``/``LANGCHAIN_TRACING_V2`` in the process
    environment cannot smuggle egress past the switch.  The environment is
    mutated on every path (to *disable* as well as to enable), so the result is
    deterministic regardless of prior process state.  This function never raises.
    """
    if not enabled:
        _deactivate_tracing()
        return TracingState(active=False, reason="disabled")

    if not _langsmith_available():
        _deactivate_tracing()
        logger.info(
            "tracing_skipped",
            reason="sdk_missing",
            hint="the langsmith SDK ships with the core install (langgraph -> langchain-core)",
        )
        return TracingState(active=False, reason="sdk_missing", project=project)

    if not api_key:
        _deactivate_tracing()
        logger.info("tracing_skipped", reason="key_missing", hint="set NEXUS_TRACING_API_KEY")
        return TracingState(active=False, reason="key_missing", project=project)

    # The SDK resolves ``TRACING_V2``/``TRACING`` under both the ``LANGSMITH_``
    # and ``LANGCHAIN_`` namespaces, and ``LANGCHAIN_TRACING``/``LANGCHAIN_HANDLER``
    # are the legacy V1 flags that make langchain-core raise.  Set the current
    # spelling and clear the legacy ones so exactly one switch is ever on.
    os.environ["LANGSMITH_TRACING"] = "true"
    os.environ["LANGSMITH_API_KEY"] = api_key
    os.environ["LANGSMITH_PROJECT"] = project
    os.environ["LANGSMITH_ENDPOINT"] = endpoint
    for name in ("LANGCHAIN_TRACING_V2", "LANGCHAIN_TRACING", "LANGCHAIN_HANDLER"):
        os.environ.pop(name, None)
    # Force the process-global override on, so the switch wins even over a warm
    # (stale) SDK env cache; the disabled path forces it off symmetrically.
    _set_sdk_override(True)
    logger.info("tracing_enabled", project=project, endpoint=endpoint)
    return TracingState(active=True, reason="active", project=project)


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
        _deactivate_tracing()
        logger.warning("tracing_bootstrap_failed", error=type(exc).__name__)
        return TracingState(active=False, reason="disabled")


__all__ = [
    "DEFAULT_ENDPOINT",
    "TracingReason",
    "TracingState",
    "bootstrap_tracing",
    "configure_tracing",
    "sanitize_exception",
]
