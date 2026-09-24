"""nexus_presence + nexus_job_queue_pg (PostgreSQL only)

Revision ID: a41c9e2b7f63
Revises: 7c2f9d41e8a3
Create Date: 2026-09-23 00:00:00.000000

task-163 (D-0010 / ADR 0005): the scale-to-zero state tiers.  Only
security-relevant state leaves the scale-to-zero container; the two
tiers that must survive a Koyeb kill are:

* ``nexus_presence`` — presence with *server-clock* TTLs
  (``online_until``/``updated_at`` as ``timestamptz``; the store sets
  and checks them with ``now() + make_interval(...)`` /
  ``online_until > now()``, so two containers never disagree about who
  is online and a container waking 40 minutes later reads the truth).
* ``nexus_job_queue_pg`` — the application-owned job queue with
  PostgreSQL as the durable hand-off point.  It mirrors the SQLite
  queue (``adapters/in_process_job_queue.py``) one-for-one — same
  columns, same TEXT ISO-8601 timestamps — plus ``owner_id`` and
  ``locked_at`` (the claim/steal protocol: ``FOR UPDATE SKIP LOCKED``
  claims, stale-steal after a 900 s processing timeout, so a killed
  container's rows return to ``pending`` and are never
  double-executed).  ``ix_nexus_job_queue_pg_claim`` is the claim
  index (oldest-first ``pending`` scan).

Isolation contract (explicit, for review — "No hidden migration"):

* This revision creates exactly two tables and one index, and nothing
  else.  No LangGraph table is ever Alembic-owned, and this revision
  never touches them.  No other DDL anywhere in the branch accompanies
  this revision.
* PostgreSQL dialect only: on SQLite both tiers keep their legacy
  stores (in-memory presence, SQLite queue), which keeps the shared
  chain ORM-exact on SQLite and ``alembic check`` clean.
* A database stamped at this head therefore *must* contain both
  tables; ``nexus adopt-pg`` treats a legacy database without them as
  drift (fail-fast, explicit operator fix — never implicit repair).

No hidden migration. No hidden mutation. No implicit repair.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a41c9e2b7f63"
down_revision: str | Sequence[str] | None = "7c2f9d41e8a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _is_postgresql() -> bool:
    return op.get_context().dialect.name == "postgresql"


def upgrade() -> None:
    if not _is_postgresql():
        return
    op.create_table(
        "nexus_presence",
        sa.Column("user_id", sa.BigInteger(), primary_key=True),
        sa.Column("online_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "nexus_job_queue_pg",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("job_type", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("owner_id", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("locked_at", sa.Text(), nullable=True),
        sa.Column("started_at", sa.Text(), nullable=True),
        sa.Column("finished_at", sa.Text(), nullable=True),
        sa.UniqueConstraint("idempotency_key"),
    )
    op.create_index(
        "ix_nexus_job_queue_pg_claim",
        "nexus_job_queue_pg",
        ["status", "created_at"],
    )


def downgrade() -> None:
    if not _is_postgresql():
        return
    op.drop_index("ix_nexus_job_queue_pg_claim", table_name="nexus_job_queue_pg")
    op.drop_table("nexus_job_queue_pg")
    op.drop_table("nexus_presence")
