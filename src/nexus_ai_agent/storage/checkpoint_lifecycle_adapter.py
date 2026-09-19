"""Async ``CheckpointLifecyclePort`` implementation over the SQLite index.

The store itself is a plain synchronous SQLite handle; this adapter makes it
usable from async runtime code (off-thread) and keeps destructive operations
behind their POST_V1 guard.  It never touches LangGraph's own tables.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import asdict
from datetime import datetime
from typing import Any

from nexus_ai_agent.application.ports.checkpoint_lifecycle import CheckpointLifecyclePort
from nexus_ai_agent.domain.glossary import POST_V1_CHECKPOINT_DELETION
from nexus_ai_agent.storage.checkpoint_lifecycle import CheckpointRecord, LifecycleStore


class SQLiteCheckpointLifecycleAdapter:
    """Lifecycle port adapter; satisfies :class:`CheckpointLifecyclePort`."""

    def __init__(self, store: LifecycleStore) -> None:
        self._store = store

    async def record_checkpoint(
        self, thread_id: str, checkpoint_id: str, *, created_at: datetime
    ) -> None:
        await asyncio.to_thread(
            self._store.upsert, CheckpointRecord(thread_id, checkpoint_id, created_at)
        )

    async def touch_thread(self, thread_id: str, *, accessed_at: datetime) -> None:
        await asyncio.to_thread(self._store.touch_thread, thread_id, accessed_at)

    async def inspect(self) -> Sequence[dict[str, Any]]:
        records = await asyncio.to_thread(self._store.records)
        return [asdict(record) for record in records]

    async def delete_thread(self, thread_id: str, *, idempotency_key: str) -> None:
        raise NotImplementedError(
            f"{POST_V1_CHECKPOINT_DELETION}: thread deletion is not in PR1/PR2"
        )

    async def schema_fingerprint(self) -> str:
        return await asyncio.to_thread(self._store.schema_fingerprint)

    def __repr__(self) -> str:
        return f"SQLiteCheckpointLifecycleAdapter(path={self._store.path!r})"


__all__ = ["CheckpointLifecyclePort", "SQLiteCheckpointLifecycleAdapter"]
