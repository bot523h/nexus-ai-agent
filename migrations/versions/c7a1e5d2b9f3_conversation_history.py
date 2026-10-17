"""conversation_history under the Alembic authority (audit 2026-10-15)

Revision ID: c7a1e5d2b9f3
Revises: b5d8e2f4a917
Create Date: 2026-10-15 15:10:00.000000

``features.conversation_store.ConversationStore`` used to declare this table on
a private ``MetaData`` and run ``create_all()`` at runtime — a second schema
owner next to the migration chain, invisible to ``alembic check``.  The table
is now ``storage.models.ConversationHistory`` and this revision makes the chain
its owner.

Isolation contract (explicit, for review — "No hidden migration"):

* **Adopt, never rebuild.** Every deployment where the store ever ran already
  has the table.  The model's DDL is byte-for-byte what the store emitted
  (``id`` INTEGER PK, ``conv_id``/``role`` VARCHAR NOT NULL, ``parts_json``
  TEXT NOT NULL, ``created_at`` DATETIME DEFAULT CURRENT_TIMESTAMP, index
  ``ix_conv_history_conv_id``), so an existing table is left untouched — no
  row is read, copied or deleted.  Only a database that never had the table
  (fresh install, or a deployment that never used the store) gets it created.
* The index is created only when missing, under its historical name.
* Downgrade drops the table (symmetric with a fresh-install upgrade).  On a
  database that had the table *before* this revision that is destructive for
  conversation history — exactly like every other table-creating revision's
  downgrade — so it is an operator decision, never a runtime one.
* Dialect-neutral: SQLite and PostgreSQL run the same operations.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c7a1e5d2b9f3"
down_revision: str | Sequence[str] | None = "b5d8e2f4a917"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "conversation_history"
INDEX = "ix_conv_history_conv_id"


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(TABLE):
        op.create_table(
            TABLE,
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("conv_id", sa.String(), nullable=False),
            sa.Column("role", sa.String(), nullable=False),
            sa.Column("parts_json", sa.Text(), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(),
                server_default=sa.func.current_timestamp(),
                nullable=True,
            ),
            sa.PrimaryKeyConstraint("id"),
        )
        existing_indexes: set[str] = set()
    else:
        existing_indexes = {
            str(index["name"]) for index in inspector.get_indexes(TABLE) if index.get("name")
        }
    if INDEX not in existing_indexes:
        op.create_index(INDEX, TABLE, ["conv_id"], unique=False)


def downgrade() -> None:
    op.drop_index(INDEX, table_name=TABLE)
    op.drop_table(TABLE)
