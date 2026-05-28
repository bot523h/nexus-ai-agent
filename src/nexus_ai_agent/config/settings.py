from __future__ import annotations

from functools import lru_cache
from typing import Any

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Application configuration loaded from environment variables and a local .env file.

    Env conventions:
    - TELEGRAM_BOT_TOKEN (legacy/unprefixed) is supported for convenience.
    - All other settings use the NEXUS_ prefix (e.g., NEXUS_DB_PATH).
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        env_prefix="NEXUS_",
    )

    telegram_bot_token: str = Field(
        ...,
        validation_alias=AliasChoices("TELEGRAM_BOT_TOKEN", "NEXUS_TELEGRAM_BOT_TOKEN"),
    )
    db_path: str = "data/app.sqlite"
    checkpoint_path: str = "data/langgraph.sqlite"
    vector_path: str = "data/vector.sqlite"
    model_path: str = "models/model.gguf"
    log_level: str = "INFO"
    enable_shell: bool = False
    allowed_user_ids: list[int] = []

    @field_validator("allowed_user_ids", mode="before")
    @classmethod
    def _parse_allowed_user_ids(cls, v: Any) -> Any:
        # Support: "", "123", "1,2,3", ["1","2"], [1,2]
        if v is None:
            return []
        if isinstance(v, str):
            s = v.strip()
            if not s:
                return []
            return [int(part.strip()) for part in s.split(",") if part.strip()]
        return v


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

