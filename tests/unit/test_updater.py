"""Approval-gate tests for AutoUpdater.do_update (Phase 0).

The update (git pull + pip install .) must not run unless the owner has
approved a ``self_update`` request via the existing /approve flow.
"""

from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlmodel import select

from nexus_ai_agent.agent.approval import ApprovalSystem
from nexus_ai_agent.agent.updater import AutoUpdater
from nexus_ai_agent.storage.db import get_session as real_get_session
from nexus_ai_agent.storage.models import PendingApproval


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    """Point updater/approval ``get_session`` at a fresh sqlite file per test."""
    db_path = str(tmp_path / "app.sqlite")

    @asynccontextmanager
    async def bound_get_session():
        async with real_get_session(db_path) as session:
            yield session

    import nexus_ai_agent.agent.approval as approval_mod
    import nexus_ai_agent.agent.updater as updater_mod

    monkeypatch.setattr(updater_mod, "get_session", bound_get_session)
    monkeypatch.setattr(approval_mod, "get_session", bound_get_session)
    return bound_get_session


async def _first_pending(temp_db: Any) -> PendingApproval | None:
    async with temp_db() as session:
        result = await session.execute(
            select(PendingApproval).where(
                PendingApproval.change_type == "self_update",
                PendingApproval.status == "pending",
            )
        )
        return result.scalars().first()


@pytest.mark.asyncio
async def test_do_update_refused_without_approval(settings_override, temp_db, monkeypatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        "nexus_ai_agent.agent.updater.subprocess.run",
        lambda *args, **kwargs: calls.append(args[0]),
    )

    updater = AutoUpdater("v3.0.0")
    system = ApprovalSystem()
    ok, detail = await updater.do_update(system)

    assert ok is False
    assert "approval" in detail
    assert calls == []  # git pull / pip install must not have run

    row = await _first_pending(temp_db)
    assert row is not None
    assert row.status == "pending"
    assert row.change_type == "self_update"


@pytest.mark.asyncio
async def test_do_update_proceeds_after_owner_approval(
    settings_override, temp_db, monkeypatch
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        "nexus_ai_agent.agent.updater.subprocess.run",
        lambda *args, **kwargs: calls.append(args[0]),
    )

    updater = AutoUpdater("v3.0.0")
    system = ApprovalSystem()
    ok, _ = await updater.do_update(system)
    assert ok is False

    row = await _first_pending(temp_db)
    assert row is not None and row.id is not None
    assert await system.approve(row.id) is True

    ok, detail = await updater.do_update(system)
    assert ok is True
    assert detail == "update completed"
    assert calls == [["git", "pull"], [sys.executable, "-m", "pip", "install", "."]]


@pytest.mark.asyncio
async def test_do_update_without_approval_system_runs_directly(
    settings_override, temp_db, monkeypatch
) -> None:
    """approval=None is a dev/CLI path; the bot handler always passes one."""
    calls: list[list[str]] = []
    monkeypatch.setattr(
        "nexus_ai_agent.agent.updater.subprocess.run",
        lambda *args, **kwargs: calls.append(args[0]),
    )

    updater = AutoUpdater("v3.0.0")
    ok, _ = await updater.do_update(None)
    assert ok is True
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_stale_approval_requires_fresh_approval(
    settings_override, temp_db, monkeypatch
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        "nexus_ai_agent.agent.updater.subprocess.run",
        lambda *args, **kwargs: calls.append(args[0]),
    )

    updater = AutoUpdater("v3.0.0")
    system = ApprovalSystem()
    ok, _ = await updater.do_update(system)
    assert ok is False

    row = await _first_pending(temp_db)
    assert row is not None and row.id is not None
    assert await system.approve(row.id) is True

    # Age the approval beyond the 30-minute window.
    async with temp_db() as session:
        stale = await session.get(PendingApproval, row.id)
        assert stale is not None
        stale.created_at = datetime.utcnow() - timedelta(hours=1)
        session.add(stale)
        await session.commit()

    ok, detail = await updater.do_update(system)
    assert ok is False
    assert "approval" in detail
    assert calls == []
