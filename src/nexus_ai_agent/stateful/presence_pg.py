"""PostgreSQL-backed presence store — task-163, D-0010 / ADR 0005.

Presence is a *tier* of state: on the scale-to-zero path the bot container
dies after ~30 s of idleness, so "who is online" must be answerable by the
database, not by a process that no longer exists.

The key decision is the **server clock**: TTLs are computed and checked
entirely in PostgreSQL (``now() + make_interval(...)`` /
``online_until > now()``).  A container that wakes 40 minutes later reads
presence exactly the way the database says it is — no local clock, no
drift between two containers, no stale "online" flag that only a dead
process would have cleared.

The table ``nexus_presence`` is created by migration ``a41c9e2b7f63``
(PostgreSQL only; SQLite keeps the in-memory presence as before).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

__all__ = ["PRESENCE_TABLE", "PgPresenceStore", "build_presence_store"]

#: Canonical table name (owned by migration ``a41c9e2b7f63``; also part of
#: the Postgres "head state" set in ``storage/adopt_pg.py``).
PRESENCE_TABLE = "nexus_presence"


def _default_connector(url: str) -> Any:
    import psycopg  # noqa: PLC0415 — core dep, lazy like the adapters

    return psycopg.connect(url, autocommit=True)


def _is_transient(exc: BaseException) -> bool:
    """Connection-class failures worth one reconnect + retry.

    ``psycopg`` must be imported at module level (not
    ``from psycopg import OperationalError``) because the module import
    itself would fail in environments without the driver.
    """
    import psycopg

    return isinstance(exc, (psycopg.OperationalError, psycopg.InterfaceError))


class PgPresenceStore:
    """Presence with server-clock TTLs in PostgreSQL."""

    def __init__(
        self,
        database_url: str,
        *,
        default_ttl_seconds: float = 120.0,
        table: str = PRESENCE_TABLE,
        connection: Any | None = None,
        connector: Callable[[str], Any] | None = None,
    ) -> None:
        self._database_url = database_url
        self._default_ttl = float(default_ttl_seconds)
        self._table = table
        self._connector = connector or _default_connector
        self._closed = False
        # Lazy by design: construction must not require the database to be
        # reachable (the composition roots build engines before knowing the
        # pod's network state); the first statement connects.
        self._conn = connection

    def mark_online(self, user_id: int, ttl_seconds: float | None = None) -> None:
        """Upsert ``user_id`` as online; expiry is set by the *server* clock."""
        ttl = self._default_ttl if ttl_seconds is None else float(ttl_seconds)
        sql = (
            f"INSERT INTO {self._table} (user_id, online_until, updated_at) "
            f"VALUES (%s, now() + make_interval(secs => %s), now()) "
            f"ON CONFLICT (user_id) DO UPDATE "
            f"SET online_until = EXCLUDED.online_until, updated_at = now()"
        )
        self._execute(sql, (int(user_id), ttl))

    def is_online(self, user_id: int) -> bool:
        """True when the server clock says the TTL has not expired."""
        sql = f"SELECT online_until > now() FROM {self._table} WHERE user_id = %s"
        row = self._execute(sql, (int(user_id),))
        if row is None:
            return False
        return bool(row[0])

    def count_online(self) -> int:
        """How many users the server clock currently considers online."""
        sql = f"SELECT count(*) FROM {self._table} WHERE online_until > now()"
        row = self._execute(sql, ())
        return int(row[0]) if row else 0

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        conn, self._conn = self._conn, None
        if conn is None:
            return
        try:
            conn.close()
        except Exception:  # noqa: BLE001 — shutdown path: never raise
            logger.debug("presence close failed (already gone?)", exc_info=True)

    def _execute(self, sql: str, params: tuple[Any, ...]) -> Any:
        """Run one statement; on a transient failure, reconnect once and retry.

        Scale-to-zero wakes present exactly this way: the previous
        connection is long dead, the database is fine.  One reconnect keeps
        the store usable without a full retry storm.
        """
        try:
            return self._execute_noreconnect(sql, params)
        except Exception as exc:
            if not _is_transient(exc) or self._closed:
                raise
            logger.warning("presence connection lost — reconnecting once", exc_info=True)
            self._reconnect()
            return self._execute_noreconnect(sql, params)

    def _execute_noreconnect(self, sql: str, params: tuple[Any, ...]) -> Any:
        if self._conn is None:
            if self._closed:
                raise RuntimeError("presence store is closed")
            self._conn = self._connector(self._database_url)
        cur = self._conn.execute(sql, params)
        if cur.description is not None:
            return cur.fetchone()
        return None

    def _reconnect(self) -> None:
        try:
            if self._conn is not None:
                self._conn.close()
        except Exception:  # noqa: BLE001 — the old handle is what we are replacing
            pass
        self._conn = self._connector(self._database_url)


def build_presence_store(settings: Any) -> PgPresenceStore | None:
    """Composition-time builder: ``None`` when not on the PostgreSQL path."""
    url = getattr(settings, "database_url", None)
    if not url:
        return None
    return PgPresenceStore(str(url))
