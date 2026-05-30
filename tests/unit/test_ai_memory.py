import pytest
from unittest.mock import AsyncMock
from nexus_ai_agent.features.ai_memory import AIMemoryEngine

@pytest.mark.asyncio
async def test_extract_context():
    # Mocking Gemini provider
    mock_gemini = AsyncMock()
    # Should return a JSON string
    mock_gemini.generate.return_value = '{"name": "Majid", "occupation": "Developer"}'
    
    engine = AIMemoryEngine(gemini_provider=mock_gemini)
    await engine.update_from_message(123, "My name is Majid and I am a developer")
    
    context = await engine.get_context(123)
    assert "Majid" in context
    assert "Developer" in context

@pytest.mark.asyncio
async def test_forget_me():
    engine = AIMemoryEngine()
    # Manually save something to test deletion
    await engine._save_memory(123, {"name": "Majid"})
    await engine.forget_user(123)
    context = await engine.get_context(123)
    assert context == ""
