from pathlib import Path

import pytest
from sqlmodel import select

from nexus_ai_agent.storage.db import get_session
from nexus_ai_agent.storage.models import PendingApproval


@pytest.mark.asyncio
async def test_session_initializes_tables(tmp_path: Path) -> None:
    db_path = tmp_path / "first.sqlite"

    async with get_session(str(db_path)) as session:
        session.add(PendingApproval(change_type="test", description="bootstrap"))
        await session.commit()

    async with get_session(str(db_path)) as session:
        approvals = (await session.execute(select(PendingApproval))).scalars().all()
        assert len(approvals) == 1


@pytest.mark.asyncio
async def test_session_switches_database_paths(tmp_path: Path) -> None:
    first_path = tmp_path / "first.sqlite"
    second_path = tmp_path / "second.sqlite"

    async with get_session(str(first_path)) as session:
        session.add(PendingApproval(change_type="first", description="first database"))
        await session.commit()

    async with get_session(str(second_path)) as session:
        approvals = (await session.execute(select(PendingApproval))).scalars().all()
        assert approvals == []

    assert first_path.exists()
    assert second_path.exists()
