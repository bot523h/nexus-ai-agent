import pytest
from nexus_ai_agent.agent.approval import ApprovalSystem

@pytest.mark.asyncio
async def test_approve_status():
    system = ApprovalSystem()
    approval_id = await system.request_approval("test_change", "test_desc")
    await system.approve(approval_id)
    status = await system.get_status(approval_id)
    assert status == "approved"
