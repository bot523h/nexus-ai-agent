"""Local-first knowledge retrieval over explicitly authorized source roots.

The subsystem in one paragraph: configured roots are scanned under strict
allowlist/exclusion rules, every accepted file is hashed and chunked with the
existing deterministic chunker, and the chunks are indexed through the
:class:`~nexus_ai_agent.knowledge_retrieval.ports.KnowledgeIndexPort` — the
shipping implementation of which is the LlamaIndex adapter.  The canonical
source registry lives in the application database (table
``knowledgesource``); the LlamaIndex store under ``data/knowledge_index/``
is derived state that a restart, a crash or an explicit rebuild can always
recreate.  Search returns source-grounded evidence with sha256-verified
citations and never an interpretation: analysis, authorization and execution
belong to the application (see ``docs/architecture/KNOWLEDGE_RETRIEVAL.md``).

Public surface: :class:`KnowledgeRetrievalService` (operations), the value
objects in :mod:`.models`, the port in :mod:`.ports`, the configuration in
:mod:`.config`, and the typed failures in :mod:`.errors`.  Composition roots
(``cli.py``) wire the service to an index adapter — this package never
imports an adapter itself (see ``tests/architecture/
test_knowledge_retrieval_boundary.py``).
"""

from __future__ import annotations

from nexus_ai_agent.knowledge_retrieval.config import KnowledgeConfig
from nexus_ai_agent.knowledge_retrieval.errors import (
    IndexUnavailableError,
    KnowledgeConfigError,
    KnowledgeDisabledError,
    KnowledgeRetrievalError,
    KnowledgeStoreError,
    SourceRejectedError,
)
from nexus_ai_agent.knowledge_retrieval.models import (
    Evidence,
    IndexStatus,
    IngestReport,
    QueryState,
    SearchResponse,
    SourceRecord,
    SourceStatus,
)
from nexus_ai_agent.knowledge_retrieval.ports import (
    IndexHit,
    KnowledgeIndexPort,
    StoreInfo,
)
from nexus_ai_agent.knowledge_retrieval.service import KnowledgeRetrievalService

__all__ = [
    "Evidence",
    "IndexHit",
    "IndexStatus",
    "IndexUnavailableError",
    "IngestReport",
    "KnowledgeConfig",
    "KnowledgeConfigError",
    "KnowledgeDisabledError",
    "KnowledgeIndexPort",
    "KnowledgeRetrievalError",
    "KnowledgeRetrievalService",
    "KnowledgeStoreError",
    "QueryState",
    "SearchResponse",
    "SourceRecord",
    "SourceRejectedError",
    "SourceStatus",
    "StoreInfo",
]
