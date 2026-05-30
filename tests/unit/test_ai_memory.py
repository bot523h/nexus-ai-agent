import pytest
from nexus_ai_agent.features.ai_memory import AIMemoryEngine

@pytest.mark.asyncio
async def test_extract_name():
    engine = AIMemoryEngine()
    # Mocking Gemini response for extraction
    engine.gemini = AsyncMock()
    engine.gemini.generate.return_value = '{"name": "Majid"}'
    
    name = await engine.extract_name(123, "My name is Majid")
    assert name == "Majid"

@pytest.mark.asyncio
async def test_forget_me():
    engine = AIMemoryEngine()
    await engine.store_memory(123, name="Majid")
    await engine.forget_user(123)
    memory = await engine.get_memory(123)
    assert memory.name is None

class AsyncMock(MagicMock):
    async def __call__(self, *args, **kwargs):
        return super(AsyncMock, self).__call__(*args, **kwargs)
