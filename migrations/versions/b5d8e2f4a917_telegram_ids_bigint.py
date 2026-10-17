"""telegram_ids_bigint (audit A-17: Telegram identifiers are 64-bit)

Revision ID: b5d8e2f4a917
Revises: e575469d90b9
Create Date: 2026-10-15 13:10:00.000000

Found by the first live PostgreSQL run of the 2026-10-15 remediation: every
Telegram identifier column (user ids, chat/channel ids, "who set this" ids)
was created as a 32-bit ``INTEGER``.  Telegram user ids crossed 2**31 in 2021
and supergroup/channel ids are 13-digit ``-100…`` numbers, so on PostgreSQL
the first message from a modern account — or from any supergroup — failed
with ``NumericValueOutOfRange`` inside ``_upsert_user`` / ``_upsert_chat``.
SQLite never showed the defect because its INTEGER is 64-bit.

Isolation contract (explicit, for review — "No hidden migration"):

* PostgreSQL only: on SQLite the ORM type — the named ``TypeDecorator``
  ``storage.models.TelegramId`` (``impl = BigInteger``, whose
  ``load_dialect_impl`` returns ``Integer()`` for the sqlite dialect and
  ``BigInteger()`` for every other one) — renders ``INTEGER``, so the SQLite
  schema is byte-for-byte what it was (``INTEGER PRIMARY KEY`` stays the
  rowid alias) and ``alembic check`` sees no drift: there is nothing to do
  and the revision is a no-op there. On PostgreSQL the same type renders
  ``BIGINT``, which is exactly what this revision alters.
* Exactly the ``TELEGRAM_ID_COLUMNS`` listed below change, type only
  (``INTEGER`` → ``BIGINT``); nullability, defaults, indexes and unique
  constraints are untouched (PostgreSQL rebuilds the affected indexes).
  Internal surrogate keys (``*.id``, ``message.chat_id``/``task.chat_id``
  → ``chat.id``, ``toolrun.task_id``) and counters stay ``INTEGER``.
* The list is kept in lockstep with the ORM by
  ``tests/unit/test_telegram_id_width.py``.
* Downgrade narrows the same columns back to ``INTEGER``; PostgreSQL refuses
  it while any stored id exceeds 32 bits, which is the correct failure.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b5d8e2f4a917"
down_revision: str | Sequence[str] | None = "e575469d90b9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: (table, column) — every Telegram identifier column, in ORM order.
TELEGRAM_ID_COLUMNS: tuple[tuple[str, str], ...] = (
    ("adcampaign", "chat_id"),
    ("adcampaign", "created_by"),
    ("adminlog", "user_id"),
    ("analyticsevent", "chat_id"),
    ("analyticsevent", "user_id"),
    ("anonsession", "user1_id"),
    ("anonsession", "user2_id"),
    ("channelschedule", "chat_id"),
    ("chat", "chat_id"),
    ("cloudfile", "user_id"),
    ("documentchunk", "user_id"),
    ("engagementconfig", "chat_id"),
    ("forcejoinconfig", "chat_id"),
    ("moderationconfig", "chat_id"),
    ("personalityconfig", "chat_id"),
    ("personalityconfig", "set_by"),
    ("quizscore", "user_id"),
    ("quizscore", "chat_id"),
    ("referral", "referrer_id"),
    ("referral", "referee_id"),
    ("referralcode", "user_id"),
    ("reminder", "user_id"),
    ("reminder", "chat_id"),
    ("user", "telegram_id"),
    ("useractiveagent", "user_id"),
    ("userlanguage", "user_id"),
    ("usermemory", "user_id"),
    ("userreputation", "user_id"),
    ("userreputation", "chat_id"),
    ("userxp", "user_id"),
    ("userxp", "chat_id"),
    ("viralpost", "chat_id"),
    ("welcomemessage", "chat_id"),
)


def _is_postgresql() -> bool:
    return op.get_context().dialect.name == "postgresql"


def upgrade() -> None:
    if not _is_postgresql():
        return
    for table, column in TELEGRAM_ID_COLUMNS:
        op.alter_column(
            table,
            column,
            type_=sa.BigInteger(),
            existing_type=sa.Integer(),
            existing_nullable=False,
        )


def downgrade() -> None:
    if not _is_postgresql():
        return
    for table, column in reversed(TELEGRAM_ID_COLUMNS):
        op.alter_column(
            table,
            column,
            type_=sa.Integer(),
            existing_type=sa.BigInteger(),
            existing_nullable=False,
        )
