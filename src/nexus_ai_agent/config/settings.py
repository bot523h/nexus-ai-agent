from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

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
    database_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("NEXUS_DATABASE_URL", "DATABASE_URL"),
    )
    checkpoint_path: str = Field(
        default="data/langgraph.sqlite",
        validation_alias=AliasChoices("NEXUS_CHECKPOINT_PATH", "CHECKPOINT_PATH"),
    )
    lifecycle_hooks_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("NEXUS_LIFECYCLE_HOOKS_ENABLED", "LIFECYCLE_HOOKS_ENABLED"),
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
    auto_update: bool = Field(
        default=False,
        validation_alias=AliasChoices("NEXUS_AUTO_UPDATE", "AUTO_UPDATE"),
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

    # ── Local RAG storage ─────────────────────────────────────────────
    chroma_db_path: str = Field(
        default="data/chroma",
        validation_alias=AliasChoices("CHROMA_DB_PATH", "NEXUS_CHROMA_DB_PATH"),
    )
    vazir_font_path: str = Field(
        default="assets/fonts/Vazirmatn.ttf",
        validation_alias=AliasChoices("VAZIR_FONT_PATH", "NEXUS_VAZIR_FONT_PATH"),
    )

    # ── v3.7.0: Multi-provider LLM routing (litellm) ─────────────────
    # Priority chain: Ollama (local, unlimited) → Groq → Gemini → OpenRouter:free.
    # Only providers with credentials/settings enter the chain. The chain is
    # orchestrated by litellm.Router with long cooldowns on the first 429 for
    # providers with daily caps (Groq/Gemini/OpenRouter) to prevent retry-storms.
    llm_routing_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("NEXUS_LLM_ROUTING_ENABLED", "LLM_ROUTING_ENABLED"),
    )
    # When true, drop providers that may train on user prompts
    # (OpenRouter ":free" endpoints) from the routing chain.
    llm_strict_privacy: bool = Field(
        default=False,
        validation_alias=AliasChoices("NEXUS_LLM_STRICT_PRIVACY", "LLM_STRICT_PRIVACY"),
    )
    # Cooldown (seconds) applied to a cloud deployment after its first 429.
    # 86400 = park it for ~24h instead of hammering a drained daily quota.
    llm_cloud_cooldown: int = Field(
        default=86_400,
        validation_alias=AliasChoices("NEXUS_LLM_CLOUD_COOLDOWN", "LLM_CLOUD_COOLDOWN"),
    )
    # Per-request timeout (seconds) for every routed provider.
    llm_request_timeout: int = Field(
        default=60,
        validation_alias=AliasChoices("NEXUS_LLM_REQUEST_TIMEOUT", "LLM_REQUEST_TIMEOUT"),
    )
    # 1) Ollama — local, unlimited. Leave ollama_model empty to disable.
    ollama_base_url: str = Field(
        default="http://localhost:11434",
        validation_alias=AliasChoices("NEXUS_OLLAMA_BASE_URL", "OLLAMA_BASE_URL"),
    )
    ollama_model: str = Field(
        default="",
        validation_alias=AliasChoices("NEXUS_OLLAMA_MODEL", "OLLAMA_MODEL"),
    )
    # 2) Groq — free tier (https://console.groq.com).
    groq_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("GROQ_API_KEY", "NEXUS_GROQ_API_KEY"),
    )
    groq_model: str = Field(
        default="llama-3.3-70b-versatile",
        validation_alias=AliasChoices("NEXUS_GROQ_MODEL", "GROQ_MODEL"),
    )
    # 3) Gemini — the existing free-tier engine (gemini_api_key/gemini_model above).
    # 4) OpenRouter — last resort. NOTE: some ":free" endpoints may train on
    # prompts; set NEXUS_LLM_STRICT_PRIVACY=true to exclude them from the chain.
    openrouter_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("OPENROUTER_API_KEY", "NEXUS_OPENROUTER_API_KEY"),
    )
    openrouter_model: str = Field(
        default="meta-llama/llama-3.3-70b-instruct:free",
        validation_alias=AliasChoices("NEXUS_OPENROUTER_MODEL", "OPENROUTER_MODEL"),
    )

    # ── v3.8.0: Telegram webhook mode (scale-to-zero deployments) ─────
    # "polling" (default) keeps the legacy always-on long-poll loop;
    # "webhook" serves Telegram updates over HTTP for web-type services
    # that scale to zero (Koyeb web, ...). The CLI --mode flag wins over
    # this value; see bot/webhook.py:resolve_run_mode.
    run_mode: str = Field(
        default="polling",
        validation_alias=AliasChoices("NEXUS_RUN_MODE", "RUN_MODE"),
    )
    # Public HTTPS URL Telegram should POST updates to (webhook mode only),
    # e.g. https://<app>.koyeb.app/webhook/telegram.
    webhook_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("NEXUS_WEBHOOK_URL", "WEBHOOK_URL"),
    )
    # Shared secret Telegram echoes back in the
    # X-Telegram-Bot-Api-Secret-Token header; verified constant-time in
    # api/app.py. Generate e.g. with: python -c "import secrets;
    # print(secrets.token_hex(32))".
    webhook_secret: str | None = Field(
        default=None,
        validation_alias=AliasChoices("NEXUS_WEBHOOK_SECRET", "WEBHOOK_SECRET"),
    )
    # Browser origins allowed to call the dashboard API cross-origin
    # (comma-separated, e.g. "https://nexus.example.com,https://a.example.org").
    # Empty (default) disables cross-origin browser access entirely — the
    # served dashboard is same-origin and needs no CORS. Never use "*".
    api_cors_origins: str = Field(
        default="",
        validation_alias="NEXUS_API_CORS_ORIGINS",
    )
    # Fail-closed: unset/empty disables mutating dashboard API endpoints
    # (currently POST /creative/video-edit answers 503 "Security
    # configuration incomplete"). When set, requests must be signed with
    # X-NEXUS-Signature: hex(HMAC-SHA256(key, "{timestamp}:{raw_body}"))
    # plus a fresh X-NEXUS-Timestamp (unix seconds, ±300 s).
    api_hmac_key: str | None = Field(
        default=None,
        validation_alias="NEXUS_API_HMAC_KEY",
    )
    creative_gemini_api_key: str | None = Field(
        default=None,
        validation_alias="NEXUS_CREATIVE_GEMINI_API_KEY",
    )
    creative_temp_dir: str = Field(
        default="/tmp/nexus_creative",
        description="Temporary directory for creative jobs",
    )

    # Image generation is distinct from image analysis/upload authorization.
    image_gen_provider: Literal["pollinations", "gemini"] = Field(
        default="pollinations", validation_alias="NEXUS_IMAGE_GEN_PROVIDER"
    )
    image_gen_paid_tier: bool = Field(default=False, validation_alias="NEXUS_IMAGE_GEN_PAID_TIER")
    image_gen_model: str = Field(
        default="gemini-2.5-flash-image",
        validation_alias="NEXUS_IMAGE_GEN_MODEL",
        min_length=1,
        max_length=100,
    )
    image_gen_estimated_cost_usd: Decimal = Field(
        default=Decimal("0"),
        ge=0,
        allow_inf_nan=False,
        validation_alias="NEXUS_IMAGE_GEN_ESTIMATED_COST_USD",
    )

    # ── v3.10.0: Nagar slideshow pack (Wave 2) ────────────────────────
    # Image analysis for slideshow planning: "local" (default) keeps every
    # pixel on this machine; "gemini" uses hosted multimodal scoring and is
    # additionally gated by the explicit upload switch below.
    slideshow_analysis_provider: Literal["local", "gemini"] = Field(
        default="local",
        validation_alias="NEXUS_SLIDESHOW_ANALYSIS_PROVIDER",
    )
    # Fail-closed media-egress switch: downscaled copies are only ever sent to
    # a hosted model when this is explicitly enabled. It mirrors the pack
    # manifest's declared `egress_media_optin` permission.
    slideshow_allow_image_upload: bool = Field(
        default=False,
        validation_alias="NEXUS_SLIDESHOW_ALLOW_IMAGE_UPLOAD",
    )
    # Wave 2c render lane. Exactly one allow-listed binary is executed, never
    # through a shell. Resolution order: this override, then PATH, then the
    # `imageio-ffmpeg` wheel (a dev/test convenience).
    ffmpeg_bin: str | None = Field(
        default=None,
        validation_alias="NEXUS_FFMPEG_BIN",
    )
    slideshow_render_timeout_seconds: int = Field(
        default=900,
        ge=1,
        le=7200,
        validation_alias="NEXUS_SLIDESHOW_RENDER_TIMEOUT",
    )

    # ── v3.9.0: Cloudflare R2 — technical blob tier (DB backups, heavy RAG docs) ──
    # Not part of the user-file round-robin. Create an R2 API token scoped to a
    # single bucket (Object Read & Write); see docs/r2-storage.md.
    r2_account_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices("R2_ACCOUNT_ID", "NEXUS_R2_ACCOUNT_ID"),
    )
    r2_access_key_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices("R2_ACCESS_KEY_ID", "NEXUS_R2_ACCESS_KEY_ID"),
    )
    r2_secret_access_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("R2_SECRET_ACCESS_KEY", "NEXUS_R2_SECRET_ACCESS_KEY"),
    )
    r2_bucket: str | None = Field(
        default=None,
        validation_alias=AliasChoices("R2_BUCKET", "NEXUS_R2_BUCKET"),
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
    Path(settings.creative_temp_dir).mkdir(parents=True, exist_ok=True)
    return settings
