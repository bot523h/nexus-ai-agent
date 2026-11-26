"""Composition-root factories for the IBPC service and approval key ring."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import replace

from nexus_ai_agent.nagar.ibpc.approval import (
    DurableApprovalLedger,
    durable_approval_ledger_path,
)
from nexus_ai_agent.nagar.ibpc.approval_mac import (
    KeyProvider,
    KeyProviderError,
    StaticKeyRing,
)
from nexus_ai_agent.nagar.ibpc.privacy import (
    DurablePrivacyConsentStore,
    durable_privacy_consent_path,
)
from nexus_ai_agent.nagar.ibpc.service import IBPCService, IBPCServiceDeps
from nexus_ai_agent.nagar.ibpc.session import (
    DurableLiveSessionStore,
    durable_session_store_path,
)


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
    """Build IBPC with an explicit, rotation-capable approval key provider and durable stores."""
    provider = key_provider if key_provider is not None else create_default_key_provider(environ)
    try:
        active_key_id = provider.active_key_id
        provider.key(active_key_id)
    except (AttributeError, KeyProviderError) as exc:
        raise KeyProviderError("approval key provider is not configured correctly") from exc
    updated = replace(deps, approval_key_provider=provider, durable=True)
    if updated.authorized_asset_root is None:
        updated = replace(updated, authorized_asset_root=updated.workspace_root)
    if updated.approval_ledger is None:
        updated = replace(
            updated,
            approval_ledger=DurableApprovalLedger(
                durable_approval_ledger_path(updated.workspace_root), now=updated.now
            ),
        )
    if updated.session_store is None:
        updated = replace(
            updated,
            session_store=DurableLiveSessionStore(
                durable_session_store_path(updated.workspace_root)
            ),
        )
    if updated.privacy_store is None:
        updated = replace(
            updated,
            privacy_store=DurablePrivacyConsentStore(
                durable_privacy_consent_path(updated.workspace_root)
            ),
        )
    return IBPCService(updated)


__all__ = ["build_ibpc_service", "create_default_key_provider"]
