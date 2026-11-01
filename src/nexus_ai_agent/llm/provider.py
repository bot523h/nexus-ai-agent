from __future__ import annotations

from abc import ABC, abstractmethod

# Explicit, type-safe principal for system/background work (summarization,
# knowledge processing, retrieval-free fallbacks).  ``0`` is NEVER an identity:
# the old ``user_id=0`` sentinel collapsed every caller into one shared quota
# bucket.  Real users keep their positive ids; identity-less work is metered
# under this constant, never under a user.
SYSTEM_PRINCIPAL_ID: int = -1


class LLMProvider(ABC):
    @abstractmethod
    async def generate(self, prompt: str, system: str = "", *, user_id: int | None = None) -> str:
        """Generate a completion.

        ``user_id`` is the authenticated human identity behind the request —
        it is threaded to whatever quota/accounting seam the implementation
        has.  Pass ``None`` only for genuine system/background work (metered
        as :data:`SYSTEM_PRINCIPAL_ID`).  An explicit ``0`` is not an identity
        and must be rejected — it is the removed shared-bucket sentinel.
        """
        raise NotImplementedError

    @abstractmethod
    async def embed(self, text: str) -> list[float]:
        raise NotImplementedError
