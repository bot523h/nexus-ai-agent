"""The narrow port between the knowledge domain and any index backend.

This is the only contract the application layer sees: upsert/remove one
source atomically, search, clear, inspect.  The shipping implementation is
the LlamaIndex adapter (:mod:`nexus_ai_agent.adapters.llamaindex_retrieval`);
tests inject deterministic fakes.  Keeping the port here (inside the
subpackage, like the Asset Library's ``ports.py``) means
:mod:`nexus_ai_agent.knowledge_retrieval` never imports an adapter or a
third-party index class — law R1 applies to it through
``tests/architecture/test_knowledge_retrieval_boundary.py``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from nexus_ai_agent.knowledge_retrieval.models import ChunkPayload, SourceMeta

__all__ = [
    "IndexHit",
    "KnowledgeIndexPort",
    "StoreInfo",
]


@dataclass(frozen=True, slots=True)
class IndexHit:
    """One raw index hit, before registry verification builds a citation.

    ``sha256``/``path`` are the values the source had **when it was indexed**;
    the service re-checks them against the canonical registry before any hit
    becomes evidence, which is what keeps stale index entries from being
    presented as current.
    """

    source_id: str
    sha256: str
    path: str
    chunk_index: int
    start: int
    end: int
    score: float
    text: str
    page: int | None = None


@dataclass(frozen=True, slots=True)
class StoreInfo:
    """Physical state of the derived index store (never raises to build)."""

    present: bool
    corrupt: bool
    node_count: int | None
    generation: int | None
    rebuilt_at: str | None
    load_error: str = ""
    #: Embedding-space identity recorded in the store manifest at build time.
    embedding_model: str | None = None
    #: True when the store was built with a different embedding space than
    #: the currently configured provider/model — search and incremental
    #: indexing must refuse (rebuild required) instead of mixing vectors.
    model_mismatch: bool = False


@runtime_checkable
class KnowledgeIndexPort(Protocol):
    """Async index operations; every mutation of one source is atomic."""

    async def replace_source(self, source: SourceMeta, chunks: Sequence[ChunkPayload]) -> int:
        """(Re-)index one source, dropping any previous version of it.

        Returns the number of indexed chunks.  Idempotent: replacing with the
        same content yields the same store state.
        """

    async def remove_source(self, source_id: str) -> bool:
        """Remove every indexed chunk of one source. ``True`` if it existed."""

    async def search(self, query: str, top_k: int) -> list[IndexHit]:
        """Ranked hits for a non-empty query (``[]`` when nothing matches)."""

    async def clear(self) -> None:
        """Drop the entire derived store (explicit rebuild only)."""

    async def inspect(self) -> StoreInfo:
        """Report store presence/corruption/size without raising."""
