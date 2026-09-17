"""Lifecycle port; LangGraph's runtime saver remains the runtime owner."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol


class CheckpointLifecyclePort(Protocol):
    """Metadata and safe thread-level lifecycle operations only.

    Per-checkpoint deletion is deliberately absent: LangGraph delta chains
    make mid-chain surgery unsafe until a separately reviewed post-v1 design.
    """

    async def record_checkpoint(
        self, thread_id: str, checkpoint_id: str, *, created_at: datetime
    ) -> None: ...

    async def touch_thread(self, thread_id: str, *, accessed_at: datetime) -> None: ...

    async def inspect(self) -> Sequence[dict[str, object]]: ...

    async def delete_thread(self, thread_id: str, *, idempotency_key: str) -> None: ...

    async def schema_fingerprint(self) -> str: ...
