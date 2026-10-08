"""Isolated, lazy access to the optional ``hatchet-sdk`` (import boundary).

The optional dependency must never be imported by Nexus product entrypoints or
by the provider-neutral contract. Everything that needs the SDK goes through
here, and a missing extra fails with one typed, actionable error instead of a
raw ``ImportError`` (matching the repository's optional-extras contract).
"""

from __future__ import annotations

import importlib
import os
from types import ModuleType
from typing import Any

EMBEDDED_ENV_FLAG = "NEXUS_HATCHET_ALLOW_EMBEDDED"


class HatchetUnavailable(RuntimeError):
    """The optional ``hatchet-sdk`` extra is not installed."""


class HatchetEmbeddedForbidden(RuntimeError):
    """Embedded mode was requested outside a dev/CI context (fail closed)."""


def sdk() -> ModuleType:
    """Return the ``hatchet_sdk`` module or raise a typed error."""
    try:
        return importlib.import_module("hatchet_sdk")
    except ImportError as exc:  # pragma: no cover - exercised by the core leg
        raise HatchetUnavailable(
            "the 'hatchet' execution backend needs the optional dependency; "
            "install it with: pip install -e '.[hatchet]'"
        ) from exc


def embedded_allowed() -> bool:
    """Embedded engine is dev/CI proof only — opt-in via an explicit env flag."""
    return os.environ.get(EMBEDDED_ENV_FLAG, "").strip().lower() in {"1", "true", "yes"}


def new_client(*, embedded: bool = False) -> Any:  # SDK type is optional
    """Build a Hatchet client.

    * production/dev-cloud: reads ``HATCHET_CLIENT_TOKEN`` from the environment
      (the token is never hard-coded, never written to a file and never logged);
    * embedded: only when :func:`embedded_allowed` — never from product code.
    """
    if embedded:
        if not embedded_allowed():
            raise HatchetEmbeddedForbidden(
                "embedded Hatchet is a dev/CI proof path only; set "
                f"{EMBEDDED_ENV_FLAG}=1 to allow it explicitly"
            )
        module = sdk()
        factory = getattr(module.Hatchet, "from_embedded", None) or module.Hatchet.from_embed
        return factory()
    module = sdk()
    config_cls = module.ClientConfig
    config = config_cls()  # reads HATCHET_CLIENT_TOKEN from the environment
    return module.Hatchet(config=config)
