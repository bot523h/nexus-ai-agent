from __future__ import annotations

import base64

import pytest

from nexus_ai_agent.nagar.ibpc.approval_mac import KeyProviderError
from nexus_ai_agent.nagar.ibpc.composition import create_default_key_provider


def test_default_key_provider_loads_rotated_keyring_and_rejects_unknown_ids() -> None:
    old = base64.b64encode(b"o" * 32).decode("ascii")
    new = base64.b64encode(b"n" * 32).decode("ascii")
    provider = create_default_key_provider(
        {
            "NEXUS_IBPC_APPROVAL_KEY_RING": f'{{"old": "{old}", "new": "{new}"}}',
            "NEXUS_IBPC_APPROVAL_ACTIVE_KEY_ID": "new",
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
                "NEXUS_IBPC_APPROVAL_KEY_RING": "not-json",
                "NEXUS_IBPC_APPROVAL_ACTIVE_KEY_ID": "active",
            }
        )


def test_default_key_provider_rejects_invalid_base64() -> None:
    with pytest.raises(KeyProviderError, match="invalid base64"):
        create_default_key_provider(
            {
                "NEXUS_IBPC_APPROVAL_KEY_RING": '{"active": "%%%"}',
                "NEXUS_IBPC_APPROVAL_ACTIVE_KEY_ID": "active",
            }
        )


@pytest.mark.parametrize(
    "ring,active,error",
    [
        ("{}", "active", "non-empty object"),
        ('{"active": 17}', "active", "entries must be strings"),
        ('{"active": "YQ=="}', "active", "exactly 256 bits"),
        ('{"other": "YWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWE="}', "active", "not present"),
    ],
)
def test_default_key_provider_rejects_invalid_ring_shapes(
    ring: str, active: str, error: str
) -> None:
    with pytest.raises(KeyProviderError, match=error):
        create_default_key_provider(
            {
                "NEXUS_IBPC_APPROVAL_KEY_RING": ring,
                "NEXUS_IBPC_APPROVAL_ACTIVE_KEY_ID": active,
            }
        )


def test_local_static_keyring_refuses_empty_or_missing_active_key() -> None:
    from nexus_ai_agent.nagar.ibpc.approval_mac import StaticKeyRing

    with pytest.raises(KeyProviderError, match="at least one"):
        StaticKeyRing({}, active_key_id="active")
