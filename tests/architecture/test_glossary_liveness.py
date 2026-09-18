from nexus_ai_agent.domain.glossary import (
    POST_V1_CHECKPOINT_DELETION,
    SOURCE_OF_TRUTH,
    THREAD_DELETE_UNIT,
    EntityType,
)
from nexus_ai_agent.domain.policies.retention import (
    ALLOWED_TRANSITIONS,
    FORK_AFTER_RESUMABILITY,
    RESUMABILITY_WINDOW,
    RETRY_BACKOFF,
    JournalStatus,
)


def test_glossary_contract_remains_live() -> None:
    assert SOURCE_OF_TRUTH[EntityType.MESSAGE].startswith("message")
    assert "workflow continuation" in SOURCE_OF_TRUTH[EntityType.CHECKPOINT]
    assert THREAD_DELETE_UNIT is EntityType.THREAD
    assert POST_V1_CHECKPOINT_DELETION == "POST_V1"


def test_retention_contract_remains_live() -> None:
    assert RESUMABILITY_WINDOW.days == 30
    assert FORK_AFTER_RESUMABILITY == "fork_new_thread_from_message_history"
    assert ALLOWED_TRANSITIONS
    assert ALLOWED_TRANSITIONS[JournalStatus.BLOCKED] == frozenset({JournalStatus.PENDING})
    assert JournalStatus.CANCELLED in ALLOWED_TRANSITIONS[JournalStatus.RUNNING]
    assert not ALLOWED_TRANSITIONS[JournalStatus.SUCCEEDED]
    assert not ALLOWED_TRANSITIONS[JournalStatus.CANCELLED]
    assert RETRY_BACKOFF == "exponential_backoff_on_failed_to_retrying"
