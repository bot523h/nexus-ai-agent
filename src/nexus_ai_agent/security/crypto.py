"""Token & secret encryption for NEXUS (Leviathan security layer, D8).

Pre-mortem finding (Leviathan phase 5): if the database — Neon or SQLite — is
ever compromised, any OAuth token / API key stored in plain text leaks user
integrations.  The mitigation is *encryption at rest*:

* Symmetric Fernet (AES-128-CBC + HMAC-SHA256) from the ``cryptography``
  package.
* The master key comes only from ``NEXUS_SECRET_KEY`` (environment), never from
  the database, so a DB dump alone cannot decrypt tokens.
* ``encrypt_secret`` / ``decrypt_secret`` are pure functions taking the master
  key explicitly, so they are trivially unit-testable; thin helpers
  (:func:`encrypt_for_storage` / :func:`decrypt_from_storage`) bind the real
  key from settings at call time.
* A master key may be either a raw Fernet key (44 URL-safe base64 chars) or an
  arbitrary passphrase, which is derived into a 32-byte key via scrypt.
"""

from __future__ import annotations

import base64
import re

from cryptography.fernet import Fernet, InvalidToken

from nexus_ai_agent.observability.logging import get_logger

log = get_logger(__name__)

#: Stored ciphertexts carry this prefix so a plaintext slip is detectable.
_CIPHER_PREFIX = "enc:"

#: Fixed salt for the passphrase→key derivation (not secret; binds domain).
_KDF_SALT = b"nexus-ai-agent::master-key::v1"

_RAW_KEY_RE = re.compile(r"[A-Za-z0-9_-]{44}")


class SecretCryptoError(ValueError):
    """Raised when a secret cannot be encrypted/decrypted (e.g. bad key)."""


def _fernet(master_key: str) -> Fernet:
    """Build a Fernet instance from either a raw key or a passphrase.

    A raw Fernet key is 44 url-safe base64 chars *decoding to 32 bytes*.
    Anything else — including a 44-char passphrase that fails that check — is
    treated as a passphrase and derived via scrypt, so Fernet re-keying can
    never crash the process with an unhandled ValueError.
    """
    if _RAW_KEY_RE.fullmatch(master_key):
        try:
            return Fernet(master_key.encode("ascii"))
        except ValueError:
            # 44 url-safe chars but not a valid key → passphrase, not a key.
            pass
    return Fernet(_derive_passphrase_key(master_key))


def _derive_passphrase_key(passphrase: str) -> bytes:
    """Derive a 32-byte Fernet key from an arbitrary-length passphrase (scrypt)."""
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

    kdf = Scrypt(salt=_KDF_SALT, length=32, n=2**14, r=8, p=1)
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))


def encrypt_secret(master_key: str, plaintext: str) -> str:
    """Encrypt ``plaintext`` with ``master_key``; returns ``enc:<token>``.

    ``master_key`` must come from the environment (``NEXUS_SECRET_KEY``); it is
    never persisted next to the ciphertext.
    """
    if not master_key:
        raise SecretCryptoError("NEXUS_SECRET_KEY is required to encrypt secrets")
    if not plaintext:
        raise SecretCryptoError("plaintext must not be empty")
    token = _fernet(master_key).encrypt(plaintext.encode("utf-8"))
    return _CIPHER_PREFIX + token.decode("ascii")


def decrypt_secret(master_key: str, ciphertext: str) -> str:
    """Decrypt ``enc:<token>`` back to its plaintext; raises on tamper/bad key."""
    if not isinstance(ciphertext, str) or not ciphertext.startswith(_CIPHER_PREFIX):
        raise SecretCryptoError("value is not an encrypted secret (missing enc: prefix)")
    if not master_key:
        raise SecretCryptoError("NEXUS_SECRET_KEY is required to decrypt secrets")
    try:
        raw = ciphertext[len(_CIPHER_PREFIX) :].encode("ascii")
        return _fernet(master_key).decrypt(raw).decode("utf-8")
    except InvalidToken as exc:
        raise SecretCryptoError("decryption failed: wrong key or tampered ciphertext") from exc


def _master_key_from_settings() -> str:
    from nexus_ai_agent.config.settings import get_settings

    key = get_settings().secret_key
    if not key:
        raise SecretCryptoError("NEXUS_SECRET_KEY is not configured")
    return key


def encrypt_for_storage(plaintext: str) -> str:
    """Encrypt using the configured master key (production helper)."""
    return encrypt_secret(_master_key_from_settings(), plaintext)


def decrypt_from_storage(ciphertext: str) -> str:
    """Decrypt using the configured master key (production helper)."""
    if not ciphertext:
        return ""
    return decrypt_secret(_master_key_from_settings(), ciphertext)
