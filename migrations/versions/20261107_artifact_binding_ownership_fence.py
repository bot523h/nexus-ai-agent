"""artifact binding ownership fence (task-282 Slice 2, F1)

Revision ID: 20261107bindfence
Revises: 20261106artbind
Create Date: 2026-11-07 00:00:00.000000

Adds ``projectartifactbinding.attempt`` — the queue fencing token (the job
row's ``attempt`` counter) of the execution that committed the binding.

Why: the artifact→revision binding is committed by the queue's ``confirm`` hook
between verification/publication and the ``COMPLETED`` commit.  A takeover can
land *inside* that window (after the boundary fence re-check, at the binding
write), so the superseded attempt commits a binding and then fails the
``COMPLETED`` CAS.  Without a token on the binding row the queue cannot tell
which attempt wrote it, so the lost-CAS path could not retract the row this
attempt wrote without risking deleting a successor's binding.  Recording the
token makes the retraction exact: delete only
``(artifact_id, job_id, attempt = <this claim>)``.

Isolation contract (explicit, for review — "No hidden migration"):

* This revision alters exactly one table — ``projectartifactbinding`` — adding
  exactly one NOT NULL column with a server default of ``0``, and nothing else.
  No other table, index, view or data row is touched.
* The server default ``0`` backfills every existing row to "pre-token", which
  is the safe state: a retraction scoped to a real token (``>= 1``) can never
  match a backfilled row, so no legacy binding is ever deleted by the fence.
* ``ALTER TABLE ... ADD COLUMN`` with a constant server default is valid on
  both SQLite and PostgreSQL for any existing row set.
* Downgrade drops the column and restores the pre-fence schema.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20261107bindfence"
down_revision: str | Sequence[str] | None = "20261106artbind"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("projectartifactbinding", schema=None) as batch_op:
        batch_op.add_column(sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"))


def downgrade() -> None:
    with op.batch_alter_table("projectartifactbinding", schema=None) as batch_op:
        batch_op.drop_column("attempt")
