"""Keyed, versioned approval envelope authentication for IBPC.

This is an HMAC (shared-secret authenticator), not an asymmetric signature.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import datetime

from nexus_ai_agent.connect.key_provider import KeyProvider, KeyProviderError
from nexus_ai_agent.provenance import canonicalization
from nexus_ai_agent.provenance.canonicalization import CanonicalizationScheme

DOMAIN = b"NAGAR/IBPC/APPROVAL/v1\x00"


@dataclass(frozen=True)
class ApprovalEnvelope:
    version: str
    key_id: str
    approval_id: str
    scope_digest: str
    issued_at: str
    expires_at: str
    mac: str


def scope_digest(payload: object) -> str:
    return (
        "sha256:"
        + hashlib.sha256(
            canonicalization.canonical_bytes(payload, CanonicalizationScheme.JCS_RFC8785_V2)
        ).hexdigest()
    )


def issue_envelope(
    provider: KeyProvider, *, approval_id: str, payload: object, issued_at: str, expires_at: str
) -> ApprovalEnvelope:
    key_id = provider.active_key_id
    key = provider.key(key_id)
    body = {
        "version": "nexus.ibpc.approval-envelope.v1",
        "key_id": key_id,
        "approval_id": approval_id,
        "scope_digest": scope_digest(payload),
        "issued_at": issued_at,
        "expires_at": expires_at,
    }
    mac = hmac.new(
        key.material,
        DOMAIN + canonicalization.canonical_bytes(body, CanonicalizationScheme.JCS_RFC8785_V2),
        hashlib.sha256,
    ).hexdigest()
    return ApprovalEnvelope(
        body["version"], key_id, approval_id, body["scope_digest"], issued_at, expires_at, mac
    )


def verify_envelope(
    provider: KeyProvider, envelope: ApprovalEnvelope, payload: object, *, now: str
) -> bool:
    try:
        if envelope.version != "nexus.ibpc.approval-envelope.v1":
            return False
        current = datetime.fromisoformat(now.replace("Z", "+00:00"))
        expires = datetime.fromisoformat(envelope.expires_at.replace("Z", "+00:00"))
        if current.tzinfo is None or expires.tzinfo is None or current >= expires:
            return False
        key = provider.key(envelope.key_id)
        actual_scope = scope_digest(payload)
        if not hmac.compare_digest(actual_scope, envelope.scope_digest):
            return False
        body = {
            "version": envelope.version,
            "key_id": envelope.key_id,
            "approval_id": envelope.approval_id,
            "scope_digest": envelope.scope_digest,
            "issued_at": envelope.issued_at,
            "expires_at": envelope.expires_at,
        }
        actual_mac = hmac.new(
            key.material,
            DOMAIN + canonicalization.canonical_bytes(body, CanonicalizationScheme.JCS_RFC8785_V2),
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(actual_mac, envelope.mac)
    except (KeyProviderError, TypeError, ValueError):
        return False
