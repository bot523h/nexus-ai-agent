"""Schema authority: the Alembic chain is the only writer of the schema.

Audit 2026-10-15 (remediation closeout) found two runtime schema owners next to
the migration chain:

* ``features.conversation_store.ConversationStore`` declared
  ``conversation_history`` on a private ``MetaData`` and ran ``create_all()``
  in its constructor — invisible to ``alembic check``;
* ``features.referral.ReferralEngine`` ran ``create_all()`` for two tables that
  migration ``47903d282ede`` already owns.

The async SQLite bootstrap (``storage.db.create_all_tables``) also ran
``create_all`` against files Alembic had already versioned, which would have
silently materialised a model table with no migration.

These tests pin the new contract:

1. no production module outside ``storage/db.py`` and ``storage/migrations.py``
   emits ``create_all`` (AST scan, not grep);
2. ``conversation_history`` is a ``SQLModel`` table whose DDL is byte-for-byte
   what the old store emitted (so revision ``c7a1e5d2b9f3`` adopts, never
   rebuilds), and the revision preserves existing rows;
3. the stores never create tables themselves: on an un-migrated file they fail
   loudly, on a migrated file they work;
4. ``create_all_tables`` skips a file that carries ``alembic_version``.
"""

from __future__ import annotations

import ast
import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import (
    Column,
    DateTime,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    func,
    inspect,
)
from sqlalchemy.exc import OperationalError
from sqlalchemy.schema import CreateIndex, CreateTable
from sqlmodel import SQLModel

from nexus_ai_agent.config import settings as settings_module
from nexus_ai_agent.features.conversation_store import ConversationStore
from nexus_ai_agent.features.referral import ReferralEngine
from nexus_ai_agent.storage import db as db_module
from nexus_ai_agent.storage.db import get_session
from nexus_ai_agent.storage.models import ConversationHistory

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src" / "nexus_ai_agent"
PREVIOUS_HEAD = "b5d8e2f4a917"
HEAD = "c7a1e5d2b9f3"

#: Verbatim copy of the table the old ``ConversationStore`` created at runtime
#: (``features/conversation_store.py`` before the closeout).  It is the
#: contract an existing deployment's table satisfies.
_LEGACY = MetaData()
_LEGACY_TABLE = Table(
    "conversation_history",
    _LEGACY,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("conv_id", String, nullable=False),
    Column("role", String, nullable=False),
    Column("parts_json", Text, nullable=False),
    Column("created_at", DateTime, server_default=func.current_timestamp()),
    Index("ix_conv_history_conv_id", "conv_id"),
)

# Only the schema bring-up itself may reference ``MetaData.create_all``:
#   storage/db.py — the un-versioned SQLite bootstrap (D6), also used by the
#   legacy adoption in storage/migrations.py *before* ``alembic stamp head``.
_CREATE_ALL_ALLOWED = {"storage/db.py"}


def _create_all_references() -> set[str]:
    """Every module that names ``.create_all`` — as a call *or* as a reference
    (``conn.run_sync(metadata.create_all)`` is how the bootstrap invokes it)."""
    found: set[str] = set()
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "create_all":
                found.add(path.relative_to(SRC).as_posix())
    return found


def test_only_the_schema_bootstrap_may_reference_create_all() -> None:
    assert _create_all_references() == _CREATE_ALL_ALLOWED


def _ddl(table: Table, dialect: str) -> list[str]:
    from sqlalchemy.dialects import postgresql, sqlite

    impl = {"sqlite": sqlite.dialect(), "postgresql": postgresql.dialect()}[dialect]
    statements = [str(CreateTable(table).compile(dialect=impl)).strip()]
    statements += [str(CreateIndex(index).compile(dialect=impl)).strip() for index in table.indexes]
    return statements


@pytest.mark.parametrize("dialect", ["sqlite", "postgresql"])
def test_conversation_history_model_matches_the_legacy_runtime_ddl(dialect: str) -> None:
    """Adopt-in-place is only safe if the owned DDL *is* the legacy DDL."""
    model_table = SQLModel.metadata.tables["conversation_history"]
    assert model_table is ConversationHistory.__table__
    assert _ddl(model_table, dialect) == _ddl(_LEGACY_TABLE, dialect)


def _config() -> Config:
    config = Config(str(REPO / "alembic.ini"))
    config.set_main_option("script_location", str(REPO / "migrations"))
    return config


@pytest.fixture()
def sqlite_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    path = tmp_path / "authority.sqlite"
    monkeypatch.setenv("NEXUS_DB_PATH", str(path))
    monkeypatch.delenv("NEXUS_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    settings_module.get_settings.cache_clear()
    db_module._sync_engine_url = None  # type: ignore[attr-defined]
    db_module._sync_engine_instance = None  # type: ignore[attr-defined]
    yield str(path)
    settings_module.get_settings.cache_clear()


def test_revision_adopts_an_existing_table_and_preserves_its_rows(sqlite_url: str) -> None:
    """A deployment that already has the table (every one where the store ran)."""
    config = _config()
    command.upgrade(config, PREVIOUS_HEAD)
    legacy_engine = create_engine(f"sqlite:///{sqlite_url}")
    _LEGACY.create_all(legacy_engine)  # exactly what the old store did at runtime
    with legacy_engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO conversation_history (conv_id, role, parts_json) VALUES "
            "('tg:1', 'user', '[{\"text\": \"before\"}]'), "
            "('tg:1', 'model', '[{\"text\": \"the\"}]')"
        )
    legacy_engine.dispose()

    command.upgrade(config, HEAD)

    engine = create_engine(f"sqlite:///{sqlite_url}")
    try:
        inspector = inspect(engine)
        assert "conversation_history" in inspector.get_table_names()
        assert {ix["name"] for ix in inspector.get_indexes("conversation_history")} == {
            "ix_conv_history_conv_id"
        }
        with engine.connect() as conn:
            rows = conn.exec_driver_sql(
                "SELECT conv_id, role, parts_json FROM conversation_history ORDER BY id"
            ).fetchall()
        assert [tuple(r) for r in rows] == [
            ("tg:1", "user", '[{"text": "before"}]'),
            ("tg:1", "model", '[{"text": "the"}]'),
        ]
    finally:
        engine.dispose()
    command.check(config)  # the adopted table is exactly the owned one: zero drift


def test_revision_creates_the_table_on_a_fresh_database_and_downgrade_removes_it(
    sqlite_url: str,
) -> None:
    config = _config()
    command.upgrade(config, HEAD)
    engine = create_engine(f"sqlite:///{sqlite_url}")
    try:
        assert "conversation_history" in inspect(engine).get_table_names()
        command.downgrade(config, PREVIOUS_HEAD)
        assert "conversation_history" not in inspect(engine).get_table_names()
        command.upgrade(config, HEAD)
        assert "conversation_history" in inspect(engine).get_table_names()
    finally:
        engine.dispose()


def test_stores_never_create_their_own_tables(tmp_path: Path) -> None:
    """On an un-migrated file the stores fail loudly instead of emitting DDL."""
    bare = tmp_path / "bare.sqlite"
    store = ConversationStore(db_path=str(bare))
    referral = ReferralEngine(db_path=str(bare))
    assert inspect(store._engine).get_table_names() == []  # constructors touched nothing

    with pytest.raises(OperationalError, match="no such table: conversation_history"):
        store.append("tg:1", {"role": "user", "parts": [{"text": "hi"}]})
    with pytest.raises(OperationalError, match="no such table: referralcode"):
        referral.get_or_create_code(user_id=5_000_000_001)
    assert inspect(store._engine).get_table_names() == []


def test_stores_work_on_a_file_migrated_by_the_production_bring_up(migrated_sqlite: str) -> None:
    store = ConversationStore(db_path=migrated_sqlite)
    store.append("tg:1", {"role": "user", "parts": [{"text": "hi"}]})
    assert store.get_history("tg:1") == [{"role": "user", "parts": [{"text": "hi"}]}]
    code = ReferralEngine(db_path=migrated_sqlite).get_or_create_code(user_id=5_000_000_001)
    assert code.startswith("NEXUS-")


async def test_create_all_tables_skips_a_file_the_migration_chain_owns(
    migrated_sqlite: str,
) -> None:
    """A versioned file is Alembic's: dropping an owned table must *not* be
    silently repaired by the async bootstrap (that repair is how a missing
    migration would stay hidden until the first PostgreSQL deployment)."""
    with sqlite3.connect(migrated_sqlite) as conn:
        conn.execute("DROP TABLE userxp")
    async with get_session(migrated_sqlite) as session:
        assert session is not None
    with sqlite3.connect(migrated_sqlite) as conn:
        names = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert "alembic_version" in names
    assert "userxp" not in names, "create_all ran against an Alembic-owned file"


async def test_create_all_tables_still_bootstraps_an_unversioned_file(tmp_path: Path) -> None:
    """The D6 behaviour for bare dev/test files (no ``alembic_version``) is unchanged."""
    bare = tmp_path / "bare.sqlite"
    async with get_session(str(bare)) as session:
        assert session is not None
    with sqlite3.connect(bare) as conn:
        names = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert "alembic_version" not in names
    assert {"userxp", "user", "chat", "conversation_history"} <= names
