"""Frozen Nexus vocabulary and source-of-truth contract.

Messages are the durable history. Checkpoints are disposable workflow
continuation state. A thread is the v1 unit of deletion; surgery on one
checkpoint is explicitly deferred because LangGraph stores delta lineage.
"""

from __future__ import annotations

from enum import Enum
from typing import Final, TypeAlias


class EntityType(str, Enum):
    """String-valued vocabulary enum, spelled without 3.11+ syntax.

    The ``(str, Enum)`` mixin plus ``__str__``/``__format__`` is exactly the
    pre-3.11 recipe for a value-printing string enum: ``str()``/``format()``
    yield the value on every supported interpreter (3.10–3.12+), identical
    to the 3.11 spelling this replaces.
    """

    CONVERSATION = "conversation"
    THREAD = "thread"
    MESSAGE = "message"
    CHECKPOINT = "checkpoint"
    TOOL_STATE = "tool_state"
    ATTACHMENT = "attachment"

    def __str__(self) -> str:
        return str(self.value)

    def __format__(self, spec: str) -> str:
        return format(str(self.value), spec)


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
