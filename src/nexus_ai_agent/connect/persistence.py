"""Persistence ports for CONNECT; concrete database adapters live in storage."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from nexus_ai_agent.connect.contracts import ConnectionContract
from nexus_ai_agent.connect.crypto import EncryptedSecret


@dataclass(frozen=True)
class PersistedConnection:
    connection: ConnectionContract
    envelope: EncryptedSecret
    row_version: int
    revoked_at: datetime | None
    updated_at: datetime


class ConnectionStore(Protocol):
    async def create(
        self, connection: ConnectionContract, secret: bytes
    ) -> PersistedConnection: ...
    async def get(self, connection_id: str) -> PersistedConnection | None: ...
    async def revoke(self, connection_id: str, *, expected_version: int) -> PersistedConnection: ...
