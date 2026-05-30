import pytest
from unittest.mock import AsyncMock, MagicMock
from nexus_ai_agent.agents.store.agent_manager import AgentManager

@pytest.mark.asyncio
async def test_list_agents():
    agents = AgentManager.get_all_agents()
    assert len(agents) >= 10
    assert "coding" in agents

@pytest.mark.asyncio
async def test_activate_agent():
    user_id = 12345
    await AgentManager.activate(user_id, "coding")
    active = await AgentManager.get_active_agent_name(user_id)
    assert active == "coding"

@pytest.mark.asyncio
async def test_deactivate_agent():
    user_id = 12345
    await AgentManager.activate(user_id, "coding")
    await AgentManager.deactivate(user_id)
    active = await AgentManager.get_active_agent_name(user_id)
    assert active is None
