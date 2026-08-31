from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class ChatRole(str, Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class ToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    arguments: dict[str, object] = Field(default_factory=dict)


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: ChatRole
    content: str = ""
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)


class ToolDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$")
    description: str = Field(min_length=1, max_length=2000)
    parameters: dict[str, object] = Field(default_factory=dict)


class GenerateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    messages: list[ChatMessage] = Field(min_length=1)
    tools: list[ToolDefinition] = Field(default_factory=list)
    max_tokens: int = Field(default=512, ge=1, le=32768)
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    context_window: int = Field(default=4096, ge=256, le=131072)


class GenerateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    tool_calls: list[ToolCall] = Field(default_factory=list)
    backend: str
    finish_reason: str = "stop"
