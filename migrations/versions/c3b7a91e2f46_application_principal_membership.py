"""application-owned principal, channel and project membership

Revision ID: c3b7a91e2f46
Revises: b5d8e2f4a917
Create Date: 2026-10-23 19:40:00

Adds exactly four additive tables.  Existing Telegram user/chat records,
creative assets, queue databases and LangGraph checkpoints are untouched.
The canonical principal ID is not derived from a Telegram ID; a channel
account is only one uniquely bound route to that principal.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlmodel.sql import sqltypes as sqlmodel_types

revision: str = "c3b7a91e2f46"
down_revision: str | Sequence[str] | None = "b5d8e2f4a917"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "appprincipal",
        sa.Column("principal_id", sqlmodel_types.AutoString(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("principal_id"),
    )
    op.create_table(
        "appproject",
        sa.Column("project_id", sqlmodel_types.AutoString(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("project_id"),
    )
    op.create_table(
        "appchannelbinding",
        sa.Column("binding_id", sqlmodel_types.AutoString(), nullable=False),
        sa.Column("principal_id", sqlmodel_types.AutoString(), nullable=False),
        sa.Column("channel", sqlmodel_types.AutoString(), nullable=False),
        sa.Column("external_id", sqlmodel_types.AutoString(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["principal_id"], ["appprincipal.principal_id"]),
        sa.PrimaryKeyConstraint("binding_id"),
        sa.UniqueConstraint(
            "channel",
            "external_id",
            name="uq_appchannelbinding_channel_external",
        ),
    )
    with op.batch_alter_table("appchannelbinding", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_appchannelbinding_principal_id"),
            ["principal_id"],
            unique=False,
        )
    op.create_table(
        "appprojectmembership",
        sa.Column("project_id", sqlmodel_types.AutoString(), nullable=False),
        sa.Column("principal_id", sqlmodel_types.AutoString(), nullable=False),
        sa.Column("permissions_json", sqlmodel_types.AutoString(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["principal_id"], ["appprincipal.principal_id"]),
        sa.ForeignKeyConstraint(["project_id"], ["appproject.project_id"]),
        sa.PrimaryKeyConstraint("project_id", "principal_id"),
    )


def downgrade() -> None:
    op.drop_table("appprojectmembership")
    with op.batch_alter_table("appchannelbinding", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_appchannelbinding_principal_id"))
    op.drop_table("appchannelbinding")
    op.drop_table("appproject")
    op.drop_table("appprincipal")
