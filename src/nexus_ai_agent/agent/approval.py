from typing import Dict, Any, Callable
import asyncio
import logging

logger = logging.getLogger(__name__)

class ApprovalSystem:
    def __init__(self):
        self.pending_actions: Dict[str, Any] = {}

    async def request_approval(self, action_id: str, description: str, on_approve: Callable):
        """Request owner approval for an action."""
        self.pending_actions[action_id] = {
            "description": description,
            "callback": on_approve,
            "timestamp": asyncio.get_event_loop().time()
        }
        # In a real bot, this would send a message to the owner
        logger.info(f"Approval requested for: {description}")

    async def approve(self, action_id: str):
        if action_id in self.pending_actions:
            action = self.pending_actions.pop(action_id)
            await action["callback"]()
            return True
        return False
