from __future__ import annotations

from datetime import datetime

from sqlmodel import Field, SQLModel


class User(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    telegram_id: int = Field(index=True, unique=True)
    username: str = Field(default="")
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)
    is_allowed: bool = Field(default=True, index=True)


class Chat(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    chat_id: int = Field(index=True, unique=True)
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
    chat_id: int = Field(index=True, unique=True)
    text: str = Field(default="")
    created_at: datetime = Field(default_factory=datetime.utcnow)


class ChannelSchedule(SQLModel, table=True):
    """Scheduled posts for channels / groups."""

    id: int | None = Field(default=None, primary_key=True)
    chat_id: int = Field(index=True)
    text: str
    scheduled_at: datetime = Field(index=True)
    status: str = Field(default="pending", index=True)  # pending | sent | cancelled
    created_at: datetime = Field(default_factory=datetime.utcnow)


class AnonSession(SQLModel, table=True):
    """Anonymous chat sessions between two users."""

    id: int | None = Field(default=None, primary_key=True)
    user1_id: int = Field(index=True)
    user2_id: int = Field(index=True)
    started_at: datetime = Field(default_factory=datetime.utcnow, index=True)
    status: str = Field(default="active", index=True)  # active | ended | reported


class QuizScore(SQLModel, table=True):
    """Quiz game score board."""

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(index=True)
    chat_id: int = Field(index=True)
    score: int = Field(default=0)
    answered: int = Field(default=0)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class Reminder(SQLModel, table=True):
    """User reminders with persistence across restarts."""

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(index=True)
    chat_id: int = Field(index=True)
    text: str
    remind_at: datetime = Field(index=True)
    status: str = Field(default="pending", index=True)  # pending | sent | cancelled
    created_at: datetime = Field(default_factory=datetime.utcnow)


# ── v1.3.0 models ──────────────────────────────────────────────────


class AdminLog(SQLModel, table=True):
    """Admin / owner action log."""

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(index=True)
    action: str = Field(index=True)
    target: str = Field(default="")
    details: str = Field(default="")
    timestamp: datetime = Field(default_factory=datetime.utcnow, index=True)


class ForceJoinConfig(SQLModel, table=True):
    """Per-chat force-join configuration."""

    id: int | None = Field(default=None, primary_key=True)
    chat_id: int = Field(index=True, unique=True)
    enabled: bool = Field(default=False)
    channel_username: str = Field(default="@nexus_ai_official")
    welcome_message: str = Field(default="⛔ لطفاً ابتدا در کانال عضو شوید.")
    created_at: datetime = Field(default_factory=datetime.utcnow)


class PersonalityConfig(SQLModel, table=True):
    """Per-group personality setting."""

    id: int | None = Field(default=None, primary_key=True)
    chat_id: int = Field(index=True, unique=True)
    personality: str = Field(default="friendly")
    set_by: int = Field(default=0)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class EngagementConfig(SQLModel, table=True):
    """Per-group engagement settings."""

    id: int | None = Field(default=None, primary_key=True)
    chat_id: int = Field(index=True, unique=True)
    enabled: bool = Field(default=False)
    frequency_minutes: int = Field(default=60)
    last_engagement: datetime | None = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class ModerationConfig(SQLModel, table=True):
    """Per-group moderation settings."""

    id: int | None = Field(default=None, primary_key=True)
    chat_id: int = Field(index=True, unique=True)
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
    user_id: int = Field(index=True)
    chat_id: int = Field(index=True)
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
    user_id: int = Field(index=True)
    chat_id: int = Field(index=True)
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
    chat_id: int = Field(index=True)
    text: str
    interval_hours: float = Field(default=6.0)
    status: str = Field(default="active", index=True)  # active | paused | expired
    repeat_count: int = Field(default=0)
    max_repeats: int = Field(default=0)  # 0 = unlimited
    next_run: datetime | None = Field(default=None)
    created_by: int = Field(default=0)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class ViralPost(SQLModel, table=True):
    """Auto-generated viral content."""

    id: int | None = Field(default=None, primary_key=True)
    chat_id: int = Field(index=True)
    text: str
    viral_score: float = Field(default=0.0)
    status: str = Field(default="pending", index=True)  # pending | posted | failed
    posted_at: datetime | None = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class AnalyticsEvent(SQLModel, table=True):
    """Generic analytics event."""

    id: int | None = Field(default=None, primary_key=True)
    chat_id: int = Field(index=True)
    user_id: int = Field(default=0)
    event_type: str = Field(index=True)
    event_data: str = Field(default="{}")  # JSON
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)


# ── v2.0.0 models ──────────────────────────────────────────────────────


class Referral(SQLModel, table=True):
    __table_args__ = {"extend_existing": True}
    """Referral tracking: who referred whom."""

    id: int | None = Field(default=None, primary_key=True)
    referrer_id: int = Field(index=True)
    referee_id: int = Field(index=True)
    referral_code: str = Field(index=True)
    status: str = Field(default="pending", index=True)  # pending | completed | rewarded
    reward_claimed: bool = Field(default=False)
    xp_awarded: bool = Field(default=False)
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)
    completed_at: datetime | None = Field(default=None)


class ReferralCode(SQLModel, table=True):
    __table_args__ = {"extend_existing": True}
    """Per-user unique referral code and stats."""

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(index=True, unique=True)
    code: str = Field(index=True, unique=True)
    total_referrals: int = Field(default=0)
    successful_referrals: int = Field(default=0)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class UserLanguage(SQLModel, table=True):
    __table_args__ = {"extend_existing": True}
    """Per-user language preference for i18n."""

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(index=True, unique=True)
    language: str = Field(default="en", index=True)  # ISO 639-1 code
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class CloudFile(SQLModel, table=True):
    __table_args__ = {"extend_existing": True}
    """Tracks files uploaded to unified cloud storage."""

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(index=True)
    file_name: str = Field(index=True)
    provider: str = Field(index=True)  # dropbox | pcloud | internxt | mega
    remote_path: str = Field(default="")
    file_size: int = Field(default=0)
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)

# ── v3.0.0 models ──────────────────────────────────────────────────────


class KnowledgeCache(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    query: str = Field(index=True)
    source: str
    content: str
    expires_at: datetime = Field(index=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)
