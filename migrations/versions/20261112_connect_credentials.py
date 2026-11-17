"""nagar connect credential persistence (task nagar-connect-persistence-01).

The table stores encrypted credential envelopes only. KEKs are intentionally
external to the database. No plaintext secret or key material is represented
by this migration.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261112connect"
down_revision: str | Sequence[str] | None = "20261109contentowner"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "nexus_connect_credential",
        sa.Column("connection_id", sa.String(length=128), nullable=False),
        sa.Column("principal_kind", sa.String(length=16), nullable=False),
        sa.Column("principal_id", sa.String(length=128), nullable=False),
        sa.Column("project_ids_json", sa.Text(), nullable=False),
        sa.Column("scopes_json", sa.Text(), nullable=False),
        sa.Column("credential_id", sa.String(length=128), nullable=False),
        sa.Column("credential_state", sa.String(length=16), nullable=False),
        sa.Column("connection_state", sa.String(length=16), nullable=False),
        sa.Column("credential_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("connection_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rotated_from", sa.String(length=128), nullable=True),
        sa.Column("kek_id", sa.String(length=128), nullable=False),
        sa.Column("wrapped_dek", sa.LargeBinary(), nullable=False),
        sa.Column("wrap_nonce", sa.LargeBinary(length=12), nullable=False),
        sa.Column("data_nonce", sa.LargeBinary(length=12), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("aad", sa.LargeBinary(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("row_version", sa.Integer(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("connection_id"),
        sa.UniqueConstraint("credential_id", name="uq_nexus_connect_credential_id"),
    )
    with op.batch_alter_table("nexus_connect_credential", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_nexus_connect_credential_principal_id"), ["principal_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_nexus_connect_credential_connection_state"),
            ["connection_state"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_nexus_connect_credential_kek_id"), ["kek_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_nexus_connect_credential_updated_at"), ["updated_at"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("nexus_connect_credential", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_nexus_connect_credential_updated_at"))
        batch_op.drop_index(batch_op.f("ix_nexus_connect_credential_kek_id"))
        batch_op.drop_index(batch_op.f("ix_nexus_connect_credential_connection_state"))
        batch_op.drop_index(batch_op.f("ix_nexus_connect_credential_principal_id"))
    op.drop_table("nexus_connect_credential")
