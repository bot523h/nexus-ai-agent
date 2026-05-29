from __future__ import annotations

import time

from nexus_ai_agent.presence import PresenceStore


def test_presence_mark_online_offline() -> None:
    store = PresenceStore(ttl_seconds=10)
    store.mark_online(123)
    assert store.is_online(123) is True
    store.mark_offline(123)
    assert store.is_online(123) is False


def test_presence_ttl_expiry() -> None:
    store = PresenceStore(ttl_seconds=0.01)
    store.mark_online(123)
    time.sleep(0.02)
    assert store.is_online(123) is False
