"""memory_trust_production — W3 Masterpiece 2

Revision ID: c3d5a8e91f2b
Revises: 7c2f9d41e8a3
Create Date: 2026-09-28

W3 — Memory Trust — Production/World-Class Implementation

Creates memory_trust_records table — the single source of truth for
provenance-aware, owner-isolated, conflict-explicit, idempotent,
lineage-preserving, deletion-cascading memory.

15 Golden Laws enforced:
- One Runtime → One DB Boundary (Law 5): table owned by runtime, uses injected session
- Owner Isolation Security Invariant (Law 7): owner_user_id mandatory index
- Replay Idempotent (Law 9): UNIQUE(owner_user_id, namespace, key, source_event_id)
- Conflict Explicit (Law 10): conflicted status preserved, not overwritten
- Correction Lineage (Law 11): supersedes/superseded_by + lineage JSON
- Forget Reaches Derived (Law 12): tombstones + cascade via lineage

Isolation contract:
- Creates exactly one table + indexes matching SQLModel metadata, nothing else
- Works on both SQLite and PostgreSQL
- Downgrade drops table
- No data mutation
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
import sqlmodel

# revision identifiers, used by Alembic.
revision: str = "c3d5a8e91f2b"
down_revision: str | Sequence[str] | None = "7c2f9d41e8a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "memory_trust_records",
        sa.Column("id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("owner_user_id", sa.Integer(), nullable=False),
        sa.Column("tenant", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("scope_type", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("scope_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("namespace", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("key", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("type", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("status", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("content", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("content_hash", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("source_type", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("source_event_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("actor", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("observed_at", sa.DateTime(), nullable=False),
        sa.Column("valid_from", sa.DateTime(), nullable=True),
        sa.Column("valid_until", sa.DateTime(), nullable=True),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("policy_snapshot", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("retention_until", sa.DateTime(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.Column("superseded_by", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("supersedes", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("lineage", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("evidence_digest", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("correlation_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "owner_user_id", "namespace", "key", "source_event_id", name="uq_memory_idempotency"
        ),
    )
    # Indexes — names must match SQLModel auto-generated names (ix_memory_trust_records_<col>)
    # This ensures alembic check reports zero drift.
    op.create_index("ix_memory_trust_records_owner_user_id", "memory_trust_records", ["owner_user_id"], unique=False)
    op.create_index("ix_memory_trust_records_tenant", "memory_trust_records", ["tenant"], unique=False)
    op.create_index("ix_memory_trust_records_scope_type", "memory_trust_records", ["scope_type"], unique=False)
    op.create_index("ix_memory_trust_records_scope_id", "memory_trust_records", ["scope_id"], unique=False)
    op.create_index("ix_memory_trust_records_namespace", "memory_trust_records", ["namespace"], unique=False)
    op.create_index("ix_memory_trust_records_key", "memory_trust_records", ["key"], unique=False)
    op.create_index("ix_memory_trust_records_type", "memory_trust_records", ["type"], unique=False)
    op.create_index("ix_memory_trust_records_status", "memory_trust_records", ["status"], unique=False)
    op.create_index("ix_memory_trust_records_content_hash", "memory_trust_records", ["content_hash"], unique=False)
    op.create_index("ix_memory_trust_records_source_type", "memory_trust_records", ["source_type"], unique=False)
    op.create_index("ix_memory_trust_records_source_event_id", "memory_trust_records", ["source_event_id"], unique=False)
    op.create_index("ix_memory_trust_records_actor", "memory_trust_records", ["actor"], unique=False)
    op.create_index("ix_memory_trust_records_observed_at", "memory_trust_records", ["observed_at"], unique=False)
    op.create_index("ix_memory_trust_records_recorded_at", "memory_trust_records", ["recorded_at"], unique=False)
    op.create_index("ix_memory_trust_records_updated_at", "memory_trust_records", ["updated_at"], unique=False)
    op.create_index("ix_memory_trust_records_deleted_at", "memory_trust_records", ["deleted_at"], unique=False)
    op.create_index("ix_memory_trust_records_superseded_by", "memory_trust_records", ["superseded_by"], unique=False)
    op.create_index("ix_memory_trust_records_supersedes", "memory_trust_records", ["supersedes"], unique=False)
    op.create_index("ix_memory_trust_records_evidence_digest", "memory_trust_records", ["evidence_digest"], unique=False)
    op.create_index("ix_memory_trust_records_correlation_id", "memory_trust_records", ["correlation_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_memory_trust_records_correlation_id", table_name="memory_trust_records")
    op.drop_index("ix_memory_trust_records_evidence_digest", table_name="memory_trust_records")
    op.drop_index("ix_memory_trust_records_supersedes", table_name="memory_trust_records")
    op.drop_index("ix_memory_trust_records_superseded_by", table_name="memory_trust_records")
    op.drop_index("ix_memory_trust_records_deleted_at", table_name="memory_trust_records")
    op.drop_index("ix_memory_trust_records_updated_at", table_name="memory_trust_records")
    op.drop_index("ix_memory_trust_records_recorded_at", table_name="memory_trust_records")
    op.drop_index("ix_memory_trust_records_observed_at", table_name="memory_trust_records")
    op.drop_index("ix_memory_trust_records_actor", table_name="memory_trust_records")
    op.drop_index("ix_memory_trust_records_source_event_id", table_name="memory_trust_records")
    op.drop_index("ix_memory_trust_records_source_type", table_name="memory_trust_records")
    op.drop_index("ix_memory_trust_records_content_hash", table_name="memory_trust_records")
    op.drop_index("ix_memory_trust_records_status", table_name="memory_trust_records")
    op.drop_index("ix_memory_trust_records_type", table_name="memory_trust_records")
    op.drop_index("ix_memory_trust_records_key", table_name="memory_trust_records")
    op.drop_index("ix_memory_trust_records_namespace", table_name="memory_trust_records")
    op.drop_index("ix_memory_trust_records_scope_id", table_name="memory_trust_records")
    op.drop_index("ix_memory_trust_records_scope_type", table_name="memory_trust_records")
    op.drop_index("ix_memory_trust_records_tenant", table_name="memory_trust_records")
    op.drop_index("ix_memory_trust_records_owner_user_id", table_name="memory_trust_records")
    op.drop_table("memory_trust_records")
