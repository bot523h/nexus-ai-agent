from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, Integer, UniqueConstraint
from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.types import TypeDecorator, TypeEngine
from sqlmodel import Field, SQLModel


class TelegramId(TypeDecorator[int]):
    """Column type for every Telegram identifier (user, chat, channel ids).

    Audit finding A-17 (2026-10-15, found on a live PostgreSQL run): Telegram
    user ids passed 2**31 in 2021 and supergroup/channel ids are 13-digit
    ``-100…`` numbers, yet every id column was a 32-bit ``INTEGER`` on
    PostgreSQL — the first message from a modern account or any supergroup
    failed with ``NumericValueOutOfRange``. SQLite never showed it: its INTEGER
    is 64-bit, and ``INTEGER PRIMARY KEY`` must stay the rowid alias there, so
    this type leaves SQLite exactly as it was and widens PostgreSQL to BIGINT
    (migration ``b5d8e2f4a917``).
    """

    impl = BigInteger
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:
        if dialect.name == "sqlite":
            return dialect.type_descriptor(Integer())
        return dialect.type_descriptor(BigInteger())


class User(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    telegram_id: int = Field(index=True, unique=True, sa_type=TelegramId)
    username: str = Field(default="")
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)
    is_allowed: bool = Field(default=True, index=True)


class Chat(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    chat_id: int = Field(index=True, unique=True, sa_type=TelegramId)
    thread_id: str = Field(index=True)
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)
    policy: str = Field(default="default")


class Message(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    chat_id: int = Field(foreign_key="chat.id", index=True)
    role: str = Field(index=True)  # "user"|"assistant"|"system"
    content: str
    correlation_id: str = Field(index=True)
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)


class Task(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    chat_id: int = Field(foreign_key="chat.id", index=True)
    status: str = Field(default="pending", index=True)
    plan_json: str = Field(default="{}")
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)
    completed_at: datetime | None = Field(default=None, index=True)


class ToolRun(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    task_id: int | None = Field(default=None, foreign_key="task.id", index=True)
    tool_name: str = Field(index=True)
    input_json: str = Field(default="{}")
    output_json: str = Field(default="{}")
    error: str | None = Field(default=None)
    duration_ms: int = Field(default=0)
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)


# ── v1.2.0 models ────────────────────────────────────────────────────


class WelcomeMessage(SQLModel, table=True):
    """Per-chat welcome message for new members."""

    id: int | None = Field(default=None, primary_key=True)
    chat_id: int = Field(index=True, unique=True, sa_type=TelegramId)
    text: str = Field(default="")
    created_at: datetime = Field(default_factory=datetime.utcnow)


class ChannelSchedule(SQLModel, table=True):
    """Scheduled posts for channels / groups."""

    id: int | None = Field(default=None, primary_key=True)
    chat_id: int = Field(index=True, sa_type=TelegramId)
    text: str
    scheduled_at: datetime = Field(index=True)
    status: str = Field(default="pending", index=True)  # pending | sent | cancelled
    created_at: datetime = Field(default_factory=datetime.utcnow)


class AnonSession(SQLModel, table=True):
    """Anonymous chat sessions between two users."""

    id: int | None = Field(default=None, primary_key=True)
    user1_id: int = Field(index=True, sa_type=TelegramId)
    user2_id: int = Field(index=True, sa_type=TelegramId)
    started_at: datetime = Field(default_factory=datetime.utcnow, index=True)
    status: str = Field(default="active", index=True)  # active | ended | reported


class QuizScore(SQLModel, table=True):
    """Quiz game score board."""

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(index=True, sa_type=TelegramId)
    chat_id: int = Field(index=True, sa_type=TelegramId)
    score: int = Field(default=0)
    answered: int = Field(default=0)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class Reminder(SQLModel, table=True):
    """User reminders with persistence across restarts."""

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(index=True, sa_type=TelegramId)
    chat_id: int = Field(index=True, sa_type=TelegramId)
    text: str
    remind_at: datetime = Field(index=True)
    status: str = Field(default="pending", index=True)  # pending | sent | cancelled
    created_at: datetime = Field(default_factory=datetime.utcnow)


# ── v1.3.0 models ──────────────────────────────────────────────────


class AdminLog(SQLModel, table=True):
    """Admin / owner action log."""

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(index=True, sa_type=TelegramId)
    action: str = Field(index=True)
    target: str = Field(default="")
    details: str = Field(default="")
    timestamp: datetime = Field(default_factory=datetime.utcnow, index=True)


class ForceJoinConfig(SQLModel, table=True):
    """Per-chat force-join configuration."""

    id: int | None = Field(default=None, primary_key=True)
    chat_id: int = Field(index=True, unique=True, sa_type=TelegramId)
    enabled: bool = Field(default=False)
    channel_username: str = Field(default="@nexus_ai_official")
    welcome_message: str = Field(default="⛔ لطفاً ابتدا در کانال عضو شوید.")
    created_at: datetime = Field(default_factory=datetime.utcnow)


class PersonalityConfig(SQLModel, table=True):
    """Per-group personality setting."""

    id: int | None = Field(default=None, primary_key=True)
    chat_id: int = Field(index=True, unique=True, sa_type=TelegramId)
    personality: str = Field(default="friendly")
    set_by: int = Field(default=0, sa_type=TelegramId)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class EngagementConfig(SQLModel, table=True):
    """Per-group engagement settings."""

    id: int | None = Field(default=None, primary_key=True)
    chat_id: int = Field(index=True, unique=True, sa_type=TelegramId)
    enabled: bool = Field(default=False)
    frequency_minutes: int = Field(default=60)
    last_engagement: datetime | None = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class ModerationConfig(SQLModel, table=True):
    """Per-group moderation settings."""

    id: int | None = Field(default=None, primary_key=True)
    chat_id: int = Field(index=True, unique=True, sa_type=TelegramId)
    anti_spam: bool = Field(default=True)
    anti_flood: bool = Field(default=True)
    link_filter: bool = Field(default=False)
    profanity_filter: bool = Field(default=False)
    max_warnings: int = Field(default=3)
    mute_duration_minutes: int = Field(default=30)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class UserReputation(SQLModel, table=True):
    """Per-user reputation in a chat."""

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(index=True, sa_type=TelegramId)
    chat_id: int = Field(index=True, sa_type=TelegramId)
    reputation: int = Field(default=0)
    warnings: int = Field(default=0)
    is_muted: bool = Field(default=False)
    mute_until: datetime | None = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class UserXP(SQLModel, table=True):
    """Per-user XP and levelling."""

    __table_args__ = {"extend_existing": True}

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(index=True, sa_type=TelegramId)
    chat_id: int = Field(index=True, sa_type=TelegramId)
    xp: int = Field(default=0)
    level: int = Field(default=1)
    streak: int = Field(default=0)
    last_daily: datetime | None = Field(default=None)
    achievements: str = Field(default="[]")  # JSON array
    referral_count: int = Field(default=0)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class AdCampaign(SQLModel, table=True):
    """Scheduled advertisement campaigns."""

    id: int | None = Field(default=None, primary_key=True)
    chat_id: int = Field(index=True, sa_type=TelegramId)
    text: str
    interval_hours: float = Field(default=6.0)
    status: str = Field(default="active", index=True)  # active | paused | expired
    repeat_count: int = Field(default=0)
    max_repeats: int = Field(default=0)  # 0 = unlimited
    next_run: datetime | None = Field(default=None)
    created_by: int = Field(default=0, sa_type=TelegramId)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class ViralPost(SQLModel, table=True):
    """Auto-generated viral content."""

    id: int | None = Field(default=None, primary_key=True)
    chat_id: int = Field(index=True, sa_type=TelegramId)
    text: str
    viral_score: float = Field(default=0.0)
    status: str = Field(default="pending", index=True)  # pending | posted | failed
    posted_at: datetime | None = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class AnalyticsEvent(SQLModel, table=True):
    """Generic analytics event."""

    id: int | None = Field(default=None, primary_key=True)
    chat_id: int = Field(index=True, sa_type=TelegramId)
    user_id: int = Field(default=0, sa_type=TelegramId)
    event_type: str = Field(index=True)
    event_data: str = Field(default="{}")  # JSON
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)


# ── v2.0.0 models ──────────────────────────────────────────────────────


class Referral(SQLModel, table=True):
    """Referral tracking: who referred whom.

    Single definition (audit A-09, 2026-10-15): ``features/referral.py`` used
    to declare a second, stricter copy of this table with ``extend_existing``,
    so the effective schema depended on import order. The indexes below are
    the ones migration ``47903d282ede`` creates.
    """

    id: int | None = Field(default=None, primary_key=True)
    referrer_id: int = Field(index=True, sa_type=TelegramId)
    # each user can only be referred once
    referee_id: int = Field(index=True, unique=True, sa_type=TelegramId)
    referral_code: str = Field(index=True)
    status: str = Field(default="pending", index=True)  # pending | completed | rewarded
    reward_claimed: bool = Field(default=False)
    xp_awarded: bool = Field(default=False)
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)
    completed_at: datetime | None = Field(default=None)


class ReferralCode(SQLModel, table=True):
    """Per-user unique referral code and stats (single definition, see Referral)."""

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(index=True, unique=True, sa_type=TelegramId)
    code: str = Field(index=True, unique=True)
    total_referrals: int = Field(default=0)
    successful_referrals: int = Field(default=0)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class UserLanguage(SQLModel, table=True):
    __table_args__ = {"extend_existing": True}
    """Per-user language preference for i18n."""

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(default=0, sa_type=TelegramId)
    language: str = Field(default="en")  # ISO 639-1 code
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class CloudFile(SQLModel, table=True):
    __table_args__ = {"extend_existing": True}
    """Tracks files uploaded to unified cloud storage."""

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(default=0, sa_type=TelegramId)
    file_name: str = Field(default="")
    provider: str = Field(default="")  # dropbox | pcloud | internxt | mega
    remote_path: str = Field(default="")
    file_size: int = Field(default=0)
    created_at: datetime = Field(default_factory=datetime.utcnow)


# ── v3.0.0 models ──────────────────────────────────────────────────────


class KnowledgeCache(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    query: str = Field(index=True)
    source: str
    content: str
    expires_at: datetime = Field(index=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class PendingApproval(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    change_type: str
    description: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    status: str = "pending"  # pending, approved, rejected
    auto_apply_at: datetime | None = None


class UserActiveAgent(SQLModel, table=True):
    user_id: int = Field(primary_key=True, sa_type=TelegramId)
    agent_name: str
    activated_at: datetime = Field(default_factory=datetime.utcnow)


class UserMemory(SQLModel, table=True):
    user_id: int = Field(primary_key=True, sa_type=TelegramId)
    name: str | None = None
    interests: str = "[]"  # JSON list
    occupation: str | None = None
    personality_tags: str = "[]"  # JSON list
    last_updated: datetime = Field(default_factory=datetime.utcnow)
    # ── P0-7: explicit consent for LLM egress (AIMemory) ──────────────
    # ``ai_memory_consent`` is a tri-state: None (unset / never asked or
    # answered) | "granted" | "denied".  No message text may leave the
    # process for the external LLM while it is not exactly "granted".
    ai_memory_consent: str | None = None
    ai_memory_consent_at: datetime | None = None
    # Whether the one-time consent question has already been shown, so the
    # bot never re-prompts a user who ignored the first question.
    ai_memory_prompted: bool | None = None


class DocumentChunk(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(index=True, sa_type=TelegramId)
    filename: str
    chunk_text: str
    chunk_index: int
    embedding: str  # JSON list of floats


# ── Nagar Asset Library V1 (task-266) ────────────────────────────────
# Durable metadata for the first-party Asset Library.  The bytes live in the
# content-addressed blob store; these tables are the authoritative record of
# *logical* asset identity, immutable content versions and project association.
# A logical asset is independent of its file version; each version carries the
# measured digest, so "the same asset" and "the same bytes" are distinct.


class Asset(SQLModel, table=True):
    """Stable logical identity of a media asset, independent of any version."""

    asset_id: str = Field(primary_key=True)
    media_kind: str = Field(index=True)  # "image" | "audio" | "video"
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)


class AssetVersion(SQLModel, table=True):
    """One immutable content version of an asset (its own measured identity)."""

    version_id: str = Field(primary_key=True)
    asset_id: str = Field(foreign_key="asset.asset_id", index=True)
    content_sha256: str = Field(index=True)
    size_bytes: int
    media_kind: str
    media_type: str
    blob_key: str
    original_filename: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)


class ProjectAssetLink(SQLModel, table=True):
    """Durable association of an asset version with an authorized project."""

    link_id: str = Field(primary_key=True)
    project_id: str = Field(index=True)
    asset_id: str = Field(foreign_key="asset.asset_id", index=True)
    version_id: str = Field(foreign_key="assetversion.version_id", index=True)
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)


class AssetIngest(SQLModel, table=True):
    """Identity and recoverable state of one ingest attempt (idempotency key)."""

    # The scoped idempotency key is authoritative for dedup: a database UNIQUE
    # constraint makes concurrent same-key ingests collapse to one row instead
    # of racing SELECT-then-INSERT into two independent assets.
    __table_args__ = (
        UniqueConstraint("project_id", "idempotency_key", name="uq_assetingest_project_idem"),
    )

    ingest_id: str = Field(primary_key=True)
    idempotency_key: str = Field(index=True)
    project_id: str = Field(index=True)
    request_fingerprint: str = Field(index=True)
    # STAGING -> READY, or FAILED when safe recovery is no longer possible.
    state: str = Field(default="STAGING", index=True)
    # Media classification is only known after the leading bytes are read, so
    # these are unknown (NULL) until the ingest has been staged.
    media_kind: str | None = Field(default=None)
    media_type: str | None = Field(default=None)
    original_filename: str = ""
    content_sha256: str | None = Field(default=None, index=True)
    size_bytes: int | None = Field(default=None)
    blob_key: str | None = Field(default=None)
    # The staging path is a trusted, server-derived relative key (never a
    # caller-supplied path); it is cleared once the ingest is READY/FAILED.
    staging_key: str | None = Field(default=None)
    asset_id: str | None = Field(default=None, index=True)
    version_id: str | None = Field(default=None, index=True)
    error_code: str | None = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)
    updated_at: datetime = Field(default_factory=datetime.utcnow, index=True)
