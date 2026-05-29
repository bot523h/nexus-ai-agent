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
