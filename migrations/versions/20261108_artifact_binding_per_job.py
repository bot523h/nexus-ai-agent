"""per-job artifact binding identity (task-282 Slice 2, F5/R4)

Revision ID: 20261108perjobbind
Revises: 20261107bindfence
Create Date: 2026-11-08 00:00:00.000000

Root cause (F5/R4).  ``projectartifactbinding`` was keyed on ``artifact_id``
(the content address) with a ``UNIQUE(project_id, artifact_id)`` constraint, so
two jobs that published byte-identical artifacts shared **one** provenance row.
``retract(artifact_id, job_id, attempt)`` deleted that shared row when the job
that happened to *own* it lost its terminal ``COMPLETED`` CAS — destroying the
provenance of the *other*, still-COMPLETED job.  Content de-duplication (the
SHA-256 bytes) is correct and stays in the Asset Library; it is the *provenance
record* that must be per producing job.

What this revision changes — exactly one table, ``projectartifactbinding``:

* the primary key becomes the pair ``(artifact_id, job_id)`` — one provenance
  row per producing job, so a retraction scoped to ``(job_id, attempt)`` can
  only ever delete that job's own row;
* the ``uq_projectartifactbinding_project_artifact`` unique constraint is
  dropped (it forbade the second job's row);
* the redundant ``ix_projectartifactbinding_job_id`` index is dropped (``job_id``
  is now part of the primary key).

Isolation contract (explicit, for review — "No hidden migration"):

* No other table, column or data row is touched.  ``appproject`` and
  ``projectrevision`` are referenced, never modified.
* **Existing rows are preserved verbatim.**  Every pre-existing binding already
  carries a ``job_id`` (``""`` for a caller outside the queue), so it simply
  becomes that job's row under the new key — no data is dropped or rewritten.
  A primary-key change cannot be expressed as an in-place ``ALTER`` on SQLite,
  so the table is recreated and its rows copied; the shape is identical on
  SQLite and PostgreSQL.
* Downgrade restores the pre-revision schema (``PRIMARY KEY(artifact_id)`` plus
  the unique constraint).  It is **lossy by construction**: the old key can only
  hold one row per content address, so when two jobs share bytes the earliest
  row (``created_at``, then ``job_id``) is kept and the duplicates are dropped.
  The downgrade documents that explicitly rather than failing.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlmodel.sql import sqltypes as sqlmodel_types

# revision identifiers, used by Alembic.
revision: str = "20261108perjobbind"
down_revision: str | Sequence[str] | None = "20261107bindfence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "projectartifactbinding"
_OLD = "projectartifactbinding_old"

_COLUMNS = (
    "artifact_id",
    "project_id",
    "revision_id",
    "job_id",
    "attempt",
    "artifact_kind",
    "size_bytes",
    "artifact_version_id",
    "created_at",
)

_READ_INDEXES = (
    "ix_projectartifactbinding_project_id",
    "ix_projectartifactbinding_revision_id",
    "ix_projectartifactbinding_artifact_version_id",
    "ix_projectartifactbinding_created_at",
)


def _create(primary_key: sa.PrimaryKeyConstraint, extra: tuple[sa.SchemaItem, ...]) -> None:
    op.create_table(
        _TABLE,
        sa.Column("artifact_id", sqlmodel_types.AutoString(), nullable=False),
        sa.Column("project_id", sqlmodel_types.AutoString(), nullable=False),
        sa.Column("revision_id", sqlmodel_types.AutoString(), nullable=False),
        sa.Column("job_id", sqlmodel_types.AutoString(), nullable=False, server_default=""),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "artifact_kind", sqlmodel_types.AutoString(), nullable=False, server_default="binary"
        ),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("artifact_version_id", sqlmodel_types.AutoString(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["appproject.project_id"]),
        sa.ForeignKeyConstraint(["revision_id"], ["projectrevision.revision_id"]),
        primary_key,
        *extra,
    )


def _copy_columns() -> str:
    joined = ", ".join(_COLUMNS)
    return f"INSERT INTO {_TABLE} ({joined}) SELECT {joined} FROM {_OLD}"


def upgrade() -> None:
    # ``job_id`` becomes part of the primary key, so its standalone index is
    # redundant.  Dropping it before the rebuild also avoids a name clash with
    # the indexes recreated below.
    op.drop_index("ix_projectartifactbinding_job_id", table_name=_TABLE)

    op.rename_table(_TABLE, _OLD)
    _create(sa.PrimaryKeyConstraint("artifact_id", "job_id"), ())
    # Every legacy row keeps its identity (and its ``job_id``): no row is lost.
    op.execute(_copy_columns())
    op.drop_table(_OLD)

    for name in _READ_INDEXES:
        column = name.removeprefix("ix_projectartifactbinding_")
        op.create_index(name, _TABLE, [column], unique=False)


def downgrade() -> None:
    for name in _READ_INDEXES:
        op.drop_index(name, table_name=_TABLE)

    op.rename_table(_TABLE, _OLD)
    _create(
        sa.PrimaryKeyConstraint("artifact_id"),
        (
            sa.UniqueConstraint(
                "project_id", "artifact_id", name="uq_projectartifactbinding_project_artifact"
            ),
        ),
    )
    # The pre-revision key can hold only one row per content address: keep the
    # earliest (``created_at``, tie-broken by ``job_id``) and drop the rest.
    op.execute(
        f"DELETE FROM {_OLD} WHERE EXISTS ("  # noqa: S608 - static, parameter-free DDL
        f"SELECT 1 FROM {_OLD} newer WHERE newer.artifact_id = {_OLD}.artifact_id "
        f"AND (newer.created_at < {_OLD}.created_at "
        f"OR (newer.created_at = {_OLD}.created_at AND newer.job_id < {_OLD}.job_id)))"
    )
    op.execute(_copy_columns())
    op.drop_table(_OLD)

    for name in ("ix_projectartifactbinding_revision_id", "ix_projectartifactbinding_project_id"):
        column = name.removeprefix("ix_projectartifactbinding_")
        op.create_index(name, _TABLE, [column], unique=False)
    op.create_index("ix_projectartifactbinding_job_id", _TABLE, ["job_id"], unique=False)
    for name in (
        "ix_projectartifactbinding_artifact_version_id",
        "ix_projectartifactbinding_created_at",
    ):
        column = name.removeprefix("ix_projectartifactbinding_")
        op.create_index(name, _TABLE, [column], unique=False)
