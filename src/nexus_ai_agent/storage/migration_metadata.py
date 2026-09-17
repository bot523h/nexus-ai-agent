"""Aggregate SQLAlchemy metadata targets for Alembic autogenerate (D2).

The application currently models every table on :data:`sqlmodel.SQLModel.metadata`
(see :mod:`nexus_ai_agent.storage.models`).  Membership tools that arrive later
(V1) may keep their own ``MetaData`` object.  :data:`METADATA_TARGETS` is the
extension point for registering those additional targets, and
:func:`get_target_metadata` merges them into a single metadata object that
``migrations/env.py`` hands to Alembic.

Importing this module must produce the *effective* metadata the production
runtime sees.  Besides ``storage.db`` → ``storage.models``, this means importing
:mod:`nexus_ai_agent.features.referral`: the production import chain
(``bot.handlers``) loads that module, whose ``Referral``/``ReferralCode`` classes
re-declare the ``referral``/``referralcode`` tables with ``extend_existing=True``
and add the indexes/unique constraints the feature actually relies on.  Without
that import here, autogenerate would walk the pre-extension metadata and the
initial migration would knowingly omit 7 indexes.
"""

from __future__ import annotations

from sqlalchemy import MetaData
from sqlmodel import SQLModel

from nexus_ai_agent.features import referral as _referral  # noqa: F401
from nexus_ai_agent.storage import db as _db  # noqa: F401  (populates SQLModel.metadata)

#: The metadata objects Alembic should autogenerate against, in priority order.
#: For now only SQLModel.metadata; add one entry per new metadata owner.
METADATA_TARGETS: list[MetaData] = [SQLModel.metadata]


def get_target_metadata() -> MetaData:
    """Return the metadata object Alembic should target.

    A single registered target is returned as-is (avoiding an unnecessary
    shallow copy every run).  Multiple targets are merged into a fresh
    ``MetaData`` whose tables are added from each target (without copying the
    ``MetaData`` objects themselves), so autogenerate sees every table while
    staying isolated from future metadata the targets may carry.
    """
    if not METADATA_TARGETS:
        return MetaData()
    if len(METADATA_TARGETS) == 1:
        return METADATA_TARGETS[0]
    combined = MetaData()
    for target in METADATA_TARGETS:
        for table in target.tables.values():
            combined._add_table(table.name, table.schema, table)
    return combined
