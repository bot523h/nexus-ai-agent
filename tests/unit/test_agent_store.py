import pytest

from nexus_ai_agent.agents.store.agent_manager import AgentManager


@pytest.mark.asyncio
async def test_list_agents():
    agents = AgentManager.list_agents()
    assert len(agents) >= 10
    # The list contains dicts with 'id'
    ids = [a["id"] for a in agents]
    assert "coding" in ids


@pytest.mark.asyncio
async def test_activate_agent():
    # Isolated unit test with no runtime bootstrap.  Pass an injected
    # provider (W1 Law 10: no accidental bypass — production wiring
    # always passes the runtime-owned provider; isolated tests pass
    # their own stub).
    from unittest.mock import MagicMock

    fake_provider = MagicMock()

    user_id = 12345
    await AgentManager.activate(user_id, "coding")
    active = await AgentManager.get_active(user_id, provider=fake_provider)
    assert active is not None
    assert "Coding" in active.name


@pytest.mark.asyncio
async def test_deactivate_agent():
    user_id = 12345
    await AgentManager.activate(user_id, "coding")
    await AgentManager.deactivate(user_id)
    active = await AgentManager.get_active(user_id)
    assert active is None
