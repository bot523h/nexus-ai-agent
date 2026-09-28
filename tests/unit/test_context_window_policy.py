"""The context-window policy: one owner, two live bounds, no persona divergence.

Before this, ``ShortTermMemory`` had zero callers and the conversation window was
re-implemented inline in six places with four different sizes — phi saw the last
8 messages, qwen and chat_agent 10, gemma 12. ``select_persona`` routes on ~50
keyword literals, so *which* persona a turn landed on silently decided how much
of the user's own conversation the assistant could see. Three settings
(``max_short_term_messages``, ``max_tokens_before_summary``, ``top_k_memories``)
were declared and read by nothing.

These tests pin the unification. The headline invariant is
:func:`test_every_persona_sees_the_same_window` — it is the property that could
not even be stated before, because there was no single window to state it about.
"""

from __future__ import annotations

import pytest

from nexus_ai_agent.agents.chat_agent import ChatAgent
from nexus_ai_agent.agents.gemma_agent import GemmaAgent
from nexus_ai_agent.agents.phi_agent import PhiAgent
from nexus_ai_agent.agents.qwen_agent import QwenAgent
from nexus_ai_agent.config import settings as settings_module
from nexus_ai_agent.memory.short_term import ShortTermMemory


class RecordingLLM:
    """Records the prompt it was handed so the window can be observed."""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def generate(self, prompt: str, system: str | None = None) -> str:
        _ = system
        self.prompts.append(prompt)
        return "ok"

    async def embed(self, text: str) -> list[float]:
        _ = text
        return [0.0] * 384


def _conversation(turns: int) -> list[dict]:
    messages: list[dict] = []
    for index in range(turns):
        role = "user" if index % 2 == 0 else "assistant"
        messages.append({"role": role, "content": f"message number {index}"})
    return messages


def _state(messages: list[dict]) -> dict:
    return {
        "thread_id": "t",
        "chat_id": 1,
        "user_id": 1,
        "correlation_id": "c",
        "messages": messages,
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


# ── the policy itself ────────────────────────────────────────────────────


def test_get_window_honours_its_own_limit() -> None:
    """It used to return ``messages[-20:]`` whatever ``MAX_MESSAGES`` said."""
    messages = _conversation(50)
    assert len(ShortTermMemory(max_messages=7).get_window(messages)) == 7
    assert len(ShortTermMemory(max_messages=20).get_window(messages)) == 20
    assert ShortTermMemory(max_messages=7).get_window(messages)[-1] == messages[-1]


def test_get_window_keeps_the_newest_not_the_oldest() -> None:
    messages = _conversation(30)
    window = ShortTermMemory(max_messages=5).get_window(messages)
    assert window == messages[-5:]


def test_token_budget_bounds_a_long_conversation() -> None:
    """The second bound: raising the count must not mean an unbounded prompt."""
    long_messages = [{"role": "user", "content": "x" * 400} for _ in range(40)]
    window = ShortTermMemory(max_messages=40, max_tokens_before_summary=1000).get_window(
        long_messages
    )
    assert len(window) < 40, "the budget must actually trim"
    assert ShortTermMemory(max_messages=40).estimate_tokens(window) <= 1000
    assert window[-1] == long_messages[-1], "trimming drops the oldest"


def test_a_single_oversized_message_never_yields_an_empty_window() -> None:
    """Fail-safe: an empty window would silently send the model no conversation."""
    huge = [{"role": "user", "content": "y" * 100_000}]
    assert ShortTermMemory(max_tokens_before_summary=10).get_window(huge) == huge
    assert ShortTermMemory().render(huge), "render must not come back empty"


def test_empty_and_missing_input_is_safe() -> None:
    assert ShortTermMemory().get_window([]) == []
    assert ShortTermMemory().render([]) == ""
    assert ShortTermMemory().estimate_tokens([]) == 0


def test_render_is_the_single_join_and_uses_the_window() -> None:
    messages = _conversation(30)
    rendered = ShortTermMemory(max_messages=3).render(messages)
    assert rendered == "\n".join(
        f"{m['role']}: {m['content']}" for m in messages[-3:]
    )


def test_render_tolerates_a_malformed_message() -> None:
    """State comes off a checkpoint; a missing key must not raise mid-turn."""
    rendered = ShortTermMemory().render([{"role": "user"}, {"content": "orphan"}, {}])
    assert isinstance(rendered, str)


def test_estimate_tokens_is_deterministic() -> None:
    policy = ShortTermMemory()
    messages = _conversation(12)
    assert policy.estimate_tokens(messages) == policy.estimate_tokens(messages)
    assert policy.estimate_tokens(messages) > 0


def test_should_summarize_is_a_sync_predicate_on_the_budget() -> None:
    """It was ``async`` while doing no I/O — a pointless await on a pure check."""
    policy = ShortTermMemory(max_tokens_before_summary=10)
    assert policy.should_summarize(_conversation(40)) is True
    assert ShortTermMemory(max_tokens_before_summary=100_000).should_summarize(
        _conversation(4)
    ) is False


@pytest.mark.asyncio
async def test_summarize_covers_the_same_window_the_personas_see() -> None:
    """It used to slice its own ``[-10:]``, so summary and reply could disagree."""
    llm = RecordingLLM()
    messages = _conversation(30)
    policy = ShortTermMemory(max_messages=5)
    await policy.summarize(messages, llm)  # type: ignore[arg-type]

    assert llm.prompts, "the summariser was called"
    prompt = llm.prompts[0]
    assert "message number 29" in prompt, "the newest message is in the summary"
    assert "message number 0" not in prompt, "and messages outside the window are not"
    for message in policy.get_window(messages):
        assert message["content"] in prompt


# ── the headline invariant: no persona divergence ────────────────────────


@pytest.mark.asyncio
async def test_every_persona_sees_the_same_window(settings_override) -> None:
    """Same conversation ⇒ same visible history, whichever persona answers.

    This is the property that did not exist before: phi saw 8 messages, qwen 10,
    gemma 12, so the answer depended on a keyword match rather than on the
    conversation.
    """
    messages = _conversation(40)
    seen: dict[str, str] = {}
    for name, agent in (
        ("phi", PhiAgent(RecordingLLM())),  # type: ignore[arg-type]
        ("qwen", QwenAgent(RecordingLLM())),  # type: ignore[arg-type]
        ("gemma", GemmaAgent(RecordingLLM())),  # type: ignore[arg-type]
        ("chat", ChatAgent(RecordingLLM())),  # type: ignore[arg-type]
    ):
        llm = RecordingLLM()
        agent.llm = llm  # type: ignore[assignment]
        await agent.run(_state(messages))  # type: ignore[arg-type]
        assert llm.prompts, f"{name} did not reach the model"
        seen[name] = llm.prompts[0]

    conversations = {prompt.split("\nassistant:")[0] for prompt in seen.values()}
    # chat_agent has no "\nassistant:" suffix, so compare on the shared window
    # instead: every persona must contain exactly the configured last N messages.
    policy = ShortTermMemory(
        max_messages=settings_module.get_settings().max_short_term_messages,
        max_tokens_before_summary=settings_module.get_settings().max_tokens_before_summary,
    )
    expected = policy.render(messages)
    for name, prompt in seen.items():
        window_in_prompt = "\n".join(
            line for line in prompt.splitlines() if line.startswith(("user: ", "assistant: "))
        )
        assert window_in_prompt == expected, f"{name} rendered a different window"
    assert len(conversations) >= 1


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [3, 6, 15])
async def test_the_settings_knob_is_live_not_decoration(settings_override, limit: int) -> None:
    """``NEXUS_MAX_SHORT_TERM_MESSAGES`` changed nothing before this."""
    import os

    os.environ["MAX_SHORT_TERM_MESSAGES"] = str(limit)
    settings_module.get_settings.cache_clear()
    try:
        agent = PhiAgent(RecordingLLM())  # type: ignore[arg-type]
        llm = RecordingLLM()
        agent.llm = llm  # type: ignore[assignment]
        await agent.run(_state(_conversation(40)))  # type: ignore[arg-type]

        rendered = llm.prompts[0].split("\nassistant:")[0]
        assert rendered.count("message number") == limit
        assert f"message number {39}" in rendered
        assert f"message number {39 - limit}" not in rendered
    finally:
        os.environ.pop("MAX_SHORT_TERM_MESSAGES", None)
        settings_module.get_settings.cache_clear()


@pytest.mark.asyncio
async def test_token_budget_knob_is_live(settings_override) -> None:
    import os

    os.environ["MAX_TOKENS_BEFORE_SUMMARY"] = "20"
    settings_module.get_settings.cache_clear()
    try:
        agent = GemmaAgent(RecordingLLM())  # type: ignore[arg-type]
        llm = RecordingLLM()
        agent.llm = llm  # type: ignore[assignment]
        long_messages = [{"role": "user", "content": "z" * 200} for _ in range(30)]
        await agent.run(_state(long_messages))  # type: ignore[arg-type]
        rendered = llm.prompts[0]
        assert rendered.count("z" * 200) < 30, "the budget trimmed the prompt"
    finally:
        os.environ.pop("MAX_TOKENS_BEFORE_SUMMARY", None)
        settings_module.get_settings.cache_clear()


# ── boundary ─────────────────────────────────────────────────────────────


def test_short_term_stays_a_pure_leaf() -> None:
    """The policy takes limits as arguments; reading settings is composition.

    ``tests/architecture/test_memory_boundaries.py`` forbids ``features``/``bot``
    imports from ``memory/``. Keeping ``config`` out too is what lets the same
    policy be reused by a composition root that is not the Telegram bot.
    """
    import ast
    from pathlib import Path

    source = Path("src/nexus_ai_agent/memory/short_term.py").read_text(encoding="utf-8")
    imported: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    assert not any(name.startswith(("nexus_ai_agent.config", "nexus_ai_agent.features")) for name in imported), imported


def test_base_agent_owns_the_settings_read() -> None:
    """One place reads settings, so one place has to change."""
    from nexus_ai_agent.agents.base import BaseAgent

    policy = BaseAgent.short_term(PhiAgent(RecordingLLM()))  # type: ignore[arg-type]
    settings = settings_module.get_settings()
    assert isinstance(policy, ShortTermMemory)
    assert policy.max_messages == settings.max_short_term_messages
    assert policy.max_tokens_before_summary == settings.max_tokens_before_summary
