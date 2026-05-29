from __future__ import annotations

from nexus_ai_agent.storage.ai_storage import ProviderConfig


def test_provider_config_includes_gdrive_bearer_token() -> None:
    cfg = ProviderConfig(gdrive_bearer_token="token")
    assert cfg.gdrive_bearer_token == "token"
