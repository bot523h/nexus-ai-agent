from datetime import timezone

import pytest
from sqlmodel import select

from nexus_ai_agent.storage.control_plane_models import (
    AgentSession,
    Job,
    JobPriority,
    JobStatus,
    LogLevel,
    Node,
    NodeStatus,
    SessionStatus,
    SystemLog,
)
from nexus_ai_agent.storage.db import get_session


@pytest.mark.asyncio
async def test_control_plane_tables_and_defaults(tmp_path) -> None:
    db_path = str(tmp_path / "control-plane.sqlite")
    async with get_session(db_path) as session:
        node = Node(node_key="node-1", name="Primary")
        job = Job(
            job_key="job-1",
            kind="health_check",
            node_id=None,
            status=JobStatus.QUEUED,
            priority=JobPriority.HIGH,
        )
        agent_session = AgentSession(session_key="session-1", user_id=42)
        log = SystemLog(event="node.created", message="Node registered")
        session.add_all([node, job, agent_session, log])
        assert node.created_at.tzinfo is timezone.utc
        await session.commit()
        await session.refresh(node)
        await session.refresh(job)

        assert node.id is not None
        assert job.id is not None
        assert node.status is NodeStatus.PROVISIONING
        assert agent_session.status is SessionStatus.ACTIVE
        assert log.level is LogLevel.INFO
        # SQLite stores timezone-aware datetimes as naive values on reload;
        # the in-memory default above remains explicitly UTC-aware.

    async with get_session(db_path) as session:
        assert len((await session.execute(select(Node))).scalars().all()) == 1
        assert len((await session.execute(select(Job))).scalars().all()) == 1
        assert len((await session.execute(select(AgentSession))).scalars().all()) == 1
        assert len((await session.execute(select(SystemLog))).scalars().all()) == 1
