"""Frozen Nexus vocabulary and source-of-truth contract.

Messages are the durable history. Checkpoints are disposable workflow
continuation state. A thread is the v1 unit of deletion; surgery on one
checkpoint is explicitly deferred because LangGraph stores delta lineage.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final, TypeAlias


class EntityType(StrEnum):
    CONVERSATION = "conversation"
    THREAD = "thread"
    MESSAGE = "message"
    CHECKPOINT = "checkpoint"
    TOOL_STATE = "tool_state"
    ATTACHMENT = "attachment"


SourceOfTruth: TypeAlias = str
SOURCE_OF_TRUTH: Final[dict[EntityType, SourceOfTruth]] = {
    EntityType.CONVERSATION: "application conversation store",
    EntityType.THREAD: "application conversation store",
    EntityType.MESSAGE: "message persistence; durable user-visible history",
    EntityType.CHECKPOINT: "LangGraph checkpointer; workflow continuation state only",
    EntityType.TOOL_STATE: "tool integration owning the state",
    EntityType.ATTACHMENT: "object storage plus attachment metadata",
}

THREAD_DELETE_UNIT: Final[EntityType] = EntityType.THREAD
POST_V1_CHECKPOINT_DELETION: Final[str] = "POST_V1"
