"""Composition-root factories for the IBPC service and approval key ring."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

from nexus_ai_agent.nagar.ibpc.approval import DurableCanonicalApprovalLedger
from nexus_ai_agent.nagar.ibpc.approval_mac import (
    KeyProvider,
    KeyProviderError,
    StaticKeyRing,
)
from nexus_ai_agent.nagar.ibpc.live_session import DurableLiveSessionStore
from nexus_ai_agent.nagar.ibpc.privacy import DurablePrivacyConsentStore
from nexus_ai_agent.nagar.ibpc.service import (
    IBPCService,
    IBPCServiceDeps,
    utc_now,
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
    """Build IBPC with an explicit, rotation-capable approval key provider."""
    provider = key_provider if key_provider is not None else create_default_key_provider(environ)
    try:
        active_key_id = provider.active_key_id
        provider.key(active_key_id)
    except (AttributeError, KeyProviderError) as exc:
        raise KeyProviderError("approval key provider is not configured correctly") from exc
    return IBPCService(replace(deps, approval_key_provider=provider))


def build_durable_ibpc_service(
    db_path: Path,
    hmac_secret: bytes,
    *,
    deps: IBPCServiceDeps,
    key_provider: KeyProvider | None = None,
    principal_authority: object | None = None,
    with_consent_ledger: bool = True,
) -> IBPCService:
    """Production composition: durable sessions, canonical approvals, consent.

    Three durable stores live under one directory (``db_path``):

    * ``ibpc_live_session.sqlite3`` — sessions + turns (slice 3.1);
    * ``approvals.sqlite3`` — ``DurableCanonicalApprovalLedger`` rows and their
      canonical RFC 8785 + HMAC-SHA256 bindings (slice 3.2);
    * ``ibpc_privacy_consent.sqlite3`` — grants, revocations, audit chain
      (slice 3.3), unless ``with_consent_ledger=False``.

    ``hmac_secret`` must be exactly 256 bits; it is used to build the default
    single-key provider. A caller-supplied ``key_provider`` (rotation-capable)
    takes precedence over ``hmac_secret``.

    ``principal_authority`` is the durable Studio authorization service
    (slice 3.4). It is threaded into the deps so every session start, turn
    creation, approval and consume verifies membership and role on the target
    project. Passing ``None`` composes a service that still fails closed at
    turn creation through ``deps.authority``.
    """
    base = Path(db_path)
    base.mkdir(parents=True, exist_ok=True)
    if not isinstance(hmac_secret, (bytes, bytearray)) or len(hmac_secret) != 32:
        raise KeyProviderError("hmac_secret must be exactly 256 bits (32 bytes)")
    provider = (
        key_provider
        if key_provider is not None
        else StaticKeyRing({"production": bytes(hmac_secret)}, active_key_id="production")
    )
    try:
        provider.key(provider.active_key_id)
    except (AttributeError, KeyProviderError) as exc:
        raise KeyProviderError("approval key provider is not configured correctly") from exc
    now = deps.now or utc_now
    approvals = DurableCanonicalApprovalLedger(
        base / "approvals.sqlite3", key_provider=provider, now=now
    )
    sessions = DurableLiveSessionStore(base / "ibpc_live_session.sqlite3", now=now)
    consent = (
        DurablePrivacyConsentStore(base / "ibpc_privacy_consent.sqlite3", now=now)
        if with_consent_ledger
        else None
    )
    composed = replace(
        deps,
        approval_key_provider=provider,
        approval_ledger=approvals,
        session_store=sessions,
        principal_authority=principal_authority,
        privacy_consent_store=consent,
    )
    return IBPCService(composed)


__all__ = [
    "build_durable_ibpc_service",
    "build_ibpc_service",
    "create_default_key_provider",
]
