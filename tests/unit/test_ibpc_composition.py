from __future__ import annotations

import base64

import pytest

from nexus_ai_agent.connect.key_provider import KeyProviderError
from nexus_ai_agent.nagar.ibpc.composition import create_default_key_provider


def test_default_key_provider_loads_rotated_keyring_and_rejects_unknown_ids() -> None:
    old = base64.b64encode(b"o" * 32).decode("ascii")
    new = base64.b64encode(b"n" * 32).decode("ascii")
    provider = create_default_key_provider(
        {
            "NEXUS_CONNECT_KEK_RING": f'{{"old": "{old}", "new": "{new}"}}',
            "NEXUS_CONNECT_ACTIVE_KEK_ID": "new",
        }
    )
    assert provider.active_key_id == "new"
    assert provider.key("old").material == b"o" * 32
    assert provider.key("new").material == b"n" * 32
    with pytest.raises(KeyProviderError):
        provider.key("forged")


def test_default_key_provider_fails_closed_without_secret_material() -> None:
    with pytest.raises(KeyProviderError, match="required"):
        create_default_key_provider({})


def test_default_key_provider_rejects_malformed_ring() -> None:
    with pytest.raises(KeyProviderError, match="valid JSON"):
        create_default_key_provider(
            {
                "NEXUS_CONNECT_KEK_RING": "not-json",
                "NEXUS_CONNECT_ACTIVE_KEK_ID": "active",
            }
        )


def test_default_key_provider_rejects_invalid_base64() -> None:
    with pytest.raises(KeyProviderError, match="invalid base64"):
        create_default_key_provider(
            {
                "NEXUS_CONNECT_KEK_RING": '{"active": "%%%"}',
                "NEXUS_CONNECT_ACTIVE_KEK_ID": "active",
            }
        )
