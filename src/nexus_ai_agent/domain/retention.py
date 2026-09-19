"""Product retention decisions, independent of database implementations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta


@dataclass(frozen=True)
class RetentionDecision:
    """The product contract for resumability and history retention."""

    resumability_window: timedelta
    message_history_retention: timedelta | None
    checkpoint_delete_unit: str = "thread"
    expired_resume_behavior: str = "fork_new_thread_from_message_history"


# Product decision record: checkpoints are workflow state, not conversation history.
DEFAULT_RETENTION_DECISION = RetentionDecision(
    resumability_window=timedelta(days=30),
    message_history_retention=None,
)
