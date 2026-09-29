"""End-to-end Telegram proof (task-214).

WHAT THIS FILE ADDS TO task-205
-------------------------------
``tests/unit/test_chat_memory_reachability.py`` drives the production state
constructor ``bot/handlers.py::_base_state`` and then invokes the compiled
graph itself.  That leaves one span untested: the real Telegram entry point
``on_message`` and everything it does *around* the graph —

    presence → auth → rate_limiter → force_join gate → document-chat route
    → consent prompt → user/chat upsert → AgentManager lookup
    → _base_state → graph.ainvoke → _reply

A regression anywhere in that span — a gate that returns early, a branch that
bypasses the graph, a ``thread_id`` that stops matching the chat — is invisible
to task-205, which starts one layer below it.

WHAT IS REAL HERE
-----------------
The graph is compiled for real, the store is a real sqlite store, and the LLM
records the prompt it was handed, because the system prompt is the only place
from the outside where "the model saw it" can be observed.

The *database* is stubbed.  ``_upsert_user``/``_upsert_chat`` are bookkeeping,
not the memory path, and this file is about the memory path; the stub records
the ``thread_id`` it was handed, which is a more direct observation of the
derivation than reading it back out of a table would be.

``bot/handlers.py`` is NOT edited.  Seven open PRs contest it.

THE TRAP THIS FILE PINS
-----------------------
``handlers.py`` sets ``state["intent"] = "unknown"`` in *two* places — inside
``_base_state`` and again just before ``graph.ainvoke``.  It is harmless today
only because ``_router_node`` recomputes the intent with ``classify_intent``
before ``route_intent`` ever reads it.  That is an ordering accident, not a
guarantee, and nothing in the suite stated it.  ``test_intent_unknown_is_
overwritten_by_the_router`` states it.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from nexus_ai_agent.bot import handlers as bot_handlers
from nexus_ai_agent.config.settings import Settings
from tests.unit.test_chat_memory_reachability import (
    AuditedMemory,
    RecordingLLM,
    _build,
)

pytestmark = pytest.mark.anyio

USER_A = 111
USER_B = 222
CHAT_A = -1001
CHAT_B = -1002
FACT = "Falcon"


# ── doubles for everything that is not the memory path ────────────────────── #
class RecordingFactory:
    """Stands in for the SQLAlchemy session factory.

    ``_upsert_user``/``_upsert_chat`` are bookkeeping, not the memory path, and
    this file is about the memory path.  The session context is never entered:
    both upserts are spied out below, and the spy records the ``thread_id`` the
    handler derived — a more direct observation of the derivation than reading
    it back out of a table would be.
    """

    def __init__(self) -> None:
        self.chats: list[tuple[int, str]] = []

    def __call__(self) -> Any:  # pragma: no cover - never entered
        raise AssertionError("the DB layer must be spied, not reached")


class FakeUpdate:
    """The minimum surface ``on_message`` actually touches on an Update."""

    def __init__(self, text: str, *, chat_id: int, user_id: int) -> None:
        self.replies: list[str] = []
        self.effective_chat = SimpleNamespace(id=chat_id)
        self.effective_user = SimpleNamespace(id=user_id, username=f"u{user_id}")
        self.message = SimpleNamespace(text=text, chat=self.effective_chat)
        self.message.reply_text = self._reply_text
        self.edited_message = None

    async def _reply_text(self, text: str, **_kw: Any) -> None:
        self.replies.append(text)


# ── fixture: the REAL on_message, wired to the REAL graph ─────────────────── #
@pytest.fixture
def telegram(monkeypatch: pytest.MonkeyPatch):
    import dataclasses

    from nexus_ai_agent.bot.feature_handlers import build_feature_engines

    llm = RecordingLLM()
    memory = AuditedMemory(":memory:", llm)
    graph = _build(memory, llm)
    factory = RecordingFactory()

    settings = Settings().model_copy(
        update={"allowed_user_ids": [USER_A, USER_B], "owner_telegram_id": 0}
    )
    # The two DB-backed engines are replaced, not the memory path: `force_join`
    # would otherwise query a schema-less sqlite, and neither one participates in
    # recall.  Everything between them and the graph stays the real thing.
    real = build_feature_engines(settings)
    engines = dataclasses.replace(real, force_join=_StubForceJoin(), ai_memory=_StubMemoryEngine())

    # Seams on_message reaches for that are not the memory path.
    monkeypatch.setattr(bot_handlers, "route_doc_text", _never)
    monkeypatch.setattr(bot_handlers, "ensure_consent_prompted", _consent)
    monkeypatch.setattr(bot_handlers.AgentManager, "get_active", staticmethod(_no_active_agent))
    monkeypatch.setattr(bot_handlers, "_upsert_user", _upsert_user_spy)
    monkeypatch.setattr(bot_handlers, "_upsert_chat", _upsert_chat_spy(factory))

    registered = bot_handlers.build_handlers(
        graph=graph,
        db_session_factory=factory,
        settings=settings,
        presence=SimpleNamespace(mark_online=lambda _uid: None),
        storage=None,
        feature_engines=engines,
    )
    on_message = next(
        h.callback
        for h in registered
        if getattr(h, "callback", None) is not None and h.callback.__name__ == "on_message"
    )

    results: list[Any] = []

    async def send(text: str, *, chat_id: int = CHAT_A, user_id: int = USER_A) -> FakeUpdate:
        update = FakeUpdate(text, chat_id=chat_id, user_id=user_id)
        start = len(llm.calls)
        real_ainvoke = graph.ainvoke

        async def capture(state: Any, *a: Any, **kw: Any) -> Any:
            out = await real_ainvoke(state, *a, **kw)
            results.append(out)
            return out

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(graph, "ainvoke", capture)
            await on_message(update, SimpleNamespace(args=None, bot_data={}))
        update.prompts = llm.calls[start:]  # type: ignore[attr-defined]
        update.result = results[-1] if results else None  # type: ignore[attr-defined]
        return update

    return SimpleNamespace(
        send=send,
        memory=memory,
        llm=llm,
        factory=factory,
        graph=graph,
        on_message=on_message,
    )


async def _never(update: Any, context: Any) -> bool:
    return False


async def _consent(*_a: Any, **_kw: Any) -> None:
    return None


async def _no_active_agent(_user_id: int) -> None:
    return None


async def _upsert_user_spy(_db: Any, tg_user: Any) -> None:
    return None


def _upsert_chat_spy(factory: RecordingFactory):
    async def _spy(db: Any, chat_id: int, thread_id: str) -> None:
        factory.chats.append((chat_id, thread_id))

    return _spy


class _StubForceJoin:
    """Force-join is off by default; this makes 'off' explicit and schema-free."""

    async def should_block(self, user_id: int, chat_id: Any = "") -> bool:
        return False


class _StubMemoryEngine:
    """The consent/extraction engine, inert. Recall does not come from here."""

    def __init__(self) -> None:
        self.extractions: list[tuple[int, str]] = []

    async def get_context(self, user_id: int) -> str:
        return ""

    async def update_from_message(self, user_id: int, text: str) -> None:
        self.extractions.append((user_id, text))


# ── the tests ─────────────────────────────────────────────────────────────── #
async def test_a_stored_fact_reaches_the_reply_through_on_message(telegram) -> None:
    """NORTH STAR: store → real Telegram entry point → the model actually saw it.

    No state is built by hand here.  The only inputs are a stored memory and a
    Telegram-shaped Update, exactly as the running bot receives them.
    """
    await telegram.memory.store("tg:-1001", "User: my project is called Falcon\nAssistant: noted")

    update = await telegram.send("What was my project called again?")

    assert telegram.memory.reads, "the memory store was never read on the chat path"
    assert any(FACT in c["system"] or FACT in c["prompt"] for c in update.prompts), (
        "the stored fact never reached the model through on_message"
    )
    assert update.replies, "on_message replied to nobody"


async def test_on_message_invokes_the_graph_with_the_thread_id_base_state_derived(
    telegram,
) -> None:
    """The graph must run under the very thread id the chat was upserted with."""
    await telegram.send("hello there", chat_id=CHAT_A, user_id=USER_A)

    assert telegram.factory.chats, "on_message never upserted a chat"
    chat_id, thread_id = telegram.factory.chats[-1]
    assert (chat_id, thread_id) == (CHAT_A, f"tg:{CHAT_A}")
    assert telegram.memory.reads[-1]["thread_id"] == f"tg:{CHAT_A}"


async def test_one_users_memory_never_reaches_another_through_on_message(
    telegram,
) -> None:
    """The whole point of the thread fence, proven at the real entry point."""
    await telegram.memory.store("tg:-1001", "User: my project is called Falcon\nAssistant: noted")

    b = await telegram.send("What was my project called again?", chat_id=CHAT_B, user_id=USER_B)

    assert telegram.memory.reads, "user B's turn performed no read at all"
    assert telegram.memory.reads[-1]["thread_id"] == f"tg:{CHAT_B}"
    assert not any(FACT in c["system"] or FACT in c["prompt"] for c in b.prompts), (
        "user A's memory reached user B through the real entry point"
    )


async def test_a_second_chat_never_inherits_the_first_chats_memory(telegram) -> None:
    await telegram.memory.store("tg:-1001", "User: my project is called Falcon\nAssistant: noted")
    b = await telegram.send("remind me", chat_id=CHAT_B, user_id=USER_A)
    assert not any("Falcon" in c["system"] for c in b.prompts)


async def test_intent_unknown_is_overwritten_by_the_router(telegram) -> None:
    """PIN THE ORDERING ACCIDENT.

    ``on_message`` sets ``intent="unknown"`` right before invoking the graph
    (and ``_base_state`` sets it again).  That is currently harmless *only*
    because ``_router_node`` recomputes the intent with ``classify_intent``
    before ``route_intent`` ever reads it — the log line for this very call
    ends in ``intent=chat``.

    Both ends are observed here: what ``on_message`` handed the graph, and what
    came back.  Without this test the reason is folklore, and reordering the two
    nodes would silently send every turn down the chat path.
    """
    handed: dict[str, Any] = {}
    real_ainvoke = telegram.graph.ainvoke

    async def spy(state: Any, *a: Any, **kw: Any) -> Any:
        handed["intent"] = state.get("intent")
        handed["thread_id"] = state.get("thread_id")
        out = await real_ainvoke(state, *a, **kw)
        handed["classified"] = out.get("intent")
        return out

    monkey = pytest.MonkeyPatch()
    try:
        monkey.setattr(telegram.graph, "ainvoke", spy)
        await telegram.send("What was my project called again?")
    finally:
        monkey.undo()

    assert handed["intent"] == "unknown", (
        "on_message stopped forcing intent='unknown'; this pin is now stale"
    )
    assert handed["classified"] != "unknown", (
        "the router node stopped recomputing the intent — every turn would now "
        "be routed by whatever on_message happened to write"
    )
    assert handed["classified"] == "chat"


async def test_an_active_agent_bypasses_the_memory_path_entirely(telegram) -> None:
    """DOCUMENTED GAP, pinned so it cannot change by accident.

    When ``AgentManager.get_active`` returns an agent for the user, ``on_message``
    answers through ``active_agent.respond(...)`` and **never builds a graph
    state at all** — so that user gets no durable-memory read on any turn.  H1
    cannot fix this: the reader is inside the branch that is not taken.
    """
    calls: list[tuple[int, str, str]] = []

    class _Active:
        name = "echo"

        async def respond(self, user_id: int, text: str, **kw: Any) -> str:
            calls.append((user_id, text, str(kw.get("context"))))
            return "agent answer"

    async def _active(_uid: int) -> Any:
        return _Active()

    monkey = pytest.MonkeyPatch()
    try:
        monkey.setattr(bot_handlers.AgentManager, "get_active", staticmethod(_active))
        update = await telegram.send("What was my project called again?")
    finally:
        monkey.undo()

    assert update.replies == ["agent answer"]
    assert not telegram.memory.reads, (
        "the active-agent branch started reading durable memory; this pin is stale"
    )
    assert calls, "the active agent was never called"


async def test_the_reply_carries_exactly_what_the_graph_produced(telegram) -> None:
    """The answer the model produced is the answer the user receives.

    ``on_message`` is the last hop before Telegram: it decides what leaves the
    process.  A handler that answered from somewhere other than the graph — a
    canned string, a stale buffer, a swallowed exception — would leave every
    other guard in this repository green while the user saw the wrong thing.
    """
    update = await telegram.send("What was my project called again?")

    assert update.result is not None, "the graph was never invoked"
    produced = update.result.get("response") or ""
    assert produced, "the graph produced no response to forward"
    assert update.replies == [produced], "the reply sent to Telegram is not what the graph produced"
