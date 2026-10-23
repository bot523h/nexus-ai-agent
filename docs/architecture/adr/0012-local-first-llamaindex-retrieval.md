---
status: accepted
date: 2026-10-23
deciders: agent session arena/01c872e0-nexus-ai-agent (board task-273-llamaindex-retrieval)
consulted: AGENTS.md protocol v2, docs/architecture/MODULE_MAP.md
informed: future per-project knowledge scoping work
---

# 0012. Local-first LlamaIndex behind a narrow port, with SQLite as the only authority

## Context and Problem Statement

NEXUS needs evidence-grounded retrieval over operator-allowlisted local sources (board
`task-273`). The existing neighbours — `features/rag.py` (per-user Chroma uploads) and
`knowledge/` (provenance-first web cache) — do not provide authorized-root registration,
hash-based change detection, incremental reindex, stale cleanup, versioned citations or
rebuild operations. The mission constrains the shape: local-first (no paid services, no
telemetry, no silent downloads), LlamaIndex confined to ingestion/indexing/retrieval, and
SQLite remaining the single authority.

## Decision Drivers

- **Isolation**: the core domain must not grow a framework dependency; a fitness function
  must be able to prove confinement mechanically.
- **Local-first determinism**: tests and default operations run offline, without keys.
- **Recoverability**: any derived state must be rebuildable from authority + sources.
- **House conventions**: composition in `cli.py`, typed errors, structured redaction-aware
  logs, Alembic-managed tables, docs updated in the same change.

## Considered Options

1. **Pinned `llama-index-core` behind `KnowledgeIndexPort`, adapter-only import** (chosen).
2. Extend `features/rag_core.py` BM25 only — no LlamaIndex at all (fails the mission's
   integration requirement and the vector-store persistence contract).
3. Managed vector store / hosted embeddings (Chroma cloud, OpenAI) — fails local-first.

## Decision Outcome

Chosen option: **1**, because it satisfies every driver at once: `llama-index-core==0.14.25`
(MIT, pinned as a core dependency, no LLM extras) is quarantined in
`src/nexus_ai_agent/adapters/llamaindex_retrieval.py`, the core package talks only the port,
and `cli.py` is the one composition root. The hash embedding provider makes the default path
pure arithmetic; `sentence-transformers` is opt-in behind an existing-local-directory rule.
Persistence is the LlamaIndex storage context under `NEXUS_KNOWLEDGE_INDEX_PATH`, treated as
derived state, with `knowledgesource` (SQLite, migration `d4e1c0a79b21`) as authority.

### Consequences

- Positive: confinement, offline tests, rebuild-from-authority recovery, no new API/UI
  surface (CLI ops through the existing command layer).
- Negative/negative-risk: the lexical-overlap guard prefers precision over recall;
  sentence-transformers users must prefetch a model once; per-project scoping is deferred.
- Rejected: option 2 (no vector persistence contract), option 3 (paid/networked).

## Confirmation

- `tests/architecture/test_knowledge_retrieval_boundaries.py` (llama_index confined to the
  adapter; core package imports neither adapters nor the framework; single wiring site).
- `tests/architecture/test_import_boundaries.py` (legacy baseline untouched).
- `tests/unit/test_knowledge_retrieval_identity.py`,
  `tests/unit/test_knowledge_retrieval_service.py`,
  `tests/unit/test_knowledge_retrieval_cli.py`,
  `tests/integration/test_knowledge_retrieval_pipeline.py` (lifecycle, stale-drop,
  corrupt-rebuild, fail-closed security, no-network/no-LLM).
- Gates: `make lint types test` with the editable install (AGENTS.md local-gate rule).

## Links

- Living view: [`../KNOWLEDGE_RETRIEVAL.md`](../KNOWLEDGE_RETRIEVAL.md)
- Operational semantics: [`../../DECISION_LOG.md`](../../DECISION_LOG.md) entry D-0029
- Board claim: `.agents/board.json` → `task-273-llamaindex-retrieval`
