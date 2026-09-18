from datetime import datetime

import pytest

from nexus_ai_agent.adapters.langgraph.lifecycle_recording import (
    LifecycleRecordingSaver,
    TouchCoalescer,
    access_context,
    nexus_access_context,
)


class FakeSaver:
    def get_tuple(self, config):
        return {"ok": True}


class Lifecycle:
    def __init__(self):
        self.touches = []

    async def touch_thread(self, thread_id, *, accessed_at):
        self.touches.append((thread_id, accessed_at))

    async def record_checkpoint(self, *args, **kwargs):
        pass


@pytest.mark.asyncio
async def test_touch_requires_user_context_and_is_coalesced():
    lifecycle = Lifecycle()
    saver = LifecycleRecordingSaver(FakeSaver(), lifecycle)
    assert nexus_access_context.get() == "system"
    saver.get_tuple({"configurable": {"thread_id": "t"}})
    async with access_context("user"):
        saver.get_tuple({"configurable": {"thread_id": "t"}})
        saver.get_tuple({"configurable": {"thread_id": "t"}})
    await saver.flush()
    assert [thread for thread, _ in lifecycle.touches] == ["t"]


def test_touch_coalescer_keeps_latest_timestamp():
    coalescer = TouchCoalescer()
    now = datetime.now()
    coalescer.add("t", now)
    coalescer.add("t", datetime.fromtimestamp(now.timestamp() + 1))
    assert list(coalescer.pending) == ["t"]
    assert coalescer.pending["t"].timestamp() == now.timestamp() + 1
