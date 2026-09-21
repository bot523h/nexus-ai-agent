"""Force-join gate tests (P0-3).

Two independent defects made the "anti-bypass" gate a no-op:

1. ``check_membership`` answered ``True`` when no bot instance was bound, and
   the handler built ``ForceJoinManager()`` without one — so every visitor was
   a "member".
2. ``should_block`` filtered with ``ForceJoinConfig.enabled is True``, a Python
   identity test that SQLAlchemy compiled to ``WHERE 0`` — so the query never
   matched and the gate concluded "force-join is not enabled anywhere".

Both are pinned here against a real SQLite table.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlmodel import SQLModel

from nexus_ai_agent.config import settings as settings_module
from nexus_ai_agent.features import force_join as fj
from nexus_ai_agent.features.force_join import DEFAULT_CHANNEL, ForceJoinManager
from nexus_ai_agent.storage.models import ForceJoinConfig


class _Member:
    def __init__(self, status: str) -> None:
        self.status = status


class _FakeBot:
    """Minimal Telegram bot stand-in for membership lookups."""

    def __init__(self, status: str = "member") -> None:
        self.status = status
        self.calls: list[tuple[str, int]] = []

    async def get_chat_member(self, chat_id: str, user_id: int) -> _Member:
        self.calls.append((chat_id, user_id))
        if self.status == "raise":
            raise RuntimeError("bot was kicked from the channel")
        return _Member(self.status)


@pytest.fixture(autouse=True)
def _clean_cache() -> Any:
    fj._membership_cache.clear()
    yield
    fj._membership_cache.clear()


@pytest.fixture()
def _db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    db_path = tmp_path / "app.sqlite"
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("NEXUS_DB_PATH", str(db_path))
    settings_module.get_settings.cache_clear()
    engine = create_engine(f"sqlite:///{db_path}")
    SQLModel.metadata.create_all(engine, tables=[ForceJoinConfig.__table__])
    engine.dispose()
    yield db_path
    settings_module.get_settings.cache_clear()


@pytest.mark.asyncio
async def test_membership_without_a_bot_fails_closed(_db: Path) -> None:
    """The old behaviour returned True here — the gate was wide open."""
    manager = ForceJoinManager()
    assert manager.bot is None
    assert await manager.check_membership(1) is False


@pytest.mark.asyncio
async def test_membership_follows_the_real_telegram_answer(_db: Path) -> None:
    manager = ForceJoinManager(bot=_FakeBot("member"))
    assert await manager.check_membership(1) is True

    fj._membership_cache.clear()
    left = ForceJoinManager(bot=_FakeBot("left"))
    assert await left.check_membership(1) is False


@pytest.mark.asyncio
async def test_membership_fails_closed_when_the_api_call_raises(_db: Path) -> None:
    manager = ForceJoinManager(bot=_FakeBot("raise"))
    assert await manager.check_membership(1) is False


@pytest.mark.asyncio
async def test_should_block_is_false_when_no_chat_enabled_force_join(_db: Path) -> None:
    manager = ForceJoinManager(bot=_FakeBot("left"))
    assert await manager.should_block(1, command="ai") is False


@pytest.mark.asyncio
async def test_should_block_blocks_a_non_member_once_enabled(_db: Path) -> None:
    """Regression for the ``enabled is True`` query bug: this used to be False."""
    ForceJoinManager.set_config(-100123, enabled=True, channel_username="@nexus_ai_official")
    bot = _FakeBot("left")
    manager = ForceJoinManager(bot=bot)
    assert await manager.should_block(1, command="ai") is True
    assert bot.calls == [(DEFAULT_CHANNEL, 1)]


@pytest.mark.asyncio
async def test_should_block_lets_members_through(_db: Path) -> None:
    ForceJoinManager.set_config(-100123, enabled=True)
    manager = ForceJoinManager(bot=_FakeBot("administrator"))
    assert await manager.should_block(1, command="ai") is False


@pytest.mark.asyncio
async def test_public_commands_are_never_blocked(_db: Path) -> None:
    ForceJoinManager.set_config(-100123, enabled=True)
    manager = ForceJoinManager(bot=_FakeBot("left"))
    for command in ("start", "help", "forcejoin_status"):
        assert await manager.should_block(1, command=command) is False


@pytest.mark.asyncio
async def test_invalidate_cache_forces_a_fresh_check(_db: Path) -> None:
    bot = _FakeBot("left")
    manager = ForceJoinManager(bot=bot)
    assert await manager.check_membership(1) is False
    assert await manager.check_membership(1) is False
    assert len(bot.calls) == 1  # served from cache
    manager.invalidate_cache(1)
    assert await manager.check_membership(1) is False
    assert len(bot.calls) == 2
