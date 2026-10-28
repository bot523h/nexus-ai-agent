"""knowledge_retrieval (task-273): canonical source registry table

Revision ID: d4e1c0a79b21
Revises: c3b7a91e2f46
Create Date: 2026-10-23 19:40:00.000000

Adds the authoritative registry of authorized knowledge sources — the rows
that make a citation resolvable (``source_id`` + ``sha256``) and stale index
hits detectable after restarts, rebuilds and source edits.  The derived
LlamaIndex store under ``data/knowledge_index/`` is intentionally **not**
part of this migration: it is rebuildable state, never canonical.

Isolation contract (explicit, for review — "No hidden migration"):

* This revision adds exactly one table (``knowledgesource``) and its
  indexes, and nothing else.  No existing table, column, index or row is
  touched.
* ``root`` + ``path`` carry a UNIQUE constraint so two registrations of the
  same source cannot diverge; ``sha256``/``status``/``chunks``/timestamps
  are filled by ingestion and ``last_error`` holds operator-safe reason
  strings only (never file content).
* Downgrade drops the table (and its indexes) — equivalent to unregistering
  every source; the derived index is left in place and simply becomes
  orphaned state that ``nexus knowledge rebuild`` recreates from disk after
  the registry is restored.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlmodel.sql import sqltypes as sqlmodel_types

# revision identifiers, used by Alembic.
revision: str = "d4e1c0a79b21"
down_revision: str | Sequence[str] | None = "c3b7a91e2f46"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "knowledgesource",
        sa.Column("source_id", sqlmodel_types.AutoString(), nullable=False),
        sa.Column("root", sqlmodel_types.AutoString(), nullable=False),
        sa.Column("path", sqlmodel_types.AutoString(), nullable=False),
        sa.Column("sha256", sqlmodel_types.AutoString(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("status", sqlmodel_types.AutoString(), nullable=False),
        sa.Column("chunks", sa.Integer(), nullable=False),
        sa.Column("indexed_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("last_error", sqlmodel_types.AutoString(), nullable=True),
        sa.PrimaryKeyConstraint("source_id"),
        sa.UniqueConstraint("root", "path", name="uq_knowledgesource_root_path"),
    )
    with op.batch_alter_table("knowledgesource", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_knowledgesource_root"), ["root"], unique=False)
        batch_op.create_index(batch_op.f("ix_knowledgesource_path"), ["path"], unique=False)
        batch_op.create_index(batch_op.f("ix_knowledgesource_sha256"), ["sha256"], unique=False)
        batch_op.create_index(batch_op.f("ix_knowledgesource_status"), ["status"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_knowledgesource_updated_at"), ["updated_at"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("knowledgesource", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_knowledgesource_updated_at"))
        batch_op.drop_index(batch_op.f("ix_knowledgesource_status"))
        batch_op.drop_index(batch_op.f("ix_knowledgesource_sha256"))
        batch_op.drop_index(batch_op.f("ix_knowledgesource_path"))
        batch_op.drop_index(batch_op.f("ix_knowledgesource_root"))
    op.drop_table("knowledgesource")
