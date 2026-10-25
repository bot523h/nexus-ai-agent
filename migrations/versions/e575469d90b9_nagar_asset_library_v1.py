"""nagar asset library v1 (task-266)

Revision ID: e575469d90b9
Revises: 7c2f9d41e8a3
Create Date: 2026-10-11 14:49:34.388688

Adds the durable metadata tables for the first-party Asset Library.  The bytes
live in the content-addressed blob store; these tables are the authoritative
record of logical asset identity, immutable content versions and project
association:

* ``asset`` — stable logical identity of a media asset;
* ``assetversion`` — an immutable content version with its measured SHA-256 and
  byte size;
* ``projectassetlink`` — a durable, project-scoped association;
* ``assetingest`` — the recoverable state of one ingest attempt (its scoped
  idempotency key and STAGING -> READY / FAILED lifecycle).

Isolation contract (explicit, for review — "No hidden migration"):

* This revision adds exactly four tables and their indexes, and nothing else.
  No existing table, column, index or row is touched.
* ``assetingest.media_kind``/``media_type`` are nullable because classification
  happens only after the leading bytes are read; every other column is
  ``NOT NULL`` with the ORM providing the value.
* ``content_sha256``/``size_bytes``/``blob_key``/``staging_key``/``asset_id``/
  ``version_id``/``error_code`` are nullable by design: they are filled in as an
  ingest progresses and cleared when it terminates, which is what makes an
  interrupted ingest recoverable.
* Downgrade drops the four tables (and their indexes) in foreign-key-safe order.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlmodel.sql import sqltypes as sqlmodel_types

# revision identifiers, used by Alembic.
revision: str = "e575469d90b9"
down_revision: str | Sequence[str] | None = "7c2f9d41e8a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "asset",
        sa.Column("asset_id", sqlmodel_types.AutoString(), nullable=False),
        sa.Column("media_kind", sqlmodel_types.AutoString(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("asset_id"),
    )
    with op.batch_alter_table("asset", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_asset_created_at"), ["created_at"], unique=False)
        batch_op.create_index(batch_op.f("ix_asset_media_kind"), ["media_kind"], unique=False)

    op.create_table(
        "assetversion",
        sa.Column("version_id", sqlmodel_types.AutoString(), nullable=False),
        sa.Column("asset_id", sqlmodel_types.AutoString(), nullable=False),
        sa.Column("content_sha256", sqlmodel_types.AutoString(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("media_kind", sqlmodel_types.AutoString(), nullable=False),
        sa.Column("media_type", sqlmodel_types.AutoString(), nullable=False),
        sa.Column("blob_key", sqlmodel_types.AutoString(), nullable=False),
        sa.Column("original_filename", sqlmodel_types.AutoString(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["asset_id"], ["asset.asset_id"]),
        sa.PrimaryKeyConstraint("version_id"),
    )
    with op.batch_alter_table("assetversion", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_assetversion_asset_id"), ["asset_id"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_assetversion_content_sha256"), ["content_sha256"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_assetversion_created_at"), ["created_at"], unique=False
        )

    op.create_table(
        "projectassetlink",
        sa.Column("link_id", sqlmodel_types.AutoString(), nullable=False),
        sa.Column("project_id", sqlmodel_types.AutoString(), nullable=False),
        sa.Column("asset_id", sqlmodel_types.AutoString(), nullable=False),
        sa.Column("version_id", sqlmodel_types.AutoString(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["asset_id"], ["asset.asset_id"]),
        sa.ForeignKeyConstraint(["version_id"], ["assetversion.version_id"]),
        sa.PrimaryKeyConstraint("link_id"),
    )
    with op.batch_alter_table("projectassetlink", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_projectassetlink_asset_id"), ["asset_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_projectassetlink_created_at"), ["created_at"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_projectassetlink_project_id"), ["project_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_projectassetlink_version_id"), ["version_id"], unique=False
        )

    op.create_table(
        "assetingest",
        sa.Column("ingest_id", sqlmodel_types.AutoString(), nullable=False),
        sa.Column("idempotency_key", sqlmodel_types.AutoString(), nullable=False),
        sa.Column("project_id", sqlmodel_types.AutoString(), nullable=False),
        sa.Column("request_fingerprint", sqlmodel_types.AutoString(), nullable=False),
        sa.Column("state", sqlmodel_types.AutoString(), nullable=False),
        sa.Column("media_kind", sqlmodel_types.AutoString(), nullable=True),
        sa.Column("media_type", sqlmodel_types.AutoString(), nullable=True),
        sa.Column("original_filename", sqlmodel_types.AutoString(), nullable=False),
        sa.Column("content_sha256", sqlmodel_types.AutoString(), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("blob_key", sqlmodel_types.AutoString(), nullable=True),
        sa.Column("staging_key", sqlmodel_types.AutoString(), nullable=True),
        sa.Column("asset_id", sqlmodel_types.AutoString(), nullable=True),
        sa.Column("version_id", sqlmodel_types.AutoString(), nullable=True),
        sa.Column("error_code", sqlmodel_types.AutoString(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("ingest_id"),
        sa.UniqueConstraint("project_id", "idempotency_key", name="uq_assetingest_project_idem"),
    )
    with op.batch_alter_table("assetingest", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_assetingest_asset_id"), ["asset_id"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_assetingest_content_sha256"), ["content_sha256"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_assetingest_created_at"), ["created_at"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_assetingest_idempotency_key"), ["idempotency_key"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_assetingest_project_id"), ["project_id"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_assetingest_request_fingerprint"),
            ["request_fingerprint"],
            unique=False,
        )
        batch_op.create_index(batch_op.f("ix_assetingest_state"), ["state"], unique=False)
        batch_op.create_index(batch_op.f("ix_assetingest_updated_at"), ["updated_at"], unique=False)
        batch_op.create_index(batch_op.f("ix_assetingest_version_id"), ["version_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("assetingest", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_assetingest_version_id"))
        batch_op.drop_index(batch_op.f("ix_assetingest_updated_at"))
        batch_op.drop_index(batch_op.f("ix_assetingest_state"))
        batch_op.drop_index(batch_op.f("ix_assetingest_request_fingerprint"))
        batch_op.drop_index(batch_op.f("ix_assetingest_project_id"))
        batch_op.drop_index(batch_op.f("ix_assetingest_idempotency_key"))
        batch_op.drop_index(batch_op.f("ix_assetingest_created_at"))
        batch_op.drop_index(batch_op.f("ix_assetingest_content_sha256"))
        batch_op.drop_index(batch_op.f("ix_assetingest_asset_id"))
    op.drop_table("assetingest")

    with op.batch_alter_table("projectassetlink", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_projectassetlink_version_id"))
        batch_op.drop_index(batch_op.f("ix_projectassetlink_project_id"))
        batch_op.drop_index(batch_op.f("ix_projectassetlink_created_at"))
        batch_op.drop_index(batch_op.f("ix_projectassetlink_asset_id"))
    op.drop_table("projectassetlink")

    with op.batch_alter_table("assetversion", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_assetversion_created_at"))
        batch_op.drop_index(batch_op.f("ix_assetversion_content_sha256"))
        batch_op.drop_index(batch_op.f("ix_assetversion_asset_id"))
    op.drop_table("assetversion")

    with op.batch_alter_table("asset", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_asset_media_kind"))
        batch_op.drop_index(batch_op.f("ix_asset_created_at"))
    op.drop_table("asset")
