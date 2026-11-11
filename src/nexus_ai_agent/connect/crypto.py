"""Envelope encryption primitives for CONNECT credential material.

Each record receives a fresh 256-bit DEK and fresh 96-bit nonces. The DEK is
wrapped by the active KEK and the secret is encrypted with AES-256-GCM. The
connection identity and schema version are authenticated as AAD, so ciphertext
cannot be moved between rows or silently reinterpreted by a future schema.
"""

from __future__ import annotations

import base64
import json
import re
import secrets
from dataclasses import dataclass
from typing import Final

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from nexus_ai_agent.connect.key_provider import KeyProvider, KeyProviderError

_NONCE_BYTES: Final = 12
_KEY_BYTES: Final = 32
_MAX_SECRET_BYTES: Final = 64 * 1024
_SCHEMA_VERSION: Final = 1
_WRAP_AAD_PREFIX: Final = b"nexus.connect.dek.wrap.v1|"
_KEY_ID_PATTERN: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")


class SecretEnvelopeError(ValueError):
    """Malformed or unauthentic encrypted credential material."""


@dataclass(frozen=True)
class EncryptedSecret:
    key_id: str
    wrapped_dek: bytes
    wrap_nonce: bytes
    data_nonce: bytes
    ciphertext: bytes
    aad: bytes
    schema_version: int = _SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.key_id, str) or not _KEY_ID_PATTERN.fullmatch(self.key_id):
            raise SecretEnvelopeError("CONNECT envelope key id is malformed")
        if self.schema_version != _SCHEMA_VERSION:
            raise SecretEnvelopeError("unsupported CONNECT envelope version")
        for name, value in (
            ("wrapped_dek", self.wrapped_dek),
            ("wrap_nonce", self.wrap_nonce),
            ("data_nonce", self.data_nonce),
            ("ciphertext", self.ciphertext),
            ("aad", self.aad),
        ):
            if not isinstance(value, bytes):
                raise SecretEnvelopeError(f"CONNECT envelope {name} must be bytes")
        if len(self.wrap_nonce) != _NONCE_BYTES or len(self.data_nonce) != _NONCE_BYTES:
            raise SecretEnvelopeError("CONNECT envelope nonces must be 96 bits")
        if len(self.wrapped_dek) != _KEY_BYTES + 16:
            raise SecretEnvelopeError("CONNECT wrapped DEK has an invalid length")
        if len(self.ciphertext) < 16:
            raise SecretEnvelopeError("CONNECT ciphertext is missing its authentication tag")
        if not self.aad:
            raise SecretEnvelopeError("CONNECT envelope AAD is required")

    def encoded(self) -> dict[str, str | int]:
        return {
            "key_id": self.key_id,
            "wrapped_dek": base64.b64encode(self.wrapped_dek).decode("ascii"),
            "wrap_nonce": base64.b64encode(self.wrap_nonce).decode("ascii"),
            "data_nonce": base64.b64encode(self.data_nonce).decode("ascii"),
            "ciphertext": base64.b64encode(self.ciphertext).decode("ascii"),
            "aad": base64.b64encode(self.aad).decode("ascii"),
            "schema_version": self.schema_version,
        }


class EnvelopeCipher:
    def __init__(self, provider: KeyProvider) -> None:
        self._provider = provider

    @staticmethod
    def aad_for(*, connection_id: str, credential_id: str) -> bytes:
        payload = {
            "connection_id": connection_id,
            "credential_id": credential_id,
            "schema_version": _SCHEMA_VERSION,
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")

    def encrypt(self, secret: bytes, *, connection_id: str, credential_id: str) -> EncryptedSecret:
        if not isinstance(secret, bytes) or not secret:
            raise SecretEnvelopeError("credential secret must be non-empty bytes")
        if len(secret) > _MAX_SECRET_BYTES:
            raise SecretEnvelopeError("credential secret exceeds the 64 KiB limit")
        kek_id = self._provider.active_key_id
        kek = self._provider.key(kek_id)
        dek = AESGCM.generate_key(bit_length=256)
        wrap_nonce = secrets.token_bytes(_NONCE_BYTES)
        data_nonce = secrets.token_bytes(_NONCE_BYTES)
        wrap_aad = _WRAP_AAD_PREFIX + kek_id.encode("ascii")
        wrapped_dek = AESGCM(kek.material).encrypt(wrap_nonce, dek, wrap_aad)
        aad = self.aad_for(connection_id=connection_id, credential_id=credential_id)
        ciphertext = AESGCM(dek).encrypt(data_nonce, secret, aad)
        return EncryptedSecret(
            key_id=kek_id,
            wrapped_dek=wrapped_dek,
            wrap_nonce=wrap_nonce,
            data_nonce=data_nonce,
            ciphertext=ciphertext,
            aad=aad,
        )

    def decrypt(
        self, envelope: EncryptedSecret, *, connection_id: str, credential_id: str
    ) -> bytes:
        expected_aad = self.aad_for(connection_id=connection_id, credential_id=credential_id)
        if envelope.aad != expected_aad:
            raise SecretEnvelopeError("CONNECT envelope identity binding failed")
        try:
            kek = self._provider.key(envelope.key_id)
            wrap_aad = _WRAP_AAD_PREFIX + envelope.key_id.encode("ascii")
            dek = AESGCM(kek.material).decrypt(envelope.wrap_nonce, envelope.wrapped_dek, wrap_aad)
            if len(dek) != _KEY_BYTES:
                raise SecretEnvelopeError("CONNECT DEK has an invalid length")
            return AESGCM(dek).decrypt(envelope.data_nonce, envelope.ciphertext, expected_aad)
        except (InvalidTag, KeyProviderError, TypeError, AttributeError, ValueError) as exc:
            if isinstance(exc, SecretEnvelopeError):
                raise
            raise SecretEnvelopeError("CONNECT envelope authentication failed") from exc

    def rewrap(self, envelope: EncryptedSecret) -> EncryptedSecret:
        """Re-wrap a DEK under the active KEK without re-encrypting the secret."""
        if envelope.key_id == self._provider.active_key_id:
            return envelope
        old_kek = self._provider.key(envelope.key_id)
        new_kek = self._provider.key(self._provider.active_key_id)
        old_wrap_aad = _WRAP_AAD_PREFIX + envelope.key_id.encode("ascii")
        try:
            dek = AESGCM(old_kek.material).decrypt(
                envelope.wrap_nonce, envelope.wrapped_dek, old_wrap_aad
            )
        except (InvalidTag, TypeError, AttributeError, ValueError) as exc:
            raise SecretEnvelopeError("CONNECT DEK unwrap failed during rotation") from exc
        new_nonce = secrets.token_bytes(_NONCE_BYTES)
        new_wrap_aad = _WRAP_AAD_PREFIX + new_kek.key_id.encode("ascii")
        wrapped = AESGCM(new_kek.material).encrypt(new_nonce, dek, new_wrap_aad)
        return EncryptedSecret(
            key_id=new_kek.key_id,
            wrapped_dek=wrapped,
            wrap_nonce=new_nonce,
            data_nonce=envelope.data_nonce,
            ciphertext=envelope.ciphertext,
            aad=envelope.aad,
            schema_version=envelope.schema_version,
        )


__all__ = ["EncryptedSecret", "EnvelopeCipher", "SecretEnvelopeError"]
