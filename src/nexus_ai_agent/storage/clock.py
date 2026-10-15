"""The project's single time contract (audit A-12, 2026-10-15).

Two representations exist on purpose and nothing else is allowed:

* **Stored** — every ``DateTime`` column in :mod:`nexus_ai_agent.storage.models`
  is a *naive* value that **means UTC** (``timestamp without time zone`` on
  PostgreSQL, ``DATETIME`` text on SQLite).  That is what every existing row
  already holds, so changing the column type would be a data migration for no
  product gain.  Use :func:`naive_utcnow` for defaults and comparisons against
  stored values.
* **In flight** — anything that is compared with "now" in memory, serialised
  to an API client, or logged should be *aware* UTC: :func:`utcnow`.

``datetime.utcnow()`` is banned (deprecated since Python 3.12 and it silently
mixes naive and aware values); ruff ``DTZ003`` rejects the call and
``tests/unit/test_time_contract.py`` rejects the attribute reference that
``default_factory=datetime.utcnow`` used to smuggle past the linter.

Converting between the two is explicit:

>>> as_naive_utc(datetime(2026, 1, 1, 12, tzinfo=timezone.utc))
datetime.datetime(2026, 1, 1, 12, 0)
>>> as_aware_utc(datetime(2026, 1, 1, 12))
datetime.datetime(2026, 1, 1, 12, 0, tzinfo=datetime.timezone.utc)
"""

from __future__ import annotations

from datetime import datetime, timezone

__all__ = ["as_aware_utc", "as_naive_utc", "naive_utcnow", "utcnow"]


def utcnow() -> datetime:
    """Aware current UTC time — the in-memory / wire representation."""
    return datetime.now(timezone.utc)


def naive_utcnow() -> datetime:
    """Naive current UTC time — exactly what the ``DateTime`` columns store."""
    return utcnow().replace(tzinfo=None)


def as_naive_utc(value: datetime) -> datetime:
    """Normalise ``value`` to the stored representation.

    Aware values are converted to UTC first (so an operator-supplied ``+03:30``
    timestamp is stored correctly); naive values are *assumed* UTC and returned
    unchanged, matching how the columns are read back.
    """
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def as_aware_utc(value: datetime) -> datetime:
    """Normalise ``value`` to the in-flight representation (aware UTC)."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
