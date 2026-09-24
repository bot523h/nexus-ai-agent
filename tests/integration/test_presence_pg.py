"""Integration: the scale-to-zero tier for presence (PostgreSQL).

Presence TTLs are owned by the *server* clock: this test proves the
cross-instance contract (store A marks online, store B — a different
connection, as a different container would be — reads it) and the
server-clock expiry (a 1 s TTL expires by the database's own ``now()``,
not by any local clock).

Skipped unless ``NEXUS_DATABASE_URL`` is set (CI: the ``migrate-postgres``
job provides a local pgvector/pg16 service container).
"""

from __future__ import annotations

import os
import time

import pytest

from nexus_ai_agent.stateful import PgPresenceStore

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("NEXUS_DATABASE_URL"), reason="NEXUS_DATABASE_URL not set"
    ),
]

_URL = os.environ.get("NEXUS_DATABASE_URL", "")
_UID = 999001


def test_presence_survives_across_instances_and_expires_by_server_clock() -> None:
    store_a = PgPresenceStore(_URL)
    try:
        store_a.mark_online(_UID, ttl_seconds=1)
        assert store_a.is_online(_UID) is True

        # A different connection (a different container) reads the same truth.
        store_b = PgPresenceStore(_URL)
        try:
            assert store_b.is_online(_UID) is True
            assert store_b.count_online() >= 1
        finally:
            store_b.close()

        # The database's own clock expires the 1 s TTL.
        time.sleep(1.2)
        assert store_a.is_online(_UID) is False
    finally:
        store_a.close()


def test_unknown_user_is_offline() -> None:
    store = PgPresenceStore(_URL)
    try:
        assert store.is_online(999999) is False
    finally:
        store.close()
