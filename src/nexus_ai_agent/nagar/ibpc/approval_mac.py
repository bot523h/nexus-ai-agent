"""Keyed, versioned approval envelope authentication for IBPC.

This is an HMAC (shared-secret authenticator), not an asymmetric signature.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from nexus_ai_agent.provenance import canonicalization
from nexus_ai_agent.provenance.canonicalization import CanonicalizationScheme

DOMAIN = b"NAGAR/IBPC/APPROVAL/v1\x00"


class KeyProviderError(ValueError):
    """Approval key configuration is missing, malformed, or unknown."""


@dataclass(frozen=True)
class ApprovalKey:
    key_id: str
    material: bytes

    def __post_init__(self) -> None:
        if not self.key_id or len(self.material) != 32:
            raise KeyProviderError("approval keys require an id and exactly 256 bits")


class KeyProvider(Protocol):
    @property
    def active_key_id(self) -> str: ...

    def key(self, key_id: str) -> ApprovalKey: ...


class StaticKeyRing:
    """Local, rotation-capable approval key ring; no CONNECT dependency."""

    def __init__(self, keys: Mapping[str, bytes], *, active_key_id: str) -> None:
        if not keys:
            raise KeyProviderError("at least one approval key is required")
        self._keys = {
            key_id: ApprovalKey(key_id, bytes(material)) for key_id, material in keys.items()
        }
        if active_key_id not in self._keys:
            raise KeyProviderError("active approval key is not present in the key ring")
        self._active_key_id = active_key_id

    @property
    def active_key_id(self) -> str:
        return self._active_key_id

    def key(self, key_id: str) -> ApprovalKey:
        try:
            return self._keys[key_id]
        except KeyError as exc:
            raise KeyProviderError("approval key id is unknown") from exc

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> StaticKeyRing:
        env = os.environ if environ is None else environ
        raw = env.get("NEXUS_IBPC_APPROVAL_KEY_RING", "")
        active = env.get("NEXUS_IBPC_APPROVAL_ACTIVE_KEY_ID", "")
        if not raw or not active:
            raise KeyProviderError(
                "NEXUS_IBPC_APPROVAL_KEY_RING and NEXUS_IBPC_APPROVAL_ACTIVE_KEY_ID are required"
            )
        try:
            encoded = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise KeyProviderError("approval key ring must be valid JSON") from exc
        if not isinstance(encoded, dict) or not encoded:
            raise KeyProviderError("approval key ring must be a non-empty object")
        keys: dict[str, bytes] = {}
        for key_id, value in encoded.items():
            if not isinstance(key_id, str) or not isinstance(value, str):
                raise KeyProviderError("approval key ring entries must be strings")
            try:
                keys[key_id] = base64.b64decode(value, validate=True)
            except (ValueError, TypeError) as exc:
                raise KeyProviderError("approval key ring contains invalid base64") from exc
        return cls(keys, active_key_id=active)


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
