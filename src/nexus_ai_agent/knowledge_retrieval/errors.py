"""Typed failures for the knowledge-retrieval subsystem.

Every error a caller can face has one class, one stable ``code`` and an
operator-safe message: no stack traces, no raw document content, no
filesystem details beyond what the operator configured.  The CLI maps
``code`` to an exit status; tests assert on the class.

Failure taxonomy (mirrors ``docs/architecture/KNOWLEDGE_RETRIEVAL.md`` §7):

* :class:`KnowledgeDisabledError`       — the subsystem is switched off.
* :class:`KnowledgeConfigError`         — missing/invalid configuration
  (no authorized source roots, a root that does not exist, bad limits).
* :class:`SourceRejectedError`          — a path tried to escape its
  authorized root (traversal/symlink); raised at registration time, while
  per-file rejections during a scan are *recorded* in the ingest report.
* :class:`IndexUnavailableError`        — the derived index or its
  embedding provider cannot serve the operation (corrupt store, missing
  provider); includes the recovery hint.
* :class:`KnowledgeStoreError`          — the canonical database failed.
"""

from __future__ import annotations

__all__ = [
    "IndexUnavailableError",
    "KnowledgeConfigError",
    "KnowledgeDisabledError",
    "KnowledgeRetrievalError",
    "KnowledgeStoreError",
    "SourceRejectedError",
]


class KnowledgeRetrievalError(Exception):
    """Base class for every typed knowledge-retrieval failure."""

    code: str = "knowledge_error"

    def __init__(self, message: str, *, hint: str = "", code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint
        if code is not None:
            # Per-instance refinement of the class-level stable code (e.g. a
            # typed ``IndexUnavailableError`` whose cause is the embedding
            # provider rather than the store on disk).
            self.code = code

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.message


class KnowledgeDisabledError(KnowledgeRetrievalError):
    """The subsystem is explicitly disabled (``NEXUS_KNOWLEDGE_ENABLED=false``)."""

    code = "disabled"


class KnowledgeConfigError(KnowledgeRetrievalError):
    """Configuration is missing or invalid; the operation fails closed."""

    code = "misconfigured"


class SourceRejectedError(KnowledgeRetrievalError):
    """A source path is outside its authorized root (traversal / symlink escape)."""

    code = "source_rejected"


class IndexUnavailableError(KnowledgeRetrievalError):
    """The derived index or embedding provider cannot serve the operation."""

    code = "index_unavailable"


class KnowledgeStoreError(KnowledgeRetrievalError):
    """The canonical database (source registry) failed."""

    code = "store_error"
