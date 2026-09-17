from inspect import signature

from nexus_ai_agent.application.ports.checkpoint_lifecycle import CheckpointLifecyclePort
from nexus_ai_agent.application.ports.conversation_store import ConversationStorePort
from nexus_ai_agent.application.ports.job_queue import JobQueuePort
from nexus_ai_agent.application.ports.llm import LLMPort
from nexus_ai_agent.application.ports.object_storage import ObjectStoragePort


def test_required_port_methods_are_present() -> None:
    assert all(
        hasattr(CheckpointLifecyclePort, name)
        for name in (
            "record_checkpoint",
            "touch_thread",
            "inspect",
            "delete_thread",
            "schema_fingerprint",
        )
    )
    assert hasattr(ConversationStorePort, "append_message")
    assert hasattr(JobQueuePort, "enqueue")
    assert hasattr(LLMPort, "complete")
    assert hasattr(ObjectStoragePort, "put")


def test_delete_requires_explicit_idempotency_key() -> None:
    params = signature(CheckpointLifecyclePort.delete_thread).parameters
    assert "idempotency_key" in params
    assert params["idempotency_key"].kind.name == "KEYWORD_ONLY"
