"""P1-D — consent copy states its true scope; memory egress has no bypass path.

The documented policy (docs/DECISION_LOG.md, 2026-09-21 P0-7) is Policy A:
the consent gate covers the AI-memory feature (fact extraction/storage) only.
Explicit AI commands and chat answers are a separate product function that
egresses by design.  The user-facing copy must therefore match the code:

* the prompt explains what the memory consent gates (extraction/storage) and
  what it does not (explicit AI commands);
* no absolute "nothing is ever sent to the cloud" promise remains in any
  consent text — that claim is false for a cloud-chat product;
* the deny reply says what switching off (memory extraction), not that all
  cloud processing stops;
* behaviour stays exactly as the P0-7 gate implements it: unset and denied
  never egress memory operations, granted+enabled may, disabled never;
* an explicitly requested AI reply is NOT blocked by memory-consent state;
* the graph long-term-memory path cannot bypass AIMemoryEngine to create an
  ungated egress: store/search touch only the (local) embedding seam, never
  the generation seam.
"""

from __future__ import annotations

import uuid

import pytest

from nexus_ai_agent.bot.memory_handlers import AIMEMORY_CONSENT_PROMPT
from nexus_ai_agent.features.ai_memory import (
    SKIP_DISABLED,
    SKIP_NOT_CONSENTED,
    AIMemoryEngine,
)
from nexus_ai_agent.llm.fake_llm import FakeLLMProvider
from nexus_ai_agent.memory.long_term import LongTermMemory, memory_scope_id
from nexus_ai_agent.orchestration.graph import _memory_reader, compile_graph
from nexus_ai_agent.storage.langgraph_checkpoint import get_checkpointer
from nexus_ai_agent.tools.registry import ToolRegistry

# ── copy contract ─────────────────────────────────────────────────────


def test_consent_prompt_makes_no_absolute_no_egress_promise() -> None:
    forbidden = [
        "هیچ اطلاعاتی به بیرون ارسال نمی‌شود",  # false: explicit AI commands egress
        "هرگز به مدل هوش مصنوعی",  # false absolute (old deny copy)
    ]
    for phrase in forbidden:
        assert phrase not in AIMEMORY_CONSENT_PROMPT, (
            f"consent prompt contains a false absolute promise: {phrase!r}"
        )


def test_consent_prompt_scopes_the_promise_to_memory_extraction() -> None:
    text = AIMEMORY_CONSENT_PROMPT
    assert "حافظه" in text, "the prompt must name the memory feature it gates"
    assert "استخراج" in text, "the prompt must say the gated processing is extraction into memory"
    # The promise must be scoped ("for memory extraction"), not absolute.
    scoped = "برای استخراج حافظه" in text or "برای حافظه" in text
    assert scoped, f"the no-egress promise must be scoped to memory extraction: {text!r}"


def test_consent_prompt_discloses_explicit_ai_commands_still_egress() -> None:
    assert "/ai" in AIMEMORY_CONSENT_PROMPT, (
        "the prompt must disclose that explicit AI commands still send messages "
        "to the cloud model — silence here re-creates the contradiction"
    )


def test_deny_reply_states_what_is_switched_off_without_lying() -> None:
    import inspect

    import nexus_ai_agent.bot.memory_handlers as mh

    src = inspect.getsource(mh.build_memory_handlers)
    assert "درخواست رد شد" in src, "deny reply missing"
    assert "هرگز" not in src, (
        "the deny reply must not promise 'never' for all cloud processing — "
        "explicit AI commands keep egressing by product design (Policy A)"
    )


# ── behaviour: the P0-7 gate matrix (memory operations only) ──────────


class _RecordingGemini:
    def __init__(self, reply: str = '{"name": "Sara"}') -> None:
        self.reply = reply
        self.calls: list[str] = []

    async def generate(self, prompt: str, system: str = "") -> str:
        self.calls.append(prompt)
        return self.reply


@pytest.fixture()
def mem_db(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    from sqlmodel import SQLModel

    from nexus_ai_agent.config import settings as settings_module
    from nexus_ai_agent.storage import db as db_module

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    eng = create_engine(f"sqlite:///{data_dir / 'app.sqlite'}")
    SQLModel.metadata.create_all(eng)
    eng.dispose()
    db_module._engine = None  # type: ignore[misc]
    db_module._engine_path = None  # type: ignore[misc]
    db_module._session_factory = None  # type: ignore[misc]
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("NEXUS_DATABASE_URL", raising=False)
    monkeypatch.setenv("NEXUS_AI_MEMORY_ENABLED", "true")
    settings_module.get_settings.cache_clear()
    yield
    settings_module.get_settings.cache_clear()


async def test_unset_and_denied_never_egress_memory_operations(mem_db) -> None:
    gem = _RecordingGemini()
    engine = AIMemoryEngine(gemini_provider=gem, min_egress_seconds=0)  # type: ignore[arg-type]
    assert await engine.update_from_message(1, "hi") == SKIP_NOT_CONSENTED
    await engine.set_consent(2, False)
    assert await engine.update_from_message(2, "hi") == SKIP_NOT_CONSENTED
    assert gem.calls == [], "unset/denied users must never egress memory operations"


async def test_granted_and_enabled_allows_memory_extraction(mem_db) -> None:
    gem = _RecordingGemini()
    engine = AIMemoryEngine(gemini_provider=gem, min_egress_seconds=0)  # type: ignore[arg-type]
    await engine.set_consent(3, True)
    outcome = await engine.update_from_message(3, "my name is Sara")
    assert outcome == "egressed"
    assert len(gem.calls) == 1


async def test_disabled_feature_never_egresses(mem_db, monkeypatch) -> None:
    from nexus_ai_agent.config import settings as settings_module

    gem = _RecordingGemini()
    engine = AIMemoryEngine(gemini_provider=gem, min_egress_seconds=0)  # type: ignore[arg-type]
    await engine.set_consent(4, True)
    monkeypatch.setenv("NEXUS_AI_MEMORY_ENABLED", "false")
    settings_module.get_settings.cache_clear()
    assert await engine.update_from_message(4, "hi") == SKIP_DISABLED
    assert gem.calls == []


# ── behaviour: explicit AI use is independent of memory consent ───────


async def test_explicit_ai_reply_is_not_blocked_by_memory_consent(
    mem_db,
) -> None:
    """Policy A: a denied user still gets cloud answers to explicit requests."""
    gem = _RecordingGemini()
    mem_engine = AIMemoryEngine(gemini_provider=gem, min_egress_seconds=0)  # type: ignore[arg-type]
    await mem_engine.set_consent(5, False)

    class _CaptureLLM(FakeLLMProvider):
        def __init__(self) -> None:
            self.prompts: list[str] = []

        async def generate(self, prompt: str, system: str = "") -> str:
            self.prompts.append(prompt)
            return "answer"

    llm = _CaptureLLM()
    graph = compile_graph(
        llm,
        get_checkpointer(":memory:"),
        LongTermMemory(":memory:", llm),
        ToolRegistry(enable_shell=False, workspace_root="."),
    )
    state = {
        "thread_id": f"tg:{uuid.uuid4().hex[:8]}",
        "chat_id": -1,
        "user_id": 5,
        "correlation_id": "c",
        "messages": [{"role": "user", "content": "hello"}],
        "intent": "chat",
        "active_persona": "gemma",
        "current_task": None,
        "tool_results": [],
        "memory_context": "",
        "response": "",
        "error": None,
        "turn_count": 0,
        "moderation_passed": True,
    }
    result = await graph.ainvoke(state, config={"configurable": {"thread_id": state["thread_id"]}})
    assert result.get("response"), "an explicit AI request must still be answered"
    assert gem.calls == [], "answering a chat turn must NOT trigger the AIMemory extraction egress"


# ── behaviour: the graph memory path has no ungated egress ────────────


class _EgressTripwire(FakeLLMProvider):
    """Embedding seam allowed; the generation seam is an egress tripwire."""

    def __init__(self) -> None:
        self.embed_calls: list[str] = []
        self.generate_calls: list[str] = []

    async def embed(self, text: str) -> list[float]:
        self.embed_calls.append(text)
        return await super().embed(text)

    async def generate(self, prompt: str, system: str = "") -> str:
        self.generate_calls.append(prompt)
        raise AssertionError(
            "memory store/search must never call generate() — that would be an "
            "ungated memory egress bypassing the AIMemoryEngine consent gate"
        )


async def test_graph_memory_write_and_read_do_not_egress(mem_db) -> None:
    llm = _EgressTripwire()
    long_term = LongTermMemory(":memory:", llm)
    scope = memory_scope_id(77)
    assert scope is not None

    # Write + personal read through the real graph nodes.
    state = {
        "thread_id": f"tg:{uuid.uuid4().hex[:8]}",
        "chat_id": -1,
        "user_id": 77,
        "correlation_id": "c",
        "messages": [{"role": "user", "content": "remember this secret"}],
        "intent": "memory",
        "active_persona": "gemma",
        "current_task": None,
        "tool_results": [],
        "memory_context": "",
        "response": "",
        "error": None,
        "turn_count": 0,
        "moderation_passed": True,
    }
    # The persona node calls generate() — that is the explicit AI answer seam.
    # So use the memory layer directly for the store/search proof, and keep the
    # graph invocation with a moderate persona below for the reader proof.
    await long_term.store(scope, "User: remember this secret\nAssistant: ok")
    hits = await long_term.search(scope, "secret", top_k=3)
    assert hits, "personal round-trip must work"
    assert llm.generate_calls == [], (
        f"store/search reached the generation seam: {llm.generate_calls!r}"
    )
    assert llm.embed_calls, "store/search must use the (local) embedding seam"

    # The memory READER node alone must not egress either.
    state2 = dict(state)
    state2["messages"] = [{"role": "user", "content": "what did I say?"}]
    await _memory_reader(long_term, state2)  # type: ignore[arg-type]
    assert "secret" in (state2.get("memory_context") or "")
    assert llm.generate_calls == [], "the memory reader must not egress"
