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
from typing import Any

import pytest

from nexus_ai_agent.bot.memory_handlers import AIMEMORY_CONSENT_PROMPT
from nexus_ai_agent.features.ai_memory import (
    SKIP_DISABLED,
    SKIP_NOT_CONSENTED,
    AIMemoryEngine,
)
from nexus_ai_agent.llm.fake_llm import FakeLLMProvider
from nexus_ai_agent.memory.long_term import LongTermMemory, memory_scope_id
from nexus_ai_agent.orchestration.graph import _memory_reader, _memory_writer, compile_graph
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

    async def generate(self, prompt: str, system: str = "", **kwargs: Any) -> str:
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

        async def generate(self, prompt: str, system: str = "", **kwargs: Any) -> str:
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

    async def generate(self, prompt: str, system: str = "", **kwargs: Any) -> str:
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
        # Private audience (chat_id == user_id): the reader only discloses
        # personal memory in a provable 1:1 chat (see failclosed suite).
        "chat_id": 77,
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


# ── P1-D final mission: concept 5 (audience) + group privacy ≠ consent ──


def test_consent_prompt_scopes_memory_use_to_private_audience() -> None:
    """The copy must keep the audience concept distinct and accurate.

    Five concepts stay separate: (1) explicit AI commands, (2) memory
    extraction, (3) memory storage, (4) retrieval/use in answers, (5) the
    audience — personal memory feeds answers in PRIVATE chats only and is
    never part of group-visible responses.
    """
    text = AIMEMORY_CONSENT_PROMPT
    assert "خصوصی" in text, (
        f"the copy must state the audience scope of memory use (private chats): {text!r}"
    )
    assert "گروه" in text, (
        f"the copy must state that group-visible responses never carry personal memory: {text!r}"
    )


async def test_group_privacy_is_independent_of_personal_consent(mem_db) -> None:
    """Granting memory consent must NOT unlock group disclosure.

    Group privacy (audience gate) and personal consent (egress gate) are
    distinct controls: consent granted + memory stored still yields zero
    personal context in a group audience.
    """
    from nexus_ai_agent.features.ai_memory import AIMemoryEngine

    llm = _EgressTripwire()
    long_term = LongTermMemory(":memory:", llm)
    scope = memory_scope_id(5)
    assert scope is not None

    gem = _RecordingGemini()
    engine = AIMemoryEngine(gemini_provider=gem, min_egress_seconds=0)  # type: ignore[arg-type]
    await engine.set_consent(5, True)

    # Personal memory exists for user 5 …
    await long_term.store(scope, "User: my secret code is PERSONAL-77\nAssistant: ok")

    # … and a group turn by user 5 must see none of it, consent notwithstanding.
    state = {
        "thread_id": "tg:-100999",
        "chat_id": -100999,
        "user_id": 5,
        "correlation_id": "c",
        "messages": [{"role": "user", "content": "what is my secret code?"}],
        "intent": "memory",
        "active_persona": "gemma",
        "current_task": None,
        "tool_results": [],
        "memory_context": "PERSONAL-77",
        "response": "",
        "error": None,
        "turn_count": 0,
        "moderation_passed": True,
    }
    await _memory_reader(long_term, state)  # type: ignore[arg-type]
    assert "PERSONAL-77" not in (state.get("memory_context") or ""), (
        "group disclosure happened despite the audience gate — consent and "
        "audience are distinct controls and consent must not unlock groups"
    )


# ── P1-E — the graph writer must honour the effective consent decision ──
#
# The P0-7 gate (features/ai_memory.py) covers the extraction egress, but the
# LangGraph ``_memory_writer`` stores every turn into personal long-term memory
# with no consent check at all.  The effective decision must travel from the
# message handler through ``NexusState.memory_consent``; anything but an
# explicit ``True`` — absent key, denied vote, malformed value — is denied, and
# the live feature switch is consulted too (a stale granted value cannot write
# while NEXUS_AI_MEMORY_ENABLED is false).  Every test spies at the real
# ``LongTermMemory.store`` boundary, not at the state flag.


class _StoreSpy(LongTermMemory):
    """Real store + call recording at the ``LongTermMemory.store`` boundary."""

    def __init__(self, llm: FakeLLMProvider) -> None:
        super().__init__(":memory:", llm)
        self.store_calls: list[tuple[str, str]] = []

    async def store(self, thread_id: str, text: str, metadata: dict | None = None) -> None:
        self.store_calls.append((thread_id, text))
        await super().store(thread_id, text, metadata)


def _writer_state(user_id: int, memory_consent: bool | None = None) -> dict[str, Any]:
    state: dict[str, Any] = {
        "thread_id": f"tg:{user_id}",
        "chat_id": user_id,
        "user_id": user_id,
        "correlation_id": "p1e",
        "messages": [{"role": "user", "content": "my secret code is WRITE-42"}],
        "intent": "chat",
        "active_persona": "gemma",
        "current_task": None,
        "tool_results": [],
        "memory_context": "",
        "response": "acknowledged",
        "error": None,
        "turn_count": 1,
        "moderation_passed": True,
    }
    if memory_consent is not None:
        state["memory_consent"] = memory_consent
    return state


async def test_writer_missing_consent_yields_zero_writes(mem_db) -> None:
    """Absent consent is denied: the writer must not reach the store."""
    mem = _StoreSpy(FakeLLMProvider())
    await _memory_writer(_writer_state(101), long_term_memory=mem)
    assert mem.store_calls == [], (
        "the writer stored a turn although no consent decision was carried in state"
    )


async def test_writer_denied_consent_yields_zero_writes(mem_db) -> None:
    """An explicit deny keeps the user's turns out of long-term memory."""
    mem = _StoreSpy(FakeLLMProvider())
    await _memory_writer(_writer_state(102, memory_consent=False), long_term_memory=mem)
    assert mem.store_calls == [], "a denied user's turn was stored in long-term memory"


async def test_writer_malformed_consent_yields_zero_writes(mem_db) -> None:
    """Unproven shapes (e.g. the string ``"granted"``) never count as proof."""
    state = _writer_state(103)
    state["memory_consent"] = "granted"
    mem = _StoreSpy(FakeLLMProvider())
    await _memory_writer(state, long_term_memory=mem)
    assert mem.store_calls == [], "a malformed consent value was treated as proof"


async def test_writer_granted_consent_with_memory_enabled_stores_the_turn(mem_db) -> None:
    """Positive control: grant + enabled feature writes, in the personal scope."""
    mem = _StoreSpy(FakeLLMProvider())
    state = _writer_state(104, memory_consent=True)
    await _memory_writer(state, long_term_memory=mem)
    assert len(mem.store_calls) == 1, "a consented turn was not stored"
    scope, text = mem.store_calls[0]
    assert scope == memory_scope_id(104), f"write must keep the personal scope: {scope!r}"
    assert "WRITE-42" in text and "acknowledged" in text
    hits = await mem.search("mem:u:104", "WRITE-42", top_k=3)
    assert any("WRITE-42" in h for h in hits), "the stored turn is not readable (round-trip)"


async def test_writer_feature_disabled_blocks_even_a_granted_consent_value(
    mem_db, monkeypatch
) -> None:
    """A stale/checkpointed granted value cannot beat the live kill switch."""
    from nexus_ai_agent.config import settings as settings_module

    monkeypatch.setenv("NEXUS_AI_MEMORY_ENABLED", "false")
    settings_module.get_settings.cache_clear()
    mem = _StoreSpy(FakeLLMProvider())
    await _memory_writer(_writer_state(105, memory_consent=True), long_term_memory=mem)
    assert mem.store_calls == [], "the writer stored while the memory feature was disabled"


async def test_unset_or_denied_consent_still_answers_via_the_full_graph(mem_db) -> None:
    """Policy A: the ordinary AI reply is independent of memory consent.

    End-to-end through the compiled graph (router → persona → moderation →
    writer): both an unset and a denied user get a reply, and the writer
    performs zero long-term-memory writes in both cases.
    """
    for label, consent in (("unset", None), ("denied", False)):
        llm = FakeLLMProvider()
        mem = _StoreSpy(llm)
        graph = compile_graph(
            llm,
            get_checkpointer(":memory:"),
            mem,  # type: ignore[arg-type]
            ToolRegistry(enable_shell=False, workspace_root="."),
        )
        state = _writer_state(300)
        state["thread_id"] = f"tg:300-{label}"
        state["response"] = ""
        if consent is None:
            state.pop("memory_consent", None)
        else:
            state["memory_consent"] = consent
        result = await graph.ainvoke(
            state, config={"configurable": {"thread_id": state["thread_id"]}}
        )
        assert result.get("response"), f"{label}: the AI reply must not depend on consent"
        assert mem.store_calls == [], f"{label}: the graph still wrote long-term memory"
