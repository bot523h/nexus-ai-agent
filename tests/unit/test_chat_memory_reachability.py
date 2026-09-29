"""Chat→Memory reachability contract (task-205).

WHAT THIS FILE IS
-----------------
An end-to-end contract for the single question the repository could not answer:
**if a memory is stored, can a real user's question reach it?**

The chain under test is the production one, wired by
``cli.py::run_bot`` → ``orchestration.graph.compile_graph``:

    user message → _router_node (classify_intent) → route_intent
                → [memory reader] → persona agent → moderation → memory writer

Nothing here mocks the code under test.  The graph is compiled for real, the
store is a real sqlite store, and the LLM is a real ``LLMProvider`` subclass
that *records the system prompt it was handed* — because the only place a
memory can actually be observed is the prompt the model received.

THE DEFECT THIS FILE MEASURES
-----------------------------
``graph.py::route_intent`` routes ``task`` and ``memory`` through a memory
reader and sends **everything else — including ``chat`` — straight to
``route_persona``**.  A recall question like "What was my project called
again?" classifies as ``chat``, so the turn is answered from the last ten
messages with an empty ``memory_context``, while ``_memory_writer`` has been
storing that same user's turns for the whole conversation.  The store grows;
the reader never runs.

WHY SOME TESTS ARE MARKED ``xfail``
-----------------------------------
The fix for the above is a one-line change to
``src/nexus_ai_agent/orchestration/graph.py``.  **That file is under a live
lease** held by ``task-202-memory-write-observability`` (branch
``arena/01a0e907-nexus-ai-agent``, PR #119, claimed 2026-09-28T17:51:40Z,
24 h TTL).  A lease is not bypassed here.

So the invariant is declared, measured, and marked ``xfail`` instead of
being silently deleted or quietly marked ``skip``.  Each xfail carries the
lease that blocks it.  The suite is green today *because the failures are
declared*, not because the behaviour is correct — see
``CHAT_MEMORY_REACHABILITY_AUDIT_2026-09-29.md``.

These guards are not decorative: ``scripts/chat_memory_mutations.py`` applies
the candidate H1 patch to a **shadow copy of ``src/``** (never to the leased
file), proves this suite goes fully green, and then proves each guard kills a
named mutant.  ``xfail`` is non-strict on purpose: when H1 lands the tests
XPASS, which pytest reports as passed, so the fix can never leave a stale
red behind.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from nexus_ai_agent.agents.gemma_agent import GemmaAgent
from nexus_ai_agent.agents.phi_agent import PhiAgent
from nexus_ai_agent.agents.qwen_agent import QwenAgent
from nexus_ai_agent.llm.fake_llm import FakeLLMProvider
from nexus_ai_agent.memory.long_term import LongTermMemory
from nexus_ai_agent.orchestration.graph import compile_graph
from nexus_ai_agent.orchestration.state import NexusState
from nexus_ai_agent.storage.langgraph_checkpoint import get_checkpointer
from nexus_ai_agent.tools.registry import ToolRegistry

# ── the lease that blocks the fix ────────────────────────────────────────── #
H1_BLOCKING_LEASE = (
    "H1 is blocked: src/nexus_ai_agent/orchestration/graph.py is under the live "
    "lease of task-202 (branch arena/01a0e907, PR #119, expires 2026-09-29T17:51:40Z)"
)

#: A fact that exists ONLY in the store — it was never in the visible messages,
#: so it can only appear in a prompt if the memory reader actually ran.
AURORA = "The project the user is building is called Aurora Lantern."
ZEPHYR = "The project owned by the other user is called Zephyr Quartz."

#: Real recall phrasings.  Deliberately **not** keyword-matched to
#: ``MEMORY_KEYWORDS``: a user does not say "recall", they ask a question.
RECALL_QUESTIONS = [
    "What was my project called again?",
    "Where did we leave off?",
    "What was the thing I told you earlier?",
    "Do you remember the name I gave my project?",
    "یادم هست اسم پروژه چی بود؟",
]

#: One control question that classifies as ``memory`` today.  It is what makes
#: the tripwires falsifiable: the harness demonstrably CAN detect a positive.
CONTROL_QUESTION = "Do you remember the name I gave my project?"


# ── instrumentation ──────────────────────────────────────────────────────── #
class RecordingLLM(FakeLLMProvider):
    """A real provider that remembers every prompt it was handed.

    The system prompt is the only place ``memory_context`` can be observed
    from the outside: it is rendered by ``PersonalityEngine.build_system_prompt``
    and passed to ``llm.generate``.  If a stored fact is not in one of these,
    the model never saw it — regardless of what the state dict claims.
    """

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[dict[str, str]] = []

    async def generate(self, prompt: str, system: str = "") -> str:  # type: ignore[override]
        self.calls.append({"prompt": prompt, "system": system})
        return await super().generate(prompt=prompt, system=system)


class AuditedMemory(LongTermMemory):
    """Real :class:`LongTermMemory` that audits every read and write.

    ``thread_id`` is the ONLY thing that scopes a read, and the only place a
    cross-user leak could originate.  Recording the arguments of every call
    turns that claim from an assertion about SQL into an observation of what
    the runtime actually asked for.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.reads: list[dict[str, Any]] = []
        self.writes: list[dict[str, Any]] = []

    async def search(  # type: ignore[override]
        self,
        thread_id: str,
        query: str,
        top_k: int = 3,
    ) -> list[str]:
        self.reads.append({"thread_id": thread_id, "query": query, "top_k": top_k})
        return await super().search(thread_id, query, top_k=top_k)

    async def store(  # type: ignore[override]
        self,
        thread_id: str,
        text: str,
        metadata: dict | None = None,
    ) -> None:
        self.writes.append({"thread_id": thread_id, "text": text})
        await super().store(thread_id, text, metadata=metadata)


def _state(thread_id: str, text: str, *, chat_id: int = 0, user_id: int = 0) -> NexusState:
    return {
        "thread_id": thread_id,
        "chat_id": chat_id,
        "user_id": user_id,
        "correlation_id": "task-205",
        "messages": [{"role": "user", "content": text}],
        "intent": "unknown",
        "active_persona": "",
        "current_task": None,
        "tool_results": [],
        "memory_context": "",
        "response": "",
        "error": None,
        "turn_count": 0,
        "moderation_passed": True,
    }


def _build(memory: AuditedMemory, llm: RecordingLLM):
    registry = ToolRegistry(enable_shell=False, workspace_root=".")
    return compile_graph(llm, get_checkpointer(":memory:"), memory, registry)


async def _turn(graph: Any, llm: RecordingLLM, thread_id: str, text: str, **kw: Any) -> dict:
    """Run one real turn and return the prompts issued *during that turn only*."""
    start = len(llm.calls)
    result = await graph.ainvoke(
        _state(thread_id, text, **kw),
        config={"configurable": {"thread_id": thread_id}},
    )
    result["_prompts"] = llm.calls[start:]
    return result


def _saw(turn: dict, fact: str) -> bool:
    """Did ``fact`` reach the model, or the answer, during this turn?"""
    return any(fact in c["system"] or fact in c["prompt"] for c in turn["_prompts"]) or (
        fact in (turn.get("response") or "")
    )


def _harness():
    llm = RecordingLLM()
    memory = AuditedMemory(":memory:", llm)
    return llm, memory, _build(memory, llm)


# ═══════════════════════════════════════════════════════════════════════════ #
# 1. THE CONTROL — proof that this suite can detect a positive
# ═══════════════════════════════════════════════════════════════════════════ #
async def test_control_an_intent_that_reads_memory_really_delivers_it(settings_override) -> None:
    """A ``memory``-intent turn carries the stored fact all the way to the LLM.

    Without this, every xfail below would be unfalsifiable: a harness that can
    never see a memory would report the same "not reached" for a working
    system and for a broken one.
    """
    llm, memory, graph = _harness()
    thread = f"tg:control-{uuid.uuid4().hex[:8]}"
    await memory.store(thread, AURORA)

    turn = await _turn(graph, llm, thread, CONTROL_QUESTION)

    assert memory.reads, "the control question must actually read memory"
    assert _saw(turn, AURORA), (
        "control broken: a turn that DID read memory did not deliver it to the model — "
        "the harness cannot be trusted to detect reachability"
    )


# ═══════════════════════════════════════════════════════════════════════════ #
# 2. THE GAP — real recall questions that lose their memory
# ═══════════════════════════════════════════════════════════════════════════ #
@pytest.mark.parametrize("question", RECALL_QUESTIONS)
@pytest.mark.xfail(reason=H1_BLOCKING_LEASE, strict=False)
async def test_a_recall_question_delivers_stored_memory_to_the_model(
    question: str, settings_override
) -> None:
    """storage → retrieval → chat → answer, for a question a user really asks."""
    llm, memory, graph = _harness()
    thread = f"tg:recall-{uuid.uuid4().hex[:8]}"
    await memory.store(thread, AURORA)

    turn = await _turn(graph, llm, thread, question)

    assert memory.reads, (
        f"no memory read happened for {question!r}: the turn was answered with no "
        "retrieval at all, which is exactly the 'empty == success' failure mode"
    )
    assert _saw(turn, AURORA), f"stored memory never reached the model for {question!r}"


@pytest.mark.parametrize("question", RECALL_QUESTIONS)
@pytest.mark.xfail(reason=H1_BLOCKING_LEASE, strict=False)
async def test_a_stored_turn_is_recallable_on_the_next_turn(
    question: str, settings_override
) -> None:
    """The north-star chain: something told in turn 1 is usable in turn 2.

    Turn 1 writes (the memory writer runs for *every* intent).  Turn 2 asks.
    Nothing in the transcript helps the model — the fact exists only in the
    store — so reaching the answer requires the reader to run.
    """
    llm, memory, graph = _harness()
    thread = f"tg:roundtrip-{uuid.uuid4().hex[:8]}"

    await _turn(graph, llm, thread, "My project is called Aurora Lantern.")
    assert any(AURORA.split("called ")[-1] in w["text"] for w in memory.writes), (
        "turn 1 was not written to long-term memory at all"
    )

    turn2 = await _turn(graph, llm, thread, question)

    assert _saw(turn2, "Aurora Lantern"), (
        "a fact this user said one turn ago is not reachable in the next turn"
    )


@pytest.mark.xfail(reason=H1_BLOCKING_LEASE, strict=False)
async def test_the_writer_and_the_reader_cover_the_same_intents(settings_override) -> None:
    """Write symmetry: the runtime must not store what it can never read back.

    ``_memory_writer`` runs for every intent; ``route_intent`` reads for two.
    Every turn stored on a ``chat`` intent is, by construction, a row that only
    a future H1 can reach.

    The check is **per turn**, not per conversation: a single read anywhere in
    the session would otherwise mask four turns that stored a memory and then
    answered from nothing.  (An earlier draft asserted only "some read
    happened" and passed for exactly that reason — a guard that cannot fail is
    not a guard.)
    """
    llm, memory, graph = _harness()
    thread = f"tg:symmetry-{uuid.uuid4().hex[:8]}"

    write_only: list[str] = []
    for question in RECALL_QUESTIONS:
        reads_before = len(memory.reads)
        writes_before = len(memory.writes)
        await _turn(graph, llm, thread, question)
        wrote = len(memory.writes) > writes_before
        read = len(memory.reads) > reads_before
        assert wrote, f"turn {question!r} was not stored — the fixture is wrong"
        if not read:
            write_only.append(question)

    escaped = [w["thread_id"] for w in memory.writes if w["thread_id"] != thread]
    assert not escaped, f"writes escaped their thread: {set(escaped)}"
    assert not write_only, (
        f"{len(write_only)} of {len(RECALL_QUESTIONS)} turns were written to "
        f"long-term memory and never read back: {write_only}"
    )


# ═══════════════════════════════════════════════════════════════════════════ #
# 3. THE SINK — proves the only missing link is the read, not the rendering
# ═══════════════════════════════════════════════════════════════════════════ #
@pytest.mark.parametrize(
    "agent_cls", [GemmaAgent, PhiAgent, QwenAgent], ids=["gemma", "phi", "qwen"]
)
async def test_persona_agents_render_memory_context_into_the_system_prompt(
    agent_cls: type, settings_override
) -> None:
    """Every persona already puts ``memory_context`` in front of the model.

    This localises the defect: rendering is not broken, so no amount of work on
    the agents or on ``PersonalityEngine`` can fix the user-visible symptom.
    """
    llm = RecordingLLM()
    state = _state("tg:render", "hello")
    state["memory_context"] = AURORA

    await agent_cls(llm).run(state)  # type: ignore[call-arg]

    assert llm.calls, "the agent issued no model call at all"
    assert any(AURORA in c["system"] for c in llm.calls), (
        f"{agent_cls.__name__} dropped memory_context on the floor"
    )


# ═══════════════════════════════════════════════════════════════════════════ #
# 4. ADVERSARIAL — thread A must never answer for thread B  (LAW 4)
# ═══════════════════════════════════════════════════════════════════════════ #
async def test_thread_a_memory_never_reaches_thread_b(settings_override) -> None:
    """One shared store, two threads, one leak would be caught here.

    Both turns deliberately use a ``memory``-intent question so the reader is
    guaranteed to run and return rows — otherwise "no leak" would be vacuous.
    """
    llm, memory, graph = _harness()
    alice, bob = "tg:alice", "tg:bob"
    await memory.store(alice, AURORA)
    await memory.store(bob, ZEPHYR)

    a = await _turn(graph, llm, alice, CONTROL_QUESTION)
    assert _saw(a, AURORA), "alice did not receive her own memory — the test is broken"
    assert not _saw(a, ZEPHYR), "CROSS-THREAD LEAK: bob's memory reached alice"

    b = await _turn(graph, llm, bob, CONTROL_QUESTION)
    assert _saw(b, ZEPHYR), "bob did not receive his own memory — the test is broken"
    assert not _saw(b, AURORA), "CROSS-THREAD LEAK: alice's memory reached bob"


async def test_user_a_memory_never_reaches_user_b(settings_override) -> None:
    """The Telegram identity triple is the outer fence: chat_id + user_id.

    ``bot/handlers.py::_base_state`` derives ``thread_id`` from the chat id, so
    two users in one group share a thread while a private chat is its own.  The
    invariant under test is the one the runtime actually enforces: a read is
    scoped by ``thread_id``, and nothing may widen it.
    """
    llm, memory, graph = _harness()
    await memory.store("tg:-1001", AURORA)
    await memory.store("tg:-2002", ZEPHYR)

    shared_group = await _turn(graph, llm, "tg:-1001", CONTROL_QUESTION, chat_id=-1001, user_id=111)
    private = await _turn(graph, llm, "tg:-2002", CONTROL_QUESTION, chat_id=-2002, user_id=222)

    assert not _saw(shared_group, ZEPHYR), "user 222's memory reached user 111's turn"
    assert not _saw(private, AURORA), "user 111's memory reached user 222's turn"


async def test_every_memory_read_is_scoped_to_the_calling_thread(settings_override) -> None:
    """Every ``search()`` the runtime issues carries the caller's own thread.

    This is the *caller* half of the scoping contract, observed at the API
    boundary: a widening bug shows up as a read whose ``thread_id`` is not the
    one the turn is running under.  It deliberately does **not** claim to
    catch a store that ignores the id it was handed — that is the next test,
    and it is the one that guards the SQL.
    """
    llm, memory, graph = _harness()
    owner = f"tg:scoped-{uuid.uuid4().hex[:8]}"
    await memory.store(owner, AURORA)
    await memory.store("tg:somebody-else", ZEPHYR)

    await _turn(graph, llm, owner, CONTROL_QUESTION)

    assert memory.reads, "the fixture did not cause a read; the test proves nothing"
    foreign = [r for r in memory.reads if r["thread_id"] != owner]
    assert not foreign, f"memory was read on behalf of another thread: {foreign}"


async def test_a_read_never_returns_another_threads_memories(settings_override) -> None:
    """The store half of the same contract — and the authoritative leak guard.

    ``memory_context`` is whatever ``search`` returns, so the SQL
    ``WHERE thread_id = ?`` *is* the user-isolation boundary.  This test reads
    with a ``top_k`` far larger than the corpus, which makes it independent of
    the ranking lane: whether the store is using cosine distance over
    hash-seeded vectors or falling back to ``ORDER BY id DESC``, a thread whose
    filter was dropped returns every row in the table.

    The end-to-end leak tests above are *probabilistic* detectors — a widened
    filter can still hide the other user's row behind ``top_k=3`` and a lucky
    ordering.  This one is not.
    """
    llm = RecordingLLM()
    memory = AuditedMemory(":memory:", llm)
    mine, theirs = "tg:mine", "tg:theirs"
    for i in range(3):
        await memory.store(mine, f"mine-{i}: {AURORA}")
        await memory.store(theirs, f"theirs-{i}: {ZEPHYR}")

    rows = await memory.search(mine, CONTROL_QUESTION, top_k=64)

    assert rows, "the fixture returned nothing; the test proves nothing"
    foreign = [r for r in rows if not r.startswith("mine-")]
    assert not foreign, (
        f"CROSS-THREAD LEAK at the store: {len(foreign)} of {len(rows)} rows "
        f"belonged to another thread (lane: "
        f"vector={'on' if memory._use_vec else 'off (recency)'})"
    )


# ═══════════════════════════════════════════════════════════════════════════ #
# 5. FAILURE SEMANTICS — a broken backend is not an empty recall  (LAW 11)
# ═══════════════════════════════════════════════════════════════════════════ #
class _BrokenMemory(AuditedMemory):
    """A store whose backend is down.  It fails the way a real one would."""

    async def search(self, thread_id: str, query: str, top_k: int = 3) -> list[str]:  # type: ignore[override]
        self.reads.append({"thread_id": thread_id, "query": query, "top_k": top_k})
        raise OSError("memory backend unreachable")


async def test_an_unavailable_backend_never_aborts_the_conversation(settings_override) -> None:
    """Availability beats recall: the user still gets an answer."""
    llm = RecordingLLM()
    memory = _BrokenMemory(":memory:", llm)
    graph = _build(memory, llm)
    thread = f"tg:broken-{uuid.uuid4().hex[:8]}"

    turn = await _turn(graph, llm, thread, CONTROL_QUESTION)

    assert turn.get("response"), "an unavailable memory backend silenced the whole turn"
    assert memory.reads, "the fixture never actually attempted a read"


@pytest.mark.xfail(
    reason=H1_BLOCKING_LEASE + " (the one-line fix does not type the failure)", strict=False
)
async def test_an_unavailable_backend_is_not_reported_as_a_successful_empty_recall(
    settings_override,
) -> None:
    """``empty == success`` must not be the only outcome a caller can observe.

    ``_memory_reader`` currently does ``except Exception: state.setdefault(...)``
    on a key that is always already present, so "the store is down" and "you
    have no memories" are the same ``""``.

    The contract is expressed through the field the state **already has** —
    ``NexusState.error`` — rather than through a new enum, because the
    repository's typed taxonomy (``jobs/failure_semantics.py``) belongs to the
    job lifecycle and must not be imported into the ``memory/`` leaf.  Note
    that the one-line H1 wiring does **not** satisfy this: reachability and
    typed failure are two separate changes, and only the first is available
    once the lease clears.
    """
    llm = RecordingLLM()
    memory = _BrokenMemory(":memory:", llm)
    graph = _build(memory, llm)
    thread = f"tg:typed-{uuid.uuid4().hex[:8]}"

    turn = await _turn(graph, llm, thread, CONTROL_QUESTION)

    assert memory.reads, "the fixture never attempted a read"
    assert turn.get("error"), (
        "the memory backend failed and the turn reports no error — a failed "
        "retrieval is indistinguishable from a successful one with no hits"
    )


async def test_a_successful_empty_recall_is_not_reported_as_an_error(settings_override) -> None:
    """The other half of the same invariant: a healthy empty store is not an error.

    Without this, the fix for the test above could simply be "always set
    ``error``", which would type every silent answer as a failure.
    """
    llm, memory, graph = _harness()
    thread = f"tg:healthy-{uuid.uuid4().hex[:8]}"

    turn = await _turn(graph, llm, thread, CONTROL_QUESTION)

    assert memory.reads, "the fixture never attempted a read"
    assert not turn.get("error"), (
        f"a healthy recall with no matching memory was reported as an error: {turn.get('error')!r}"
    )


# ═══════════════════════════════════════════════════════════════════════════ #
# 6. U1 HONESTY — what the retrieval lane on this base actually does
# ═══════════════════════════════════════════════════════════════════════════ #
async def test_retrieval_applies_no_relevance_floor(settings_override) -> None:
    """Main's retrieval has no relevance gate at all (U1, measured — not a score).

    ``LongTermMemory.search`` either ranks by cosine distance over the
    *hash-seeded* vectors of ``FakeLLMProvider`` (which express identity, not
    meaning — see PR #121 §1) or falls back to ``ORDER BY id DESC``.  Neither
    lane can refuse a query.  A paraphrase that shares no content word with
    the stored text still returns it, which is the concrete reason the chat
    wiring gap matters: reaching memory is necessary but not sufficient, and
    this test is the honest floor of what "reached" is worth today.

    This asserts the *absence* of a floor, not retrieval quality.  A quality
    benchmark needs the hybrid retriever in PR #121's ``retrieval/`` package,
    which is under that PR's live lease.
    """
    llm = RecordingLLM()
    memory = AuditedMemory(":memory:", llm)
    thread = "tg:paraphrase"
    await memory.store(thread, "The R2 blob tier stores database backups.")

    results = await memory.search(thread, "Where are our database snapshots kept?", top_k=3)

    assert results, "retrieval refused an unrelated query — a relevance floor exists"
    assert any("database backups" in r for r in results), (
        f"the paraphrase missed the only related memory; lane used: "
        f"vector={'on' if memory._use_vec else 'off (recency)'}"
    )


# ═══════════════════════════════════════════════════════════════════════════ #
# 7. THE REAL ENTRY POINT — everything above hand-builds NexusState
# ═══════════════════════════════════════════════════════════════════════════ #
# The tests above construct the state dict themselves, which is a real weakness:
# the production entry point is ``bot/handlers.py::on_message`` → ``_base_state`` →
# ``graph.ainvoke``, and nothing here would notice if that path stopped threading
# the caller's identity.  ``bot/handlers.py`` is under no live lease, but seven open
# PRs rewrite it, so it is READ here, never edited — the test lives in this file,
# which is the path task-205 owns.
from types import SimpleNamespace  # noqa: E402

from nexus_ai_agent.bot import handlers as bot_handlers  # noqa: E402


def _telegram_update(chat_id: int, user_id: int, text: str) -> SimpleNamespace:
    """The minimum surface ``_base_state`` actually reads from an Update."""
    return SimpleNamespace(
        effective_chat=SimpleNamespace(id=chat_id),
        effective_user=SimpleNamespace(id=user_id),
        message=SimpleNamespace(text=text),
    )


def test_the_real_entry_point_threads_the_callers_identity() -> None:
    """``_base_state`` is the production constructor of the state; pin its contract.

    The graph scopes every read by ``state["thread_id"]``.  If the entry point
    stopped deriving it, or derived it from something that collides, the whole
    isolation argument would rest on a value nobody asserts.
    """
    update = _telegram_update(chat_id=-1001, user_id=777, text="hello")
    state = bot_handlers._base_state(update, "hello")

    assert state["thread_id"] == "tg:-1001"
    assert state["chat_id"] == -1001
    assert state["user_id"] == 777
    assert state["messages"] == [{"role": "user", "content": "hello"}]


def test_thread_id_is_derived_from_the_chat_alone_not_the_user() -> None:
    """Characterisation: the memory scope is the *chat*, and that has a cost.

    Two members of one group chat therefore share a single memory scope — one
    member's stored turn is recallable in the next member's turn.  That is the
    measured basis of ``task-210``; the invariant people usually assume ("user A
    ≠ user B") is only true for private chats.

    This is a characterisation, not an endorsement: it pins the *derivation rule*
    so that changing the scope is a deliberate, visible act rather than an
    accident.  The desired per-user behaviour is asserted separately, below.
    """
    first = bot_handlers._base_state(_telegram_update(-1001, 111, "a"), "a")
    second = bot_handlers._base_state(_telegram_update(-1001, 222, "b"), "b")

    assert first["thread_id"] == second["thread_id"], (
        "group scoping changed — if this is intentional, update task-210 and the "
        "isolation tests above before changing anything else"
    )
    assert first["user_id"] != second["user_id"]


@pytest.mark.xfail(reason="task-210: the memory scope is the chat, not the user", strict=False)
async def test_a_group_member_cannot_recall_another_members_memory(settings_override) -> None:
    """The desired invariant, RED today.  Filed as a decision, not a fix.

    Unlike the H1 tripwires this one is not blocked by a lease — it is blocked by
    a *product* decision nobody has made yet: should a Telegram group have one
    shared memory, or one per member?  Answering it wrongly in either direction
    leaks or forgets, so it is stated, not guessed.
    """
    llm, memory, graph = _harness()
    group = -1001
    await memory.store(f"tg:{group}", AURORA)

    outsider = await _turn(
        graph,
        llm,
        f"tg:{group}",
        CONTROL_QUESTION,
        chat_id=group,
        user_id=999,
    )

    assert not _saw(outsider, AURORA), (
        "a different member of the same group recalled another member's memory"
    )


@pytest.mark.xfail(reason=H1_BLOCKING_LEASE, strict=False)
async def test_a_recall_sent_through_the_real_entry_point_reaches_the_model(
    settings_override,
) -> None:
    """North star, driven from the production constructor.

    A fact is stored for this user's thread, the user asks a real recall question
    through ``_base_state`` — the exact dict ``on_message`` hands the graph — and
    the fact must arrive at the model.  Nothing here mocks the code under test.
    """
    llm, memory, graph = _harness()
    update = _telegram_update(chat_id=-4242, user_id=31337, text=RECALL_QUESTIONS[0])
    state = bot_handlers._base_state(update, RECALL_QUESTIONS[0])
    await memory.store(state["thread_id"], AURORA)

    start = len(llm.calls)
    result = await graph.ainvoke(state, config={"configurable": {"thread_id": state["thread_id"]}})
    prompts = llm.calls[start:]

    assert memory.reads, "the real entry point produced a turn that read no memory"
    assert any(AURORA in c["system"] for c in prompts) or AURORA in (
        result.get("response") or ""
    ), "a recall sent through the production entry point never delivered the memory"
