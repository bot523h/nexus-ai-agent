from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class PresenceStore:
    """In-memory TTL presence tracker.

    Presence is updated only when Telegram commands/messages arrive or when an
    application-managed heartbeat calls mark_online. There is no polling loop in
    this module, so it remains battery-friendly and cheap to run.
    """

    ttl_seconds: float = 120.0
    _online_until: dict[int, float] = field(default_factory=dict)

    def mark_online(self, user_id: int, *, ttl_seconds: float | None = None) -> None:
        ttl = self.ttl_seconds if ttl_seconds is None else ttl_seconds
        self._online_until[int(user_id)] = time.monotonic() + ttl

    def mark_offline(self, user_id: int) -> None:
        self._online_until.pop(int(user_id), None)

    def is_online(self, user_id: int) -> bool:
        uid = int(user_id)
        expires_at = self._online_until.get(uid)
        if expires_at is None:
            return False
        if expires_at <= time.monotonic():
            self._online_until.pop(uid, None)
            return False
        return True


_DEFAULT_STORE = PresenceStore()


def mark_online(user_id: int, *, ttl_seconds: float | None = None) -> None:
    _DEFAULT_STORE.mark_online(user_id, ttl_seconds=ttl_seconds)


def mark_offline(user_id: int) -> None:
    _DEFAULT_STORE.mark_offline(user_id)


def is_online(user_id: int) -> bool:
    return _DEFAULT_STORE.is_online(user_id)
