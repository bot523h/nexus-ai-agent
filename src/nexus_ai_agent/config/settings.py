from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    telegram_bot_token: str = Field(
        default="CHANGE_ME",
        validation_alias=AliasChoices("TELEGRAM_BOT_TOKEN", "NEXUS_TELEGRAM_BOT_TOKEN"),
    )
    db_path: str = Field(
        default="data/app.sqlite",
        validation_alias=AliasChoices("NEXUS_DB_PATH", "DB_PATH"),
    )
    checkpoint_path: str = Field(
        default="data/langgraph.sqlite",
        validation_alias=AliasChoices("NEXUS_CHECKPOINT_PATH", "CHECKPOINT_PATH"),
    )
    vector_path: str = Field(
        default="data/vector.sqlite",
        validation_alias=AliasChoices("NEXUS_VECTOR_PATH", "VECTOR_PATH"),
    )
    model_path: str = Field(
        default="models/model.gguf",
        validation_alias=AliasChoices("NEXUS_MODEL_PATH", "MODEL_PATH"),
    )
    log_level: str = Field(
        default="INFO",
        validation_alias=AliasChoices("NEXUS_LOG_LEVEL", "LOG_LEVEL"),
    )
    enable_shell: bool = Field(
        default=False,
        validation_alias=AliasChoices("NEXUS_ENABLE_SHELL", "ENABLE_SHELL"),
    )
    allowed_user_ids: Annotated[list[int], NoDecode] = Field(
        default_factory=list,
        validation_alias=AliasChoices("NEXUS_ALLOWED_USER_IDS", "ALLOWED_USER_IDS"),
    )
    owner_telegram_id: int = Field(
        default=0,
        validation_alias=AliasChoices("NEXUS_OWNER_TELEGRAM_ID", "OWNER_TELEGRAM_ID"),
    )
    workspace_root: str = "."
    n_ctx: int = 2048
    n_gpu_layers: int = 0
    max_short_term_messages: int = 20
    max_tokens_before_summary: int = 3000
    top_k_memories: int = 3

    # Unified multi-cloud storage credentials (optional).
    github_token: str | None = Field(
        default=None,
        validation_alias=AliasChoices("GITHUB_TOKEN", "NEXUS_GITHUB_TOKEN"),
    )
    github_repo: str | None = Field(
        default=None,
        validation_alias=AliasChoices("GITHUB_REPO", "NEXUS_GITHUB_REPO"),
    )

    mega_email: str | None = Field(
        default=None,
        validation_alias=AliasChoices("MEGA_EMAIL", "NEXUS_MEGA_EMAIL"),
    )
    mega_password: str | None = Field(
        default=None, validation_alias=AliasChoices("MEGA_PASSWORD", "NEXUS_MEGA_PASSWORD")
    )

    huggingface_token: str | None = Field(
        default=None, validation_alias=AliasChoices("HUGGINGFACE_TOKEN", "NEXUS_HUGGINGFACE_TOKEN")
    )

    cloudflare_account_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "CLOUDFLARE_ACCOUNT_ID",
            "NEXUS_CLOUDFLARE_ACCOUNT_ID",
        ),
    )

    # Optional: Google Drive (or any rclone-compatible backend) via rclone.
    rclone_remote: str | None = Field(
        default=None,
        validation_alias=AliasChoices("NEXUS_RCLONE_REMOTE", "RCLONE_REMOTE"),
    )
    gdrive_bearer_token: str | None = Field(
        default=None,
        validation_alias=AliasChoices("GDRIVE_BEARER_TOKEN", "NEXUS_GDRIVE_BEARER_TOKEN"),
    )

    # Local cache root used by AIStorageManager.
    cache_dir: str = Field(
        default="data/cache",
        validation_alias=AliasChoices("NEXUS_CACHE_DIR", "CACHE_DIR"),
    )

    # Optional model identity to compute remote keys when auto-downloading.
    model_name: str = Field(
        default="",
        validation_alias=AliasChoices("NEXUS_MODEL_NAME", "MODEL_NAME"),
    )
    model_version: str = Field(
        default="",
        validation_alias=AliasChoices("NEXUS_MODEL_VERSION", "MODEL_VERSION"),
    )

    # ── v2.0.0: Google Gemini AI ──────────────────────────────────────
    gemini_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("GEMINI_API_KEY", "NEXUS_GEMINI_API_KEY"),
    )
    gemini_model: str = Field(
        default="gemini-2.0-flash",
        validation_alias=AliasChoices("NEXUS_GEMINI_MODEL", "GEMINI_MODEL"),
    )
    gemini_max_rpm: int = Field(
        default=15,
        validation_alias=AliasChoices("NEXUS_GEMINI_MAX_RPM", "GEMINI_MAX_RPM"),
    )
    gemini_max_daily: int = Field(
        default=1500,
        validation_alias=AliasChoices("NEXUS_GEMINI_MAX_DAILY", "GEMINI_MAX_DAILY"),
    )

    # v2.0.0: Bot username for referral links
    bot_username: str = Field(
        default="nexus_ai_agent_bot",
        validation_alias=AliasChoices("BOT_USERNAME", "NEXUS_BOT_USERNAME"),
    )

    # ── v2.0.0: Unified Cloud Storage ─────────────────────────────────
    dropbox_token: str | None = Field(
        default=None,
        validation_alias=AliasChoices("DROPBOX_TOKEN", "NEXUS_DROPBOX_TOKEN"),
    )
    pcloud_token: str | None = Field(
        default=None,
        validation_alias=AliasChoices("PCLOUD_TOKEN", "NEXUS_PCLOUD_TOKEN"),
    )
    internxt_token: str | None = Field(
        default=None,
        validation_alias=AliasChoices("INTERNXT_TOKEN", "NEXUS_INTERNXT_TOKEN"),
    )

    news_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("NEWS_API_KEY", "NEXUS_NEWS_API_KEY"),
    )
    youtube_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("YOUTUBE_API_KEY", "NEXUS_YOUTUBE_API_KEY"),
    )
    max_ram_mb: int = Field(
        default=1500,
        validation_alias=AliasChoices("MAX_RAM_MB", "NEXUS_MAX_RAM_MB"),
    )

    # ── v3.4.0: Redis & ChromaDB ──────────────────────────────────────
    redis_url: str = Field(
        default="redis://localhost:6379/0",
        validation_alias=AliasChoices("REDIS_URL", "NEXUS_REDIS_URL"),
    )
    chroma_db_path: str = Field(
        default="data/chroma",
        validation_alias=AliasChoices("CHROMA_DB_PATH", "NEXUS_CHROMA_DB_PATH"),
    )
    celery_broker_url: str = Field(
        default="redis://localhost:6379/0",
        validation_alias=AliasChoices("CELERY_BROKER_URL", "NEXUS_CELERY_BROKER_URL"),
    )
    celery_result_backend: str = Field(
        default="redis://localhost:6379/0",
        validation_alias=AliasChoices("CELERY_RESULT_BACKEND", "NEXUS_CELERY_RESULT_BACKEND"),
    )
    vazir_font_path: str = Field(
        default="assets/fonts/Vazirmatn.ttf",
        validation_alias=AliasChoices("VAZIR_FONT_PATH", "NEXUS_VAZIR_FONT_PATH"),
    )

    @field_validator("allowed_user_ids", mode="before")
    @classmethod
    def _parse_allowed_user_ids(cls, v):  # type: ignore[no-untyped-def]
        # Accept: "", "1,2,3", "[1,2,3]" (JSON), or already-parsed lists.
        if v is None:
            return []
        if isinstance(v, list):
            return v
        if isinstance(v, str):
            s = v.strip()
            if not s:
                return []
            if s.startswith("["):
                # Handle JSON ourselves because NoDecode keeps raw strings.
                import json

                return json.loads(s)
            parts = [p.strip() for p in s.split(",") if p.strip()]
            return [int(p) for p in parts]
        return v


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    # Ensure local storage directories exist on startup.
    Path(settings.db_path).parent.mkdir(parents=True, exist_ok=True)
    Path(settings.checkpoint_path).parent.mkdir(parents=True, exist_ok=True)
    Path(settings.vector_path).parent.mkdir(parents=True, exist_ok=True)
    Path(settings.model_path).parent.mkdir(parents=True, exist_ok=True)
    Path(settings.cache_dir).mkdir(parents=True, exist_ok=True)
    return settings
