from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from sqlmodel import Field, SQLModel


def utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp for new control-plane records."""
    return datetime.now(timezone.utc)


class NodeStatus(str, Enum):
    PROVISIONING = "provisioning"
    ONLINE = "online"
    DEGRADED = "degraded"
    OFFLINE = "offline"
    DISABLED = "disabled"


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    RETRY_WAITING = "retry_waiting"
    CANCELLED = "cancelled"


class JobPriority(str, Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    CRITICAL = "critical"


class SessionStatus(str, Enum):
    ACTIVE = "active"
    IDLE = "idle"
    CLOSED = "closed"
    EXPIRED = "expired"


class LogLevel(str, Enum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class Node(SQLModel, table=True):
    """A NEXUS installation running on a managed VPS or local host."""

    __tablename__ = "control_node"

    id: int | None = Field(default=None, primary_key=True)
    node_key: str = Field(index=True, max_length=128)
    name: str = Field(max_length=200)
    endpoint: str | None = Field(default=None, max_length=500)
    status: NodeStatus = Field(default=NodeStatus.PROVISIONING, index=True)
    version: str | None = Field(default=None, max_length=64)
    last_heartbeat_at: datetime | None = Field(default=None, index=True)
    last_error: str | None = Field(default=None, max_length=2000)
    capabilities_json: str = Field(default="{}")
    created_at: datetime = Field(default_factory=utc_now, index=True)
    updated_at: datetime = Field(default_factory=utc_now)


class Job(SQLModel, table=True):
    """A durable unit of work that can be dispatched to a NEXUS node."""

    __tablename__ = "control_job"

    id: int | None = Field(default=None, primary_key=True)
    job_key: str = Field(index=True, max_length=128)
    node_id: int | None = Field(default=None, foreign_key="control_node.id", index=True)
    kind: str = Field(max_length=128, index=True)
    status: JobStatus = Field(default=JobStatus.QUEUED, index=True)
    priority: JobPriority = Field(default=JobPriority.NORMAL, index=True)
    payload_json: str = Field(default="{}")
    result_json: str | None = None
    error_message: str | None = Field(default=None, max_length=4000)
    attempts: int = Field(default=0, ge=0)
    max_attempts: int = Field(default=3, ge=1, le=100)
    available_at: datetime = Field(default_factory=utc_now, index=True)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now, index=True)
    updated_at: datetime = Field(default_factory=utc_now)


class AgentSession(SQLModel, table=True):
    """Provider-agnostic conversation context owned by one user and connector."""

    __tablename__ = "agent_session"

    id: int | None = Field(default=None, primary_key=True)
    session_key: str = Field(index=True, max_length=128)
    user_id: int = Field(index=True)
    connector: str = Field(default="telegram", max_length=64, index=True)
    external_chat_id: str | None = Field(default=None, max_length=128)
    agent_name: str = Field(default="default", max_length=128)
    status: SessionStatus = Field(default=SessionStatus.ACTIVE, index=True)
    context_json: str = Field(default="{}")
    message_count: int = Field(default=0, ge=0)
    last_activity_at: datetime = Field(default_factory=utc_now, index=True)
    created_at: datetime = Field(default_factory=utc_now, index=True)
    closed_at: datetime | None = None


class SystemLog(SQLModel, table=True):
    """Structured operational event used by monitoring and self-healing."""

    __tablename__ = "system_log"

    id: int | None = Field(default=None, primary_key=True)
    level: LogLevel = Field(default=LogLevel.INFO, index=True)
    event: str = Field(max_length=160, index=True)
    message: str = Field(max_length=4000)
    component: str = Field(default="core", max_length=128, index=True)
    node_id: int | None = Field(default=None, foreign_key="control_node.id", index=True)
    job_id: int | None = Field(default=None, foreign_key="control_job.id", index=True)
    correlation_id: str | None = Field(default=None, max_length=128, index=True)
    details_json: str = Field(default="{}")
    recoverable: bool = Field(default=False, index=True)
    created_at: datetime = Field(default_factory=utc_now, index=True)

    def with_details(self, details: dict[str, Any]) -> SystemLog:
        """Return this log with a JSON-ready details payload supplied by the caller."""
        import json

        self.details_json = json.dumps(details, ensure_ascii=False, default=str)
        return self
