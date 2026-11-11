"""Key-encryption-key providers for CONNECT envelope encryption.

The provider owns KEK material in memory and never persists it. Production
should replace ``EnvironmentKeyRing`` with a KMS/HSM adapter; the persistence
layer depends only on this small protocol and therefore cannot accidentally
write a KEK into the database.
"""

from __future__ import annotations

import base64
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

_KEY_BYTES = 32


class KeyProviderError(ValueError):
    """A key provider is missing, malformed, or cannot satisfy a request."""


@dataclass(frozen=True)
class KeyEncryptionKey:
    key_id: str
    material: bytes

    def __post_init__(self) -> None:
        if (
            not self.key_id
            or not self.key_id.isascii()
            or any(not (char.isalnum() or char in "_.:-") for char in self.key_id)
        ):
            raise KeyProviderError("key_id must be a non-blank ASCII identifier")
        if len(self.material) != _KEY_BYTES:
            raise KeyProviderError("CONNECT KEKs must be exactly 256 bits")


class KeyProvider(Protocol):
    @property
    def active_key_id(self) -> str: ...

    def key(self, key_id: str) -> KeyEncryptionKey: ...


class StaticKeyRing:
    """Immutable in-process key ring used by tests and local bootstrap.

    A new instance with a different ``active_key_id`` models rotation. Existing
    key ids remain available for decrypt/rewrap until their retention window
    has elapsed, so old ciphertext is never silently lost.
    """

    def __init__(self, keys: Mapping[str, bytes], *, active_key_id: str) -> None:
        if not keys:
            raise KeyProviderError("at least one KEK is required")
        self._keys = {
            key_id: KeyEncryptionKey(key_id, bytes(material)) for key_id, material in keys.items()
        }
        if active_key_id not in self._keys:
            raise KeyProviderError("active_key_id is not present in the key ring")
        self._active_key_id = active_key_id

    @property
    def active_key_id(self) -> str:
        return self._active_key_id

    def key(self, key_id: str) -> KeyEncryptionKey:
        try:
            return self._keys[key_id]
        except KeyError as exc:
            raise KeyProviderError("requested KEK is not available") from exc

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> StaticKeyRing:
        env = environ or os.environ
        raw_ring = env.get("NEXUS_CONNECT_KEK_RING", "")
        active = env.get("NEXUS_CONNECT_ACTIVE_KEK_ID", "")
        if not raw_ring or not active:
            raise KeyProviderError(
                "NEXUS_CONNECT_KEK_RING and NEXUS_CONNECT_ACTIVE_KEK_ID are required"
            )
        try:
            encoded_ring = json.loads(raw_ring)
        except json.JSONDecodeError as exc:
            raise KeyProviderError("NEXUS_CONNECT_KEK_RING must be valid JSON") from exc
        if not isinstance(encoded_ring, dict) or not encoded_ring:
            raise KeyProviderError("NEXUS_CONNECT_KEK_RING must be a non-empty object")
        decoded: dict[str, bytes] = {}
        for key_id, encoded in encoded_ring.items():
            if not isinstance(key_id, str) or not isinstance(encoded, str):
                raise KeyProviderError("KEK ring entries must map string ids to base64 strings")
            try:
                decoded[key_id] = base64.b64decode(encoded, validate=True)
            except (ValueError, TypeError) as exc:
                raise KeyProviderError("KEK ring contains invalid base64") from exc
        return cls(decoded, active_key_id=active)


__all__ = [
    "KeyEncryptionKey",
    "KeyProvider",
    "KeyProviderError",
    "StaticKeyRing",
]
