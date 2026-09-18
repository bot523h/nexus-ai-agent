"""Provider-neutral attachment storage port."""

from __future__ import annotations

from typing import Protocol


class ObjectStoragePort(Protocol):
    async def put(self, *, key: str, content: bytes, idempotency_key: str) -> str: ...

    async def delete(self, *, key: str, idempotency_key: str) -> None: ...
