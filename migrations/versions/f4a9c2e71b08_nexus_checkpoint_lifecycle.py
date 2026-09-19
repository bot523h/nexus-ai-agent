"""nexus_checkpoint_lifecycle (PostgreSQL only)

Revision ID: f4a9c2e71b08
Revises: 47903d282ede
Create Date: 2026-09-19 00:00:00.000000

PR3 option A (owner-approved): the lifecycle metadata index is materialized
in PostgreSQL on the PostgreSQL path, so serverless deployments (Neon) no
longer depend on an ephemeral local sidecar file for lifecycle history.

Isolation contract (explicit, for review — "No hidden migration"):

* This revision creates exactly one table — ``nexus_checkpoint_lifecycle``
  — and nothing else.  No LangGraph table is ever Alembic-owned, and this
  revision never touches them.  No other DDL anywhere in the branch
  accompanies this revision.
* PostgreSQL dialect only: on SQLite the same table stays owned by the
  store-level ``CREATE TABLE IF NOT EXISTS`` (disposable-index semantics),
  which keeps the shared chain ORM-exact on SQLite and ``alembic check``
  clean.
* Timestamps are TEXT (ISO-8601 UTC), identical to the SQLite table, so
  both backends serialize lifecycle rows the same way and reports stay
  uniform.
* The primary key ``(thread_id, checkpoint_id)`` doubles as the lookup
  index for per-thread touches — no additional index is created.
* A database stamped at this head therefore *must* contain the table;
  ``nexus adopt-pg`` treats a legacy database without it as drift
  (fail-fast, explicit operator fix — never implicit repair).

No hidden migration. No hidden mutation. No implicit repair.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f4a9c2e71b08"
down_revision: str | Sequence[str] | None = "47903d282ede"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _is_postgresql() -> bool:
    return op.get_context().dialect.name == "postgresql"


def upgrade() -> None:
    if not _is_postgresql():
        return
    op.create_table(
        "nexus_checkpoint_lifecycle",
        sa.Column("thread_id", sa.Text(), nullable=False),
        sa.Column("checkpoint_id", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("last_accessed_at", sa.Text(), nullable=True),
        sa.Column("active_until", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("thread_id", "checkpoint_id"),
    )


def downgrade() -> None:
    if not _is_postgresql():
        return
    op.drop_table("nexus_checkpoint_lifecycle")
