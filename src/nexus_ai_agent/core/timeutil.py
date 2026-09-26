"""One clock for the whole system — deprecation-free and mixing-proof.

Why this module exists
----------------------
Two independent defects lived side by side in the tree:

1. ``datetime.utcnow()`` is **deprecated since Python 3.12** and scheduled for
   removal.  The repository declares ``requires-python = ">=3.10"`` and runs a
   blocking 3.10/3.11/3.12 CI matrix, so the call site count (13 at the time of
   this module's introduction) was a removal-day outage waiting to happen.
2. ``datetime.utcnow()`` returns a **naive** datetime while newer code writes
   ``datetime.now(timezone.utc)`` (**aware**).  Both spellings were persisted
   into the *same* SQLModel columns.  SQLite/SQLModel stores a ``DateTime``
   column without a timezone, so an aware value written today reads back naive
   tomorrow — and ``naive <= aware`` raises
   ``TypeError: can't compare offset-naive and offset-aware datetimes`` at
   runtime.  ``ModerationEngine.is_muted`` did exactly that.

The rule this module encodes
----------------------------
* **Persisted columns are naive UTC.**  Use :func:`utcnow` for every
  ``default_factory`` and every value written to a ``datetime`` column.
* **Values read back from a column are normalised** with :func:`as_naive_utc`
  before they are compared, because a row may still hold an aware value
  written by an older release (or by PostgreSQL, whose ``timestamptz`` does
  round-trip the offset).
* **Aware UTC is available** as :func:`utcnow_aware` for APIs that require it
  (HTTP dates, external protocols) — it is never persisted.

Pure stdlib, no I/O, no project imports: importable from any layer.
"""

from __future__ import annotations

from datetime import datetime, timezone

__all__ = ["as_naive_utc", "utcnow", "utcnow_aware"]


def utcnow_aware() -> datetime:
    """Current UTC instant, timezone-aware. Never store this in a column."""
    return datetime.now(timezone.utc)


def utcnow() -> datetime:
    """Current UTC instant as a **naive** datetime — the persisted spelling.

    Byte-for-byte the value ``datetime.utcnow()`` produced, without the
    deprecation.  Kept naive on purpose: every ``datetime`` column in
    :mod:`nexus_ai_agent.storage.models` is naive, and mixing the two
    spellings in one column is what produced the ``is_muted`` crash.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


def as_naive_utc(value: datetime | None) -> datetime | None:
    """Normalise *value* to naive UTC so it can be compared with :func:`utcnow`.

    ``None`` passes through.  An aware value is converted to UTC and stripped
    of its offset; a naive value is assumed to already be UTC and returned
    unchanged.  This is the read-side counterpart of :func:`utcnow` and the
    single place that has to know the convention.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)
