"""durable artifact→revision binding (task-282 Slice 2)

Revision ID: 20261106artbind
Revises: 20261104dpstate
Create Date: 2026-11-06 00:00:00.000000

Adds the durable link between a published creative artifact and the exact
immutable project revision it served:

* ``projectartifactbinding`` — one IMMUTABLE, content-addressed binding row;
  ``artifact_id`` is the content address (``sha256:<hex>``) of the published
  artifact bytes (primary key), so an identical artifact is the same row
  (idempotent replay) and its provenance can never be silently re-pointed.

Isolation contract (explicit, for review — "No hidden migration"):

* This revision creates exactly one table and its indexes, and nothing else.
  No existing table, column, index or row is touched.
* ``projectartifactbinding.project_id`` is a foreign key to the existing
  ``appproject`` table and ``revision_id`` to the existing ``projectrevision``
  table: a binding can only reference an identity and a revision that already
  exist.  No parallel artifact store, project identity or revision log is
  introduced.
* Every column is ``NOT NULL``.
* Downgrade drops the table (and its indexes).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlmodel.sql import sqltypes as sqlmodel_types

# revision identifiers, used by Alembic.
revision: str = "20261106artbind"
down_revision: str | Sequence[str] | None = "20261104dpstate"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "projectartifactbinding",
        sa.Column("artifact_id", sqlmodel_types.AutoString(), nullable=False),
        sa.Column("project_id", sqlmodel_types.AutoString(), nullable=False),
        sa.Column("revision_id", sqlmodel_types.AutoString(), nullable=False),
        sa.Column("job_id", sqlmodel_types.AutoString(), nullable=False, server_default=""),
        sa.Column(
            "artifact_kind", sqlmodel_types.AutoString(), nullable=False, server_default="binary"
        ),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("artifact_version_id", sqlmodel_types.AutoString(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["appproject.project_id"]),
        sa.ForeignKeyConstraint(["revision_id"], ["projectrevision.revision_id"]),
        sa.PrimaryKeyConstraint("artifact_id"),
        sa.UniqueConstraint(
            "project_id",
            "artifact_id",
            name="uq_projectartifactbinding_project_artifact",
        ),
    )
    with op.batch_alter_table("projectartifactbinding", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_projectartifactbinding_project_id"), ["project_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_projectartifactbinding_revision_id"), ["revision_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_projectartifactbinding_job_id"), ["job_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_projectartifactbinding_artifact_version_id"),
            ["artifact_version_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_projectartifactbinding_created_at"), ["created_at"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("projectartifactbinding", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_projectartifactbinding_created_at"))
        batch_op.drop_index(batch_op.f("ix_projectartifactbinding_artifact_version_id"))
        batch_op.drop_index(batch_op.f("ix_projectartifactbinding_job_id"))
        batch_op.drop_index(batch_op.f("ix_projectartifactbinding_revision_id"))
        batch_op.drop_index(batch_op.f("ix_projectartifactbinding_project_id"))
    op.drop_table("projectartifactbinding")
