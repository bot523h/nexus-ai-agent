"""AI memory is opt-in: no LLM call, no stored profile without consent (B7)."""

from unittest.mock import AsyncMock

import pytest

from nexus_ai_agent.features.ai_memory import AIMemoryEngine, parse_extraction


@pytest.mark.asyncio
async def test_extract_context_after_opt_in():
    mock_gemini = AsyncMock()
    mock_gemini.generate.return_value = '{"name": "Majid", "occupation": "Developer"}'

    engine = AIMemoryEngine(gemini_provider=mock_gemini)
    assert await engine.enable(123) is True
    assert await engine.enable(123) is False  # already on
    assert await engine.update_from_message(123, "My name is Majid and I am a developer")

    context = await engine.get_context(123)
    assert "Majid" in context
    assert "Developer" in context
    mock_gemini.generate.assert_awaited_once()
    await engine.forget_user(123)


@pytest.mark.asyncio
async def test_without_opt_in_nothing_is_sent_to_the_llm():
    mock_gemini = AsyncMock()
    mock_gemini.generate.return_value = '{"name": "Majid"}'
    engine = AIMemoryEngine(gemini_provider=mock_gemini)

    assert await engine.is_enabled(777) is False
    assert await engine.update_from_message(777, "My name is Majid and I live in Tehran") is False

    mock_gemini.generate.assert_not_awaited()
    assert await engine.get_context(777) == ""


@pytest.mark.asyncio
async def test_commands_and_tiny_messages_skip_the_llm_even_when_enabled():
    mock_gemini = AsyncMock()
    engine = AIMemoryEngine(gemini_provider=mock_gemini)
    await engine.enable(778)
    try:
        assert await engine.update_from_message(778, "/start") is False
        assert await engine.update_from_message(778, "ok") is False
        mock_gemini.generate.assert_not_awaited()
    finally:
        await engine.forget_user(778)


@pytest.mark.asyncio
async def test_disable_wipes_and_revokes_consent():
    mock_gemini = AsyncMock()
    mock_gemini.generate.return_value = '{"interests": ["chess", "python"]}'
    engine = AIMemoryEngine(gemini_provider=mock_gemini)
    await engine.enable(779)
    await engine.update_from_message(779, "I love playing chess and writing python")
    assert "chess" in await engine.get_context(779)

    await engine.disable(779)
    assert await engine.is_enabled(779) is False
    assert await engine.get_context(779) == ""
    # learning stays off afterwards
    assert await engine.update_from_message(779, "I also like hiking a lot these days") is False
    assert mock_gemini.generate.await_count == 1


@pytest.mark.asyncio
async def test_forget_me():
    engine = AIMemoryEngine(gemini_provider=AsyncMock())
    # Manually save something to test deletion
    await engine._save_memory(123, {"name": "Majid"})
    await engine.forget_user(123)
    context = await engine.get_context(123)
    assert context == ""


def test_lazy_provider_is_not_built_on_construction():
    engine = AIMemoryEngine()
    assert engine._gemini is None


def test_parse_extraction_tolerates_noise():
    assert parse_extraction('Sure! {"name": "A", "interests": ["x"]} done') == {
        "name": "A",
        "interests": ["x"],
    }
    assert parse_extraction("no json here") == {}
    assert parse_extraction("{not json}") == {}
    assert parse_extraction("[1, 2]") == {}
