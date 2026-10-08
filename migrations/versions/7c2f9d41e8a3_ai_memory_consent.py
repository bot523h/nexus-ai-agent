"""ai_memory_consent (P0-7: explicit consent for LLM egress)

Revision ID: 7c2f9d41e8a3
Revises: f4a9c2e71b08
Create Date: 2026-09-21 13:30:00.000000

Adds the consent state for the AIMemory engine to ``usermemory``:

* ``ai_memory_consent`` — tri-state text column: NULL (unset), ``granted``
  or ``denied``.  NULL means *no egress*: the engine must not send any user
  message text to the external LLM until the user explicitly grants it.
* ``ai_memory_consent_at`` — timestamp of the (re)vote, for auditability.
* ``ai_memory_prompted`` — one-time consent question already shown, so an
  ignored question is never repeated (prompt-spam protection).

Isolation contract (explicit, for review — "No hidden migration"):

* This revision alters exactly one table — ``usermemory`` — adding exactly
  three nullable columns, and nothing else.  No other table, index, view or
  data row is touched.
* All three columns are nullable with **no server default** so that
  ``ALTER TABLE ... ADD COLUMN`` is valid on both SQLite and PostgreSQL for
  any existing row set (backfilled rows read as "unset" / not prompted,
  which is the safe state).  The ORM mirrors the same shape
  (``str | None`` / ``datetime | None`` / ``bool | None`` with Python-side
  defaults), which keeps ``alembic check`` at zero drift on both backends —
  the same convention the initial revision uses for ``usermemory.name``.
* Downgrade removes the three columns and restores the pre-P0-7 schema.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlmodel.sql import sqltypes as sqlmodel_types

# revision identifiers, used by Alembic.
revision: str = "7c2f9d41e8a3"
down_revision: str | Sequence[str] | None = "f4a9c2e71b08"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "usermemory",
        sa.Column("ai_memory_consent", sqlmodel_types.AutoString(), nullable=True),
    )
    op.add_column(
        "usermemory",
        sa.Column("ai_memory_consent_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "usermemory",
        sa.Column("ai_memory_prompted", sa.Boolean(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("usermemory", "ai_memory_prompted")
    op.drop_column("usermemory", "ai_memory_consent_at")
    op.drop_column("usermemory", "ai_memory_consent")
