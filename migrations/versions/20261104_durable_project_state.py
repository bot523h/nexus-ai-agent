"""durable project state (task-282)

Revision ID: 20261104dpstate
Revises: d4e1c0a79b21
Create Date: 2026-11-04 00:00:00.000000

Adds the durable record for the Studio project/revision lifecycle:

* ``projectrevision`` — one IMMUTABLE, content-addressed snapshot of a Studio
  ``Project`` state; ``revision_id`` is the canonical state hash (primary key),
  so identical state cannot be stored twice and history cannot be rewritten;
* ``projectrevisionhead`` — the single mutable pointer to a project's accepted
  revision, updated with an optimistic compare-and-swap on ``revision_id``.

Isolation contract (explicit, for review — "No hidden migration"):

* This revision creates exactly two tables and their indexes, and nothing else.
  No existing table, column, index or row is touched.
* ``projectrevisionhead.project_id`` is a foreign key to the existing
  ``appproject`` table: a revision head can only reference a project identity
  that already exists in the canonical application directory.  No parallel
  project identity is introduced.
* ``projectrevision.parent_revision_id`` is nullable: the genesis revision has
  no parent.  Every other column is ``NOT NULL``.
* Downgrade drops the two tables (and their indexes) in foreign-key-safe order.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlmodel.sql import sqltypes as sqlmodel_types

# revision identifiers, used by Alembic.
revision: str = "20261104dpstate"
down_revision: str | Sequence[str] | None = "d4e1c0a79b21"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "projectrevision",
        sa.Column("revision_id", sqlmodel_types.AutoString(), nullable=False),
        sa.Column("project_id", sqlmodel_types.AutoString(), nullable=False),
        sa.Column("parent_revision_id", sqlmodel_types.AutoString(), nullable=True),
        sa.Column("state_revision", sa.Integer(), nullable=False),
        sa.Column("state_hash", sqlmodel_types.AutoString(), nullable=False),
        sa.Column("state_json", sqlmodel_types.AutoString(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("revision_id"),
        sa.UniqueConstraint(
            "project_id", "revision_id", name="uq_projectrevision_project_revision"
        ),
    )
    with op.batch_alter_table("projectrevision", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_projectrevision_project_id"), ["project_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_projectrevision_parent_revision_id"),
            ["parent_revision_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_projectrevision_state_hash"), ["state_hash"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_projectrevision_created_at"), ["created_at"], unique=False
        )

    op.create_table(
        "projectrevisionhead",
        sa.Column("project_id", sqlmodel_types.AutoString(), nullable=False),
        sa.Column("revision_id", sqlmodel_types.AutoString(), nullable=False),
        sa.Column("state_revision", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["appproject.project_id"]),
        sa.PrimaryKeyConstraint("project_id"),
    )
    with op.batch_alter_table("projectrevisionhead", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_projectrevisionhead_revision_id"), ["revision_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_projectrevisionhead_updated_at"), ["updated_at"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("projectrevisionhead", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_projectrevisionhead_updated_at"))
        batch_op.drop_index(batch_op.f("ix_projectrevisionhead_revision_id"))
    op.drop_table("projectrevisionhead")

    with op.batch_alter_table("projectrevision", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_projectrevision_created_at"))
        batch_op.drop_index(batch_op.f("ix_projectrevision_state_hash"))
        batch_op.drop_index(batch_op.f("ix_projectrevision_parent_revision_id"))
        batch_op.drop_index(batch_op.f("ix_projectrevision_project_id"))
    op.drop_table("projectrevision")
