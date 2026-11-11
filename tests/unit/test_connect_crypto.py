from datetime import datetime, timezone

import pytest

from nexus_ai_agent.connect.contracts import (
    ConnectionContract,
    ConnectionState,
    CredentialState,
    ScopeGrant,
)
from nexus_ai_agent.connect.crypto import EncryptedSecret, EnvelopeCipher, SecretEnvelopeError
from nexus_ai_agent.connect.key_provider import KeyProviderError, StaticKeyRing
from nexus_ai_agent.creative.studio.models import ActorIdentity


def ring(active="k1"):
    return StaticKeyRing({"k1": b"1" * 32, "k2": b"2" * 32}, active_key_id=active)


def test_encrypt_decrypt_binds_identity_and_hides_plaintext():
    cipher = EnvelopeCipher(ring())
    envelope = cipher.encrypt(b"provider-secret", connection_id="conn_a", credential_id="cred_a")
    assert b"provider-secret" not in envelope.ciphertext
    assert (
        cipher.decrypt(envelope, connection_id="conn_a", credential_id="cred_a")
        == b"provider-secret"
    )
    with pytest.raises(SecretEnvelopeError):
        cipher.decrypt(envelope, connection_id="conn_b", credential_id="cred_a")


def test_tamper_and_wrong_key_fail_closed():
    cipher = EnvelopeCipher(ring())
    envelope = cipher.encrypt(b"secret", connection_id="conn_a", credential_id="cred_a")
    tampered = EncryptedSecret(
        key_id=envelope.key_id,
        wrapped_dek=envelope.wrapped_dek,
        wrap_nonce=envelope.wrap_nonce,
        data_nonce=envelope.data_nonce,
        ciphertext=envelope.ciphertext[:-1] + bytes([envelope.ciphertext[-1] ^ 1]),
        aad=envelope.aad,
    )
    with pytest.raises(SecretEnvelopeError):
        cipher.decrypt(tampered, connection_id="conn_a", credential_id="cred_a")
    with pytest.raises(SecretEnvelopeError):
        EnvelopeCipher(StaticKeyRing({"other": b"3" * 32}, active_key_id="other")).decrypt(
            envelope, connection_id="conn_a", credential_id="cred_a"
        )


def test_rotation_rewraps_only_the_dek_and_preserves_payload():
    original = EnvelopeCipher(ring("k1")).encrypt(
        b"secret", connection_id="conn_a", credential_id="cred_a"
    )
    rotated = EnvelopeCipher(ring("k2")).rewrap(original)
    assert rotated.key_id == "k2"
    assert rotated.ciphertext == original.ciphertext
    assert rotated.data_nonce == original.data_nonce
    assert (
        EnvelopeCipher(ring("k2")).decrypt(rotated, connection_id="conn_a", credential_id="cred_a")
        == b"secret"
    )


def test_key_provider_rejects_missing_or_malformed_bootstrap():
    with pytest.raises(KeyProviderError):
        StaticKeyRing({}, active_key_id="k1")
    with pytest.raises(KeyProviderError):
        StaticKeyRing({"k1": b"short"}, active_key_id="k1")
    with pytest.raises(KeyProviderError):
        StaticKeyRing.from_environment(
            {"NEXUS_CONNECT_KEK_RING": "{}", "NEXUS_CONNECT_ACTIVE_KEK_ID": "k1"}
        )


def test_envelope_rejects_malformed_database_fields():
    cipher = EnvelopeCipher(ring())
    envelope = cipher.encrypt(b"secret", connection_id="conn_a", credential_id="cred_a")
    with pytest.raises(SecretEnvelopeError):
        EncryptedSecret(
            key_id=None,
            wrapped_dek=envelope.wrapped_dek,
            wrap_nonce=envelope.wrap_nonce,
            data_nonce=envelope.data_nonce,
            ciphertext=envelope.ciphertext,
            aad=envelope.aad,
        )


def test_connection_contract_has_no_secret_field():
    contract = ConnectionContract(
        connection_id="conn_a",
        principal=ActorIdentity(kind="agent", actor_id="agent-a"),
        grant=ScopeGrant(project_ids=("project_a",), scopes=frozenset({"project.read"})),
        credential={"credential_id": "cred_a", "state": CredentialState.ACTIVE},
        state=ConnectionState.ACTIVE,
        created_at=datetime.now(timezone.utc),
    )
    assert "secret" not in contract.model_dump()
