"""content-address ownership identity (task-282 Slice 2, F1 cross-project)

Revision ID: 20261109contentowner
Revises: 20261108perjobbind
Create Date: 2026-11-09 00:00:00.000000

Closes the cross-project ownership race.  ``projectartifactbinding`` is keyed
``(artifact_id, job_id)``, so the primary key can never enforce "one content
address → one project": two jobs in two projects that publish identical bytes
own two distinct rows by design.  The previous guard was a Python read-then-
insert (``SELECT project_id ... LIMIT 1``); under real concurrency both
transactions read "no owner", both insert, and both commit — attributing one
content address to two projects.  A read is not a lock.

This revision adds ``artifactcontentowner`` — one row per content address,
primary key ``artifact_id`` — so the *database* admits exactly one owning
project.  ``SqlArtifactBindingStore.bind`` inserts this row inside a SAVEPOINT
to claim the address; a losing racer takes the primary-key violation and is
refused (``CrossProjectAccess``).  The table stores only ``(artifact_id,
project_id, created_at)`` — no bytes, no path, no revisions — so it is a
uniqueness/integrity structure, **not** a parallel artifact store.

Isolation contract (explicit, for review — "No hidden migration"):

* This revision creates exactly one table, ``artifactcontentowner``, with a
  foreign key to ``appproject`` (existing projects only, never modified) and a
  non-unique index on ``project_id``.  No other table, column or index of
  ``projectartifactbinding`` (or any other table) is altered.
* Existing per-job bindings are back-filled verbatim: each distinct content
  address gets one ownership row whose project is that of its earliest binding
  (``created_at``, tie-broken by ``job_id``).  If, before this revision, two
  projects had already (buggily) co-claimed one address, the earliest binding's
  project wins and is recorded — the historical anomaly is resolved
  deterministically rather than left ambiguous.  No binding row is dropped.
* ``downgrade`` drops ``artifactcontentowner``; ownership is then enforced
  again only by the (racy) store guard, exactly as before this revision.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20261109contentowner"
down_revision: str | Sequence[str] | None = "20261108perjobbind"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "artifactcontentowner"
_BINDING = "projectartifactbinding"

# One ownership row per content address: the address of its earliest binding
# (``created_at``, then the deterministic ``job_id`` tie-break) belongs to that
# binding's project.  Valid on both SQLite and PostgreSQL.
_BACKFILL = f"""
INSERT INTO {_TABLE} (artifact_id, project_id, created_at)
SELECT b.artifact_id, b.project_id, b.created_at
FROM {_BINDING} b
WHERE NOT EXISTS (
    SELECT 1 FROM {_BINDING} n
    WHERE n.artifact_id = b.artifact_id
      AND (n.created_at < b.created_at
           OR (n.created_at = b.created_at AND n.job_id < b.job_id))
)
"""


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("artifact_id", sa.String(), nullable=False),
        sa.Column("project_id", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["appproject.project_id"]),
        sa.PrimaryKeyConstraint("artifact_id"),
    )
    op.create_index(f"ix_{_TABLE}_project_id", _TABLE, ["project_id"], unique=False)
    op.create_index(f"ix_{_TABLE}_created_at", _TABLE, ["created_at"], unique=False)
    op.execute(_BACKFILL)


def downgrade() -> None:
    op.drop_index(f"ix_{_TABLE}_created_at", table_name=_TABLE)
    op.drop_index(f"ix_{_TABLE}_project_id", table_name=_TABLE)
    op.drop_table(_TABLE)
