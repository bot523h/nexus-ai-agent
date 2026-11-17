"""Composition-root factories for the IBPC service and approval key ring."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import replace

from nexus_ai_agent.nagar.ibpc.approval_mac import (
    KeyProvider,
    KeyProviderError,
    StaticKeyRing,
)
from nexus_ai_agent.nagar.ibpc.service import IBPCService, IBPCServiceDeps


def create_default_key_provider(
    environ: Mapping[str, str] | None = None,
) -> KeyProvider:
    """Load the configured active signing key and verification key ring.

    Missing or malformed production configuration is a hard startup failure;
    this factory deliberately has no development/test fallback secret.
    """
    return StaticKeyRing.from_environment(os.environ if environ is None else environ)


def build_ibpc_service(
    deps: IBPCServiceDeps,
    *,
    key_provider: KeyProvider | None = None,
    environ: Mapping[str, str] | None = None,
) -> IBPCService:
    """Build IBPC with an explicit, rotation-capable approval key provider."""
    provider = key_provider if key_provider is not None else create_default_key_provider(environ)
    try:
        active_key_id = provider.active_key_id
        provider.key(active_key_id)
    except (AttributeError, KeyProviderError) as exc:
        raise KeyProviderError("approval key provider is not configured correctly") from exc
    return IBPCService(replace(deps, approval_key_provider=provider))


__all__ = ["build_ibpc_service", "create_default_key_provider"]
