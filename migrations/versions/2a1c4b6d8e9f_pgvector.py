"""enable pgvector extension on PostgreSQL

Revision ID: 2a1c4b6d8e9f
Revises: 47903d282ede
Create Date: 2026-09-17

Enables the ``vector`` (pgvector) extension so the hosted PostgreSQL backend
(Neon, per D9) can store embeddings alongside relational data.  It is a no-op
on SQLite, which keeps the revision chain fully portable across both backends.

Safe to run idempotently: ``CREATE EXTENSION IF NOT EXISTS`` (Alembic renders
the guard automatically), and the downgrade drops it last so a downgrade
journey to ``base`` never leaves a dangling extension behind.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "2a1c4b6d8e9f"
down_revision: str | Sequence[str] | None = "47903d282ede"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    """Upgrade schema: enable pgvector on PostgreSQL, no-op on SQLite."""
    if _is_postgres():
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")


def downgrade() -> None:
    """Downgrade schema: drop pgvector on PostgreSQL, no-op on SQLite."""
    if _is_postgres():
        op.execute("DROP EXTENSION IF EXISTS vector")
