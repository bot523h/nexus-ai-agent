from inspect import signature
from typing import get_type_hints

from nexus_ai_agent.application.ports.caption_engine import CaptionEnginePort
from nexus_ai_agent.application.ports.checkpoint_lifecycle import CheckpointLifecyclePort
from nexus_ai_agent.application.ports.conversation_store import ConversationStorePort
from nexus_ai_agent.application.ports.job_queue import JobQueuePort
from nexus_ai_agent.application.ports.llm import LLMPort
from nexus_ai_agent.application.ports.object_storage import ObjectStoragePort


def test_required_port_methods_are_present_and_typed() -> None:
    ports = {
        CheckpointLifecyclePort: (
            "record_checkpoint",
            "touch_thread",
            "inspect",
            "delete_thread",
            "schema_fingerprint",
        ),
        ConversationStorePort: ("append_message", "list_messages"),
        JobQueuePort: ("enqueue", "get_status", "get_result"),
        LLMPort: ("complete",),
        ObjectStoragePort: ("put", "delete"),
        CaptionEnginePort: ("transcribe", "is_available"),
    }
    for port, methods in ports.items():
        for name in methods:
            method = getattr(port, name)
            assert get_type_hints(method), f"untyped port method: {port.__name__}.{name}"


def test_delete_requires_explicit_idempotency_key() -> None:
    params = signature(CheckpointLifecyclePort.delete_thread).parameters
    assert params["idempotency_key"].kind.name == "KEYWORD_ONLY"
    assert get_type_hints(CheckpointLifecyclePort.delete_thread)["idempotency_key"] is str


def test_ports_do_not_import_adapters() -> None:
    for port in (
        CheckpointLifecyclePort,
        ConversationStorePort,
        JobQueuePort,
        LLMPort,
        ObjectStoragePort,
        CaptionEnginePort,
    ):
        assert "adapters" not in vars(__import__(port.__module__, fromlist=["* "]))
