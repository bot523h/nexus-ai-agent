"""Canonical Nexus data vocabulary and source-of-truth map.

Conversation is the product-level aggregate. A thread is one resumable
workflow branch. Messages are the durable conversation history; checkpoints
are disposable workflow state and never the source of truth for user-visible
history. Attachments are object-storage references owned by the conversation.
"""

from __future__ import annotations

from enum import StrEnum


class Entity(StrEnum):
    CONVERSATION = "conversation"
    THREAD = "thread"
    MESSAGE = "message"
    CHECKPOINT = "checkpoint"
    TOOL_STATE = "tool_state"
    ATTACHMENT = "attachment"


SOURCE_OF_TRUTH: dict[Entity, str] = {
    Entity.CONVERSATION: "application conversation store",
    Entity.THREAD: "application conversation store",
    Entity.MESSAGE: "message persistence; the durable user-visible history",
    Entity.CHECKPOINT: "LangGraph checkpointer; resumable workflow state only",
    Entity.TOOL_STATE: "workflow state owned by its tool integration",
    Entity.ATTACHMENT: "object storage plus attachment metadata",
}

# Per-checkpoint deletion is intentionally not part of the v1 contract.
POST_V1_CHECKPOINT_SURGERY = True
