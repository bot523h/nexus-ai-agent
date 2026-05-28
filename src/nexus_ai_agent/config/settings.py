from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    telegram_bot_token: str = "CHANGE_ME"
    db_path: str = "data/app.sqlite"
    checkpoint_path: str = "data/langgraph.sqlite"
    vector_path: str = "data/vector.sqlite"
    model_path: str = "models/model.gguf"
    log_level: str = "INFO"
    enable_shell: bool = False
    allowed_user_ids: list[int] = []
    workspace_root: str = "."
    n_ctx: int = 2048
    n_gpu_layers: int = 0
    max_short_term_messages: int = 20
    max_tokens_before_summary: int = 3000
    top_k_memories: int = 3


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    # Ensure local storage directories exist on startup.
    Path(settings.db_path).parent.mkdir(parents=True, exist_ok=True)
    return settings
