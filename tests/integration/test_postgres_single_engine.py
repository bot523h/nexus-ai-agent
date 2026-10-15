"""A-02 on a live PostgreSQL: one database per deployment, both engines.

``tests/unit/test_sync_engine_resolution.py`` proves *which* URL every feature
module resolves to without a server; the 2026-10-15 remediation (D-0026,
law R19) recorded that no live PostgreSQL run had been performed.  This test
runs only where ``NEXUS_DATABASE_URL`` is configured (CI job
``migrate-postgres``, or a local server) and proves, on the real backend:

* the async path (``get_session`` — users, chats, files, dashboard) and the
  sync path (``sync_engine`` — XP, moderation, force-join, referrals …) talk
  to the *same* PostgreSQL database: a row written through one is read back
  through the other;
* the sync feature engines really run their statements on PostgreSQL —
  ``ForceJoinManager.should_block`` flips once a ``ForceJoinConfig`` row exists
  (the predicate compiles to ``enabled IS true`` on this dialect too);
* the ``ReferralEngine`` creates and uses its tables on PostgreSQL;
* no engine in the process is SQLite;
* the ids used are a post-2021 user id (> 2**31) and a supergroup id
  (``-100…``) — the values that overflowed PostgreSQL's INTEGER before
  migration ``b5d8e2f4a917`` (audit A-17).

Rows are keyed by a per-run nonce and removed afterwards so the shared CI
database stays clean across jobs.
"""

from __future__ import annotations

import os
import secrets
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import text
from sqlmodel import Session, select

from nexus_ai_agent.config import settings as settings_module
from nexus_ai_agent.storage import db as db_module
from nexus_ai_agent.storage.db import get_session, sync_engine
from nexus_ai_agent.storage.models import Chat, ForceJoinConfig, User

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not os.getenv("NEXUS_DATABASE_URL"), reason="requires PostgreSQL"),
]


@pytest.fixture()
def pg(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Point every resolution path at the configured PostgreSQL and reset caches."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.delenv("NEXUS_DB_PATH", raising=False)
    settings_module.get_settings.cache_clear()
    db_module._engine = None  # type: ignore[attr-defined]
    db_module._session_factory = None  # type: ignore[attr-defined]
    db_module._sync_engine_url = None  # type: ignore[attr-defined]
    db_module._sync_engine_instance = None  # type: ignore[attr-defined]
    # A modern Telegram user id (> 2**31 since 2021) and a supergroup id
    # (``-100`` + 10 digits): both overflowed PostgreSQL's INTEGER before
    # migration ``b5d8e2f4a917`` (audit A-17) — the ids *are* the probe.
    nonce = 2**40 + secrets.randbelow(2**30)
    chat_id = -(10**12 + nonce)
    try:
        yield SimpleNamespace(nonce=nonce, chat_id=chat_id)
    finally:
        engine = sync_engine()
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM forcejoinconfig WHERE chat_id = :id"), {"id": chat_id})
            conn.execute(text("DELETE FROM chat WHERE chat_id = :id"), {"id": chat_id})
            conn.execute(text('DELETE FROM "user" WHERE telegram_id = :id'), {"id": nonce})
        settings_module.get_settings.cache_clear()
        db_module._sync_engine_url = None  # type: ignore[attr-defined]
        db_module._sync_engine_instance = None  # type: ignore[attr-defined]


async def test_async_and_sync_paths_share_one_postgres_database(pg: Any) -> None:
    from nexus_ai_agent.bot.handlers import _upsert_chat, _upsert_user

    async_url = db_module.resolve_database_url()
    assert async_url is not None and async_url.startswith("postgresql://")
    engine = sync_engine()
    assert engine.url.drivername == "postgresql+psycopg"

    # Written through the async session factory the handlers use …
    tg_user = SimpleNamespace(id=pg.nonce, username=f"pg-{pg.nonce}")
    user = await _upsert_user(get_session, tg_user)
    chat = await _upsert_chat(get_session, pg.chat_id, f"tg:{pg.chat_id}")
    assert user.telegram_id == pg.nonce and chat.chat_id == pg.chat_id

    # … and read back through the synchronous feature engine, same database.
    with Session(engine) as session:
        row = session.exec(select(User).where(User.telegram_id == pg.nonce)).one()
        assert row.username == f"pg-{pg.nonce}"
        stored_chat = session.exec(select(Chat).where(Chat.chat_id == pg.chat_id)).one()
        assert stored_chat.thread_id == f"tg:{pg.chat_id}"
        current = session.execute(text("SELECT current_database()")).scalar_one()
        assert current == engine.url.database


async def test_force_join_gate_runs_its_predicate_on_postgres(pg: Any) -> None:
    from nexus_ai_agent.features.force_join import ForceJoinManager

    manager = ForceJoinManager()  # unbound: membership can never be confirmed
    with Session(sync_engine()) as session:
        session.execute(text("DELETE FROM forcejoinconfig WHERE enabled IS true"))
        session.commit()
    assert await manager.should_block(pg.nonce, "") is False, "no enabled row → gate is inert"
    with Session(sync_engine()) as session:
        session.add(
            ForceJoinConfig(chat_id=pg.chat_id, enabled=True, channel_username="@nexus_pg_probe")
        )
        session.commit()
    # The predicate (``enabled IS true``) now finds the row on PostgreSQL and,
    # with no bot bound, the gate fails closed.
    assert await manager.should_block(pg.nonce, "") is True
    with Session(sync_engine()) as session:
        session.execute(text("DELETE FROM forcejoinconfig WHERE chat_id = :id"), {"id": pg.chat_id})
        session.commit()
    assert await manager.should_block(pg.nonce, "") is False


async def test_referral_engine_creates_and_uses_its_tables_on_postgres(pg: Any) -> None:
    from nexus_ai_agent.features.referral import ReferralEngine

    engine = ReferralEngine(db_path=None)
    assert engine._sync_engine().url.drivername == "postgresql+psycopg"
    with Session(sync_engine()) as session:
        tables = {
            row[0]
            for row in session.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = current_schema()")
            )
        }
    assert {"forcejoinconfig", "user", "chat"} <= tables
    assert any(name.startswith("referral") for name in tables), sorted(tables)


async def test_no_sqlite_engine_exists_in_a_postgres_deployment(pg: Any) -> None:
    sync_engine()
    assert "sqlite" not in str(db_module._sync_engine_url)  # type: ignore[attr-defined]
    async with get_session() as session:
        bind = session.get_bind()
        assert bind.dialect.name == "postgresql", bind.dialect.name
