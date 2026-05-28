import pytest

from nexus_ai_agent.agents.gemma_agent import GemmaAgent
from nexus_ai_agent.agents.phi_agent import PhiAgent
from nexus_ai_agent.agents.qwen_agent import QwenAgent
from nexus_ai_agent.llm.fake_llm import FakeLLMProvider


def _state(text):
    return {
        "thread_id": "test",
        "chat_id": 0,
        "user_id": 0,
        "correlation_id": "x",
        "messages": [{"role": "user", "content": text}],
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


@pytest.mark.asyncio
async def test_phi():
    r = await PhiAgent(FakeLLMProvider()).run(_state("Explain AI"))
    assert r["active_persona"] == "phi"
    assert r["response"]


@pytest.mark.asyncio
async def test_qwen():
    r = await QwenAgent(FakeLLMProvider()).run(_state("Tell me a story"))
    assert r["active_persona"] == "qwen"
    assert r["response"]


@pytest.mark.asyncio
async def test_gemma():
    r = await GemmaAgent(FakeLLMProvider()).run(_state("I feel sad today"))
    assert r["active_persona"] == "gemma"
    assert r["response"]


@pytest.mark.asyncio
async def test_phi_moderate():
    result = await PhiAgent(FakeLLMProvider()).moderate("Hello world")
    assert "safe" in result

