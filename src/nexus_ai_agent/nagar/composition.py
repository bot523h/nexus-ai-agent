"""Host composition for the Nagar free-text cognition entry point.

This is the **only** place a language-model provider is constructed for the
free-text intent path.  It is host-side (not inside ``nagar.creative``) so the
feature surface never builds a model itself, never reads global settings deep
inside a handler, and never becomes a second composition root.

Two honest, distinguishable states — never a fake provider:

* ``not_configured``  — no settings/provider factory available; the caller gets
  ``None`` and the cognition gateway degrades to an explicit refusal.  This is a
  normal, supported deployment state, not an error.
* ``build_failed``    — a provider factory existed but raised; reported so an
  operator can tell a misconfiguration from an intentionally model-free deploy.
  The exception text is never echoed (it can carry URLs/credentials).

Credentials never leave this module: only the returned provider object flows
onward, and nothing here logs settings.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from nexus_ai_agent.observability.logging import get_logger

logger = get_logger(__name__)


class ProviderStatus(str, Enum):
    """Why a provider is (not) available — observable, never silent."""

    AVAILABLE = "available"
    NOT_CONFIGURED = "not_configured"
    BUILD_FAILED = "build_failed"


def build_cognition_provider(settings: Any | None = None) -> tuple[Any | None, ProviderStatus]:
    """Compose the (optional) canonical model provider for the free-text path.

    Returns ``(provider, status)``.  Returns ``(None, ...)`` whenever a provider
    is not available or cannot be built; the caller must treat ``None`` as "no
    model", which the gateway turns into an explicit refusal — never a raw
    model call, never a fabricated command.
    """
    if settings is None:
        try:
            from nexus_ai_agent.config.settings import get_settings

            settings = get_settings()
        except Exception:  # noqa: BLE001 — no settings means no provider, not a crash
            return None, ProviderStatus.NOT_CONFIGURED
    try:
        from nexus_ai_agent.llm.litellm_provider import build_llm_provider

        provider, label = build_llm_provider(settings)
    except Exception:  # noqa: BLE001 — surfaced honestly as build_failed, never echoed
        logger.warning("cognition_provider_build_failed")
        return None, ProviderStatus.BUILD_FAILED
    # ``build_llm_provider`` falls back to a *fake* echo provider when nothing is
    # configured.  A fake is not a model: feeding its output into a proposal
    # would be a fabricated model answer, so it is reported as "not configured"
    # and the gateway degrades to an explicit refusal instead.
    if "FakeLLM" in label:
        return None, ProviderStatus.NOT_CONFIGURED
    return provider, ProviderStatus.AVAILABLE


__all__ = ["ProviderStatus", "build_cognition_provider"]
