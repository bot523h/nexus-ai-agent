import pytest
from nexus_ai_agent.agent.approval import ApprovalSystem
from nexus_ai_agent.storage.db import get_session
from nexus_ai_agent.storage.models import PendingApproval

@pytest.mark.asyncio
async def test_approve_status():
    system = ApprovalSystem()
    approval_id = await system.request_approval("test_change", "test_desc")
    await system.approve(approval_id)
    
    async with get_session() as session:
        approval = await session.get(PendingApproval, approval_id)
        assert approval is not None
        assert approval.status == "approved"
