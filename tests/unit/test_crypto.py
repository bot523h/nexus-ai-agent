"""Unit tests for the token-encryption layer (D8)."""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from nexus_ai_agent.security.crypto import (
    SecretCryptoError,
    decrypt_secret,
    encrypt_secret,
)

#: A canonical Fernet key: 44 url-safe base64 chars decoding to 32 bytes.
RAW_KEY = Fernet.generate_key().decode("ascii")


class TestEncryptDecryptRoundTrip:
    def test_round_trip_with_raw_key(self) -> None:
        ciphertext = encrypt_secret(RAW_KEY, "supersecret-token")
        assert ciphertext.startswith("enc:")
        assert decrypt_secret(RAW_KEY, ciphertext) == "supersecret-token"

    def test_round_trip_with_passphrase(self) -> None:
        ciphertext = encrypt_secret("a long master passphrase", "google-drive-refresh")
        assert decrypt_secret("a long master passphrase", ciphertext) == "google-drive-refresh"

    def test_44_char_invalid_key_treated_as_passphrase(self) -> None:
        # "k" * 44 is 44 url-safe chars but decodes to 33 bytes → must fall
        # back to the scrypt passphrase path instead of crashing.
        key = "k" * 44
        ciphertext = encrypt_secret(key, "token")
        assert decrypt_secret(key, ciphertext) == "token"

    def test_same_plaintext_encrypts_to_different_ciphertexts(self) -> None:
        # Fernet IV is random per call: no deterministic ciphertext leakage.
        a = encrypt_secret(RAW_KEY, "token")
        b = encrypt_secret(RAW_KEY, "token")
        assert a != b
        assert decrypt_secret(RAW_KEY, a) == decrypt_secret(RAW_KEY, b) == "token"


class TestSecretCryptoFailures:
    def test_missing_key_raises(self) -> None:
        with pytest.raises(SecretCryptoError, match="required"):
            encrypt_secret("", "token")

    def test_empty_plaintext_raises(self) -> None:
        with pytest.raises(SecretCryptoError, match="empty"):
            encrypt_secret(RAW_KEY, "")

    def test_decrypt_non_ciphertext_raises(self) -> None:
        with pytest.raises(SecretCryptoError, match="enc:"):
            decrypt_secret(RAW_KEY, "plaintext-token")

    def test_decrypt_with_wrong_key_raises(self) -> None:
        ciphertext = encrypt_secret("key-one-pass", "token")
        with pytest.raises(SecretCryptoError, match="decryption failed"):
            decrypt_secret("key-two-pass", ciphertext)

    def test_decrypt_tampered_ciphertext_raises(self) -> None:
        ciphertext = encrypt_secret(RAW_KEY, "token")
        tampered = ciphertext[:-1] + ("1" if ciphertext[-1] != "1" else "2")
        with pytest.raises(SecretCryptoError, match="decryption failed"):
            decrypt_secret(RAW_KEY, tampered)
