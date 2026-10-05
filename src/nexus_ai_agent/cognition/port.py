"""Provider-neutral cognition port (application-facing contract)."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol


class PrivacyClass(str, Enum):
    STANDARD = "standard"
    STRICT_LOCAL = "strict_local"
    REDACTED = "redacted"


class TaskClass(str, Enum):
    CHAT = "chat"
    CODE = "code"
    TRANSLATE = "translate"
    SUMMARIZE = "summarize"
    VISION = "vision"
    PLAN = "plan"
    EMBED = "embed"


@dataclass(frozen=True, slots=True)
class CognitionRequest:
    prompt: str
    task_class: TaskClass = TaskClass.CHAT
    system: str = ""
    privacy: PrivacyClass = PrivacyClass.STANDARD
    correlation_id: str = ""
    user_id: int | None = None
    timeout_s: float | None = None
    cancel_requested: bool = False
    memory_fragments: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CognitionResult:
    """Bounded textual cognition result — not an execution warrant."""

    text: str
    correlation_id: str
    provider_id: str
    route_reason: str
    usage: dict[str, Any] = field(default_factory=dict)
    degraded: bool = False


class CognitionError(Exception):
    def __init__(self, code: str, message: str, *, correlation_id: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.correlation_id = correlation_id


class CognitionPort(Protocol):
    async def propose(self, request: CognitionRequest) -> CognitionResult: ...
