"""Telegram identifiers are 64-bit on every backend (audit A-17, 2026-10-15).

Found on the first live PostgreSQL run: all Telegram id columns were 32-bit
``INTEGER`` on PostgreSQL, so ``_upsert_user`` failed with
``NumericValueOutOfRange`` for any account created after 2021 and
``_upsert_chat`` for any supergroup.  SQLite hid it (64-bit INTEGER).

Three lockstep facts, checked without a server:

1. every Telegram id column compiles to ``BIGINT`` on the PostgreSQL dialect
   and stays ``INTEGER`` on SQLite (``INTEGER PRIMARY KEY`` must remain the
   rowid alias there);
2. migration ``b5d8e2f4a917`` alters exactly the ORM's Telegram id columns —
   no more, no fewer;
3. no *new* 32-bit Telegram-looking column can be added unnoticed.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.schema import CreateTable
from sqlmodel import SQLModel

import nexus_ai_agent.storage.db  # noqa: F401  (registers every SQLModel table)
from nexus_ai_agent.storage import models

REPO = Path(__file__).resolve().parents[2]
MIGRATION = REPO / "migrations" / "versions" / "b5d8e2f4a917_telegram_ids_bigint.py"

#: Column names that carry a Telegram user/chat identifier.
TELEGRAM_ID_NAMES = {
    "telegram_id",
    "chat_id",
    "user_id",
    "user1_id",
    "user2_id",
    "referrer_id",
    "referee_id",
    "created_by",
    "set_by",
}


def _telegram_id_columns() -> list[tuple[str, str, sa.Column]]:  # type: ignore[type-arg]
    found: list[tuple[str, str, sa.Column]] = []  # type: ignore[type-arg]
    for table in SQLModel.metadata.sorted_tables:
        for column in table.columns:
            if column.name in TELEGRAM_ID_NAMES and not column.foreign_keys:
                found.append((table.name, column.name, column))
    return found


def _migration_columns() -> tuple[tuple[str, str], ...]:
    spec = importlib.util.spec_from_file_location("telegram_ids_bigint", MIGRATION)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return tuple(module.TELEGRAM_ID_COLUMNS)


def test_orm_has_the_expected_number_of_telegram_id_columns() -> None:
    assert len(_telegram_id_columns()) == 33


def test_every_telegram_id_column_is_bigint_on_postgres_and_integer_on_sqlite() -> None:
    pg, sl = postgresql.dialect(), sqlite.dialect()
    wrong: list[str] = []
    for table_name, column_name, column in _telegram_id_columns():
        pg_type = column.type.compile(dialect=pg)
        sl_type = column.type.compile(dialect=sl)
        if pg_type != "BIGINT" or sl_type != "INTEGER":
            wrong.append(f"{table_name}.{column_name}: pg={pg_type} sqlite={sl_type}")
    assert wrong == [], wrong


def test_postgres_ddl_spells_bigint_and_sqlite_ddl_is_unchanged() -> None:
    user = SQLModel.metadata.tables["user"]
    memory = SQLModel.metadata.tables["usermemory"]
    pg_user = str(CreateTable(user).compile(dialect=postgresql.dialect()))
    sl_user = str(CreateTable(user).compile(dialect=sqlite.dialect()))
    assert re.search(r"telegram_id BIGINT NOT NULL", pg_user), pg_user
    assert re.search(r"telegram_id INTEGER NOT NULL", sl_user), sl_user
    # A BIGINT primary key must still be the rowid alias on SQLite.
    sl_memory = str(CreateTable(memory).compile(dialect=sqlite.dialect()))
    assert "user_id INTEGER NOT NULL" in sl_memory and "PRIMARY KEY (user_id)" in sl_memory
    pg_memory = str(CreateTable(memory).compile(dialect=postgresql.dialect()))
    assert "user_id BIGSERIAL NOT NULL" in pg_memory, pg_memory


def test_migration_list_matches_the_orm_exactly() -> None:
    orm = tuple((table, column) for table, column, _ in _telegram_id_columns())
    assert _migration_columns() == orm


def test_internal_keys_and_counters_stay_32_bit() -> None:
    """Surrogate keys / FKs / counters are not Telegram ids and must not widen."""
    pg = postgresql.dialect()
    for table, column in (
        ("message", "chat_id"),
        ("task", "chat_id"),
        ("toolrun", "task_id"),
        ("referralcode", "total_referrals"),
        ("user", "id"),
    ):
        col = SQLModel.metadata.tables[table].columns[column]
        assert col.type.compile(dialect=pg) == "INTEGER", f"{table}.{column}"


def test_migration_is_postgres_only_and_type_only() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    assert source.count("if not _is_postgresql():") == 2
    assert source.count("op.alter_column(") == 2  # one loop body per direction
    for forbidden in ("op.create_table(", "op.drop_table(", "op.execute(", "op.drop_column("):
        assert forbidden not in source, forbidden
    assert models.TelegramId().compile(dialect=sqlite.dialect()) == "INTEGER"
    assert models.TelegramId().compile(dialect=postgresql.dialect()) == "BIGINT"
