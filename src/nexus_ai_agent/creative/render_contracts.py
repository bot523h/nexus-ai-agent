"""Pure payload contracts shared by Agent orchestration and the render worker."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from nexus_ai_agent.creative.spine.models import AgentLineageRef

CREATIVE_RENDER_JOB_TYPE = "creative_render"


class CreativeRenderPayload(BaseModel):
    """Trust-boundary schema for the durable surface→queue→worker contract."""

    model_config = ConfigDict(extra="forbid")

    command: Literal["edit", "caption", "grade"]
    operation: str = Field(min_length=1, max_length=128)
    args: list[str] = Field(default_factory=list, max_length=16)
    workspace_dir: str = Field(min_length=1, max_length=4096)
    input_path: str | None = Field(default=None, max_length=4096)
    media_duration_us: int | None = Field(default=None, ge=0)
    user_id: int = Field(ge=0)
    chat_id: int
    lang: str = Field(default="en", min_length=2, max_length=16)
    idempotency_key: str = Field(min_length=1, max_length=256)
    agent_lineage: AgentLineageRef | None = None
    # Deliberately no lifecycle opt-in field: experimental-pack opt-in is
    # server policy; extra="forbid" rejects any caller-supplied override.

    @field_validator("args")
    @classmethod
    def _bound_args(cls, args: list[str]) -> list[str]:
        if any(len(argument) > 128 for argument in args):
            raise ValueError("render arguments are limited to 128 characters each")
        return args

    @model_validator(mode="after")
    def _agent_lineage_is_trim_only(self) -> CreativeRenderPayload:
        if self.agent_lineage is not None and (self.command, self.operation) != ("edit", "trim"):
            raise ValueError("Agent lineage is only valid for the edit trim operation")
        return self


__all__ = ["CREATIVE_RENDER_JOB_TYPE", "CreativeRenderPayload"]
