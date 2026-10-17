"""A-02 on a live PostgreSQL: one database per deployment, both engines.

``tests/unit/test_sync_engine_resolution.py`` proves *which* URL every feature
module resolves to without a server; the 2026-10-15 remediation (D-0027,
law R19) recorded that no live PostgreSQL run had been performed.  This test
runs only where ``NEXUS_DATABASE_URL`` is configured (CI job
``migrate-postgres``, or a local server) and proves, on the real backend:

* the async path (``get_session`` — users, chats, files, dashboard) and the
  sync path (``sync_engine`` — XP, moderation, force-join, referrals …) talk
  to the *same* PostgreSQL database: a row written through one is read back
  through the other;
* the sync feature engines really run their statements on PostgreSQL —
  ``ForceJoinManager.should_block`` flips with the ``enabled IS true``
  predicate the column expression compiles to on this dialect;
* the ``ReferralEngine`` creates and uses its tables on PostgreSQL;
* no engine in the process is SQLite;
* the ids used are a post-2021 user id (> 2**31) and a supergroup id
  (``-100…``) — the values that overflowed PostgreSQL's INTEGER before
  migration ``b5d8e2f4a917`` (audit A-17).

Data-safety contract (review finding, 2026-10-17)
-------------------------------------------------

An earlier revision of this module opened its force-join test with an
**unscoped** delete::

    DELETE FROM forcejoinconfig WHERE enabled IS true

``NEXUS_DATABASE_URL`` is the very variable an operator sets to the real
(possibly Neon, possibly shared) database, and the module docstring offered
"a local server" as a supported target — so running the suite against a
development database permanently deleted *every other chat's* enabled
force-join configuration. That statement also contradicted the claim below
that rows are keyed by a per-run nonce.

The module is now safe by construction, not by convention:

* **Explicit opt-in.** ``NEXUS_DATABASE_URL`` alone no longer enables it;
  ``NEXUS_PG_TEST_DESTRUCTIVE=1`` must be set as well. The CI
  ``migrate-postgres`` job sets it against its disposable service container
  (``tests/unit/test_pg_integration_safety.py`` enforces that).
* **Every write is scoped to a per-run nonce.** No statement in this
  module modifies or deletes a row it did not create; the same unit test
  parses the module and fails on any ``DELETE`` without a nonce-scoped
  ``WHERE``. Read-only queries *may* inspect pre-existing rows — the
  survival assertion below needs that baseline.
* **The assertion was redesigned.** The old first assertion needed a
  *globally empty* table. The gate answers "does any enabled configuration
  exist", so the test now compares its verdict with the table's own state
  instead of emptying the table to make the comparison trivial.
* **Survival is asserted, not assumed.** A module-scoped fixture snapshots
  the set of enabled configurations before the module runs and proves after
  it that none of them disappeared, and a dedicated test drives the gate with
  a foreign enabled configuration and asserts that configuration survives.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import text
from sqlmodel import Session, select

from nexus_ai_agent.config import settings as settings_module
from nexus_ai_agent.storage import db as db_module
from nexus_ai_agent.storage.db import get_session, sync_engine
from nexus_ai_agent.storage.models import Chat, ForceJoinConfig, User

#: The second, explicit opt-in. ``NEXUS_DATABASE_URL`` alone points the whole
#: application at a database — including a real one — so it must not by itself
#: arm a module that writes to that database.
DESTRUCTIVE_OPT_IN = "NEXUS_PG_TEST_DESTRUCTIVE"

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not os.getenv("NEXUS_DATABASE_URL"),
        reason="requires PostgreSQL (NEXUS_DATABASE_URL)",
    ),
    pytest.mark.skipif(
        os.getenv(DESTRUCTIVE_OPT_IN) != "1",
        reason=(
            f"refuses to write to NEXUS_DATABASE_URL without {DESTRUCTIVE_OPT_IN}=1 "
            "(the CI migrate-postgres job sets it against its disposable service "
            "container)"
        ),
    ),
]

# The opt-in must never turn into a silent skip. If it is armed but no database
# is configured, every test below would collect as "skipped" and the build would
# report green while the PostgreSQL contract — the reason this module exists —
# went unverified. A misconfigured run is therefore a *red* build.
if os.getenv(DESTRUCTIVE_OPT_IN) == "1" and not os.getenv("NEXUS_DATABASE_URL"):
    pytest.fail(
        f"{DESTRUCTIVE_OPT_IN}=1 is set but NEXUS_DATABASE_URL is not: this module "
        "would collect as skipped, so the PostgreSQL contract it exists to verify "
        "would go unverified while the build stayed green. Set both variables, or "
        "neither.",
        pytrace=False,
    )


# ── scoped data access (no statement below is ever unscoped) ────────────────


def _enabled_force_join_chat_ids(engine: Any) -> set[int]:
    """Every chat id that currently has an *enabled* force-join configuration."""
    with Session(engine) as session:
        return set(
            session.execute(
                text("SELECT chat_id FROM forcejoinconfig WHERE enabled IS true")
            ).scalars()
        )


def _delete_force_join_rows(engine: Any, chat_ids: list[int]) -> None:
    """Remove only the ``forcejoinconfig`` rows of *chat_ids*."""
    with engine.begin() as conn:
        for chat_id in chat_ids:
            conn.execute(text("DELETE FROM forcejoinconfig WHERE chat_id = :id"), {"id": chat_id})


def _delete_run_rows(engine: Any, *, chat_ids: list[int], telegram_id: int) -> None:
    """Remove exactly the rows this run created — success path *and* failure path."""
    _delete_force_join_rows(engine, chat_ids)
    with engine.begin() as conn:
        for chat_id in chat_ids:
            conn.execute(text("DELETE FROM chat WHERE chat_id = :id"), {"id": chat_id})
        conn.execute(text('DELETE FROM "user" WHERE telegram_id = :id'), {"id": telegram_id})


@pytest.fixture(scope="module", autouse=True)
def unrelated_configurations_survive() -> Iterator[None]:
    """No test in this module may delete another chat's enabled configuration.

    Module-scoped and autouse: the snapshot is taken before the first test and
    re-checked after the last one, so every statement the module runs —
    including the force-join test that used to clear the whole table — is
    proven harmless to configurations it did not create.
    """
    before = _enabled_force_join_chat_ids(sync_engine())
    yield
    after = _enabled_force_join_chat_ids(sync_engine())
    assert before <= after, (
        "this module deleted an enabled force-join configuration it did not create: "
        f"{sorted(before - after)}"
    )


@pytest.fixture()
def pg(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
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
    # A second, *foreign* chat id: rows under it stand in for another chat's
    # data, which no statement in this module is allowed to remove.
    foreign_chat_id = chat_id - 1
    try:
        yield SimpleNamespace(nonce=nonce, chat_id=chat_id, foreign_chat_id=foreign_chat_id)
    finally:
        # ``finally``, not a bare teardown: the rows this run created are
        # removed whether the test passed, failed or errored. Only the two
        # nonce-scoped chat ids and the nonce user id are named.
        _delete_run_rows(sync_engine(), chat_ids=[chat_id, foreign_chat_id], telegram_id=nonce)
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
    """``enabled IS true`` really evaluates on PostgreSQL — table left intact.

    The gate is *global*: ``should_block`` is armed while any chat has an
    enabled configuration and the (unbound) manager cannot confirm
    membership. The old test proved the negative case by deleting every
    enabled row in the database; this one compares the verdict with the
    table's own state and only ever adds/removes the two rows it created.
    """
    from nexus_ai_agent.features.force_join import ForceJoinManager

    manager = ForceJoinManager()  # unbound: membership can never be confirmed
    engine = sync_engine()

    # Enabled configurations that predate this test — including, on a shared
    # database, real ones belonging to other chats. They are read only.
    pre_existing = _enabled_force_join_chat_ids(engine) - {pg.chat_id, pg.foreign_chat_id}

    # Our probe row: this is the row the test owns and toggles.
    with Session(engine) as session:
        session.add(
            ForceJoinConfig(chat_id=pg.chat_id, enabled=True, channel_username="@nexus_pg_probe")
        )
        session.commit()
    assert await manager.should_block(pg.nonce, "") is True, "an enabled row arms the gate"

    # Disabling *our* row returns the verdict to the pre-existing baseline …
    with Session(engine) as session:
        cfg = session.exec(
            select(ForceJoinConfig).where(ForceJoinConfig.chat_id == pg.chat_id)
        ).one()
        cfg.enabled = False
        session.add(cfg)
        session.commit()
    assert await manager.should_block(pg.nonce, "") is bool(pre_existing), (
        "the verdict tracks the table, not this chat's row"
    )

    # … and removing it entirely changes nothing either: whatever the answer
    # is, it is decided by rows this test never wrote.
    _delete_force_join_rows(engine, [pg.chat_id])
    assert await manager.should_block(pg.nonce, "") is bool(pre_existing)

    # The negative case, scoped: with a second enabled row of our own the gate
    # arms again, and with both of ours gone it is back to the baseline. On
    # the disposable CI database (no pre-existing rows) that baseline is
    # ``False`` — exactly the original "no enabled row → gate is inert"
    # assertion, now without the table-wide delete.
    with Session(engine) as session:
        session.add(
            ForceJoinConfig(
                chat_id=pg.foreign_chat_id, enabled=True, channel_username="@nexus_pg_probe_2"
            )
        )
        session.commit()
    assert await manager.should_block(pg.nonce, "") is True
    _delete_force_join_rows(engine, [pg.foreign_chat_id])
    assert await manager.should_block(pg.nonce, "") is bool(pre_existing)


async def test_an_unrelated_enabled_force_join_configuration_survives_the_gate(
    pg: Any,
) -> None:
    """The regression witness for the table-wide ``DELETE`` (2026-10-17).

    A configuration belonging to a *different* chat is created here, the gate
    is driven against it as the old test drove it, and the row is then proven
    untouched — enabled, same channel, same chat id. The old module deleted
    it in its first statement.
    """
    from nexus_ai_agent.features.force_join import ForceJoinManager

    manager = ForceJoinManager()
    engine = sync_engine()
    with Session(engine) as session:
        session.add(
            ForceJoinConfig(
                chat_id=pg.foreign_chat_id,
                enabled=True,
                channel_username="@nexus_pg_foreign",
            )
        )
        session.commit()

    # The predicate finds the foreign row on PostgreSQL and, with no bot
    # bound, the gate fails closed.
    assert await manager.should_block(pg.nonce, "") is True

    assert pg.foreign_chat_id in _enabled_force_join_chat_ids(engine)
    with Session(engine) as session:
        survivor = session.exec(
            select(ForceJoinConfig).where(ForceJoinConfig.chat_id == pg.foreign_chat_id)
        ).one()
    assert survivor.enabled is True
    assert survivor.channel_username == "@nexus_pg_foreign"
    assert survivor.chat_id == pg.foreign_chat_id


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
