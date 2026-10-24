"""Pure value objects for the knowledge-retrieval subsystem.

No framework, no database, no LlamaIndex — this module is deliberately
dependency-free so the contract (source identity, citations, report shapes,
status) can be unit-tested and reasoned about on its own.  The port lives in
:mod:`nexus_ai_agent.knowledge_retrieval.ports`; the LlamaIndex adapter is the
*only* place ``llama_index`` may be imported (enforced by
``tests/architecture/test_knowledge_retrieval_boundary.py``).

Evidence vs interpretation
--------------------------
:class:`SearchResponse` carries **retrieved evidence only** — excerpts with
their provenance.  Nothing here is an answer, a verdict, or an authorization:
the application analyses evidence, authorizes separately and executes through
the CommandBus (see ``docs/architecture/KNOWLEDGE_RETRIEVAL.md`` §5).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

__all__ = [
    "ChunkPayload",
    "Evidence",
    "IngestReport",
    "IndexStatus",
    "QueryState",
    "SearchResponse",
    "SourceMeta",
    "SourceRecord",
    "SourceStatus",
]


class SourceStatus(str, Enum):
    """Stored lifecycle of one registered source (canonical database)."""

    INGESTED = "ingested"
    ERROR = "error"


class QueryState(str, Enum):
    """Defined, distinct outcomes of a search request."""

    OK = "ok"
    EMPTY_QUERY = "empty_query"
    EMPTY_INDEX = "empty_index"
    NO_MATCHES = "no_matches"


@dataclass(frozen=True, slots=True)
class SourceMeta:
    """Identity of one ingested source as handed to the index port."""

    source_id: str
    path: str  # display path, relative to its authorized root
    root: str  # display form of the resolved root
    sha256: str
    ingested_at: str  # ISO-8601 UTC


@dataclass(frozen=True, slots=True)
class ChunkPayload:
    """One retrieval window with its location inside the source text."""

    chunk_index: int
    start: int
    end: int
    text: str
    page: int | None = None  # 1-based page when the format provides one


@dataclass(frozen=True, slots=True)
class SourceRecord:
    """Registry row as read back by operators and tests."""

    source_id: str
    root: str
    path: str
    sha256: str
    size_bytes: int
    status: SourceStatus
    chunks: int
    indexed_at: str | None
    updated_at: str | None
    last_error: str | None


@dataclass(frozen=True, slots=True)
class Evidence:
    """One source-grounded excerpt with a resolvable citation.

    Every field is measured, never invented: ``sha256`` is the content hash
    actually indexed, ``start``/``end`` are character offsets into that exact
    content, ``page`` is ``None`` for formats without pages.
    """

    source_id: str
    path: str
    root: str
    sha256: str
    chunk_index: int
    start: int
    end: int
    score: float
    excerpt: str
    page: int | None = None

    @property
    def citation(self) -> str:
        """Stable, resolvable citation string (``path@sha256#chunk``)."""
        return f"{self.path}@{self.sha256[:12]}#{self.chunk_index}"


@dataclass(frozen=True, slots=True)
class SearchResponse:
    """Result of a search: retrieved evidence + explicit query state.

    ``state`` distinguishes the four defined outcomes so callers never have to
    guess whether an empty ``evidence`` list means "disabled", "nothing
    indexed", "no match" or "the query itself was empty".  Disabled and
    corrupt-index conditions are *errors*, not responses.
    """

    query: str
    state: QueryState
    evidence: tuple[Evidence, ...] = ()
    dropped_stale: int = 0
    #: Vector search always returns *some* nearest neighbour; hits with zero
    #: lexical overlap with the query are filtered out as unrelated (counted
    #: here) instead of being passed off as evidence.
    filtered_unrelated: int = 0
    index_generation: int | None = None


@dataclass(frozen=True, slots=True)
class IngestReport:
    """Outcome of one index/rebuild run (counters + safe per-file errors)."""

    run_id: str
    scanned: int = 0
    ingested: int = 0
    new: int = 0
    modified: int = 0
    unchanged: int = 0
    removed: int = 0
    unsupported: int = 0
    rejected: int = 0
    failed: int = 0
    chunks_written: int = 0
    truncated: bool = False
    #: False when any configured root's traversal did not complete (missing,
    #: unreadable, or walk error): registry evidence under those roots was
    #: preserved because absence could not be proven.
    scan_complete: bool = True
    incomplete_roots: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    duration_s: float = 0.0
    rebuilt: bool = False

    def as_dict(self) -> dict[str, object]:
        """JSON-serialisable view (CLI ``--json``, logs, evidence records)."""
        return {
            "run_id": self.run_id,
            "scanned": self.scanned,
            "ingested": self.ingested,
            "new": self.new,
            "modified": self.modified,
            "unchanged": self.unchanged,
            "removed": self.removed,
            "unsupported": self.unsupported,
            "rejected": self.rejected,
            "failed": self.failed,
            "chunks_written": self.chunks_written,
            "truncated": self.truncated,
            "scan_complete": self.scan_complete,
            "incomplete_roots": list(self.incomplete_roots),
            "errors": list(self.errors),
            "duration_s": round(self.duration_s, 3),
            "rebuilt": self.rebuilt,
        }


@dataclass(frozen=True, slots=True)
class IndexStatus:
    """Honest subsystem status; building it must never raise."""

    enabled: bool
    configured: bool
    store_present: bool
    store_corrupt: bool
    node_count: int | None
    generation: int | None
    source_count: int
    ingested_count: int
    error_count: int
    embedding_provider: str
    provider_available: bool
    degraded_reason: str | None = None
    index_path: str = ""

    @property
    def degraded(self) -> bool:
        return self.degraded_reason is not None

    def as_dict(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "configured": self.configured,
            "store_present": self.store_present,
            "store_corrupt": self.store_corrupt,
            "node_count": self.node_count,
            "generation": self.generation,
            "source_count": self.source_count,
            "ingested_count": self.ingested_count,
            "error_count": self.error_count,
            "embedding_provider": self.embedding_provider,
            "provider_available": self.provider_available,
            "degraded": self.degraded,
            "degraded_reason": self.degraded_reason,
            "index_path": self.index_path,
        }


@dataclass(frozen=True, slots=True)
class ScanIssue:
    """One file the scanner refused, with a safe machine-readable reason."""

    path: str  # relative display path (operator's own tree; never content)
    reason: str  # symlink | traversal | secret | unsupported | oversize | ...
