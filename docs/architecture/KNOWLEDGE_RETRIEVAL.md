# Knowledge Retrieval (LlamaIndex): local-first, evidence-grounded, rebuildable

V1 of the knowledge-retrieval capability (board `task-273-llamaindex-retrieval`): controlled
ingestion of operator-allowlisted source roots into a **derived** LlamaIndex vector store, and
evidence-grounded search whose citations resolve to bytes the canonical database vouches for.
LangGraph stays the orchestrator; SQLite stays authoritative; retrieval never authorizes
anything — it only produces evidence.

<!-- truth:file=src/nexus_ai_agent/knowledge_retrieval/service.py -->
<!-- truth:file=src/nexus_ai_agent/adapters/llamaindex_retrieval.py symbol=LlamaIndexKnowledgeIndex -->
<!-- truth:file=tests/architecture/test_knowledge_retrieval_boundaries.py symbol=test_llama_index_imports_exist_only_in_the_adapter -->

## 1. Scope of V1

- **In**: allowlisted roots (`NEXUS_KNOWLEDGE_SOURCE_ROOTS`), UTF-8 text
  (`.md`/`.markdown`/`.txt`/`.rst`) + `.pdf` when the existing `pypdf` extra is installed,
  chunking, content hashes, incremental reindex, stale cleanup, path-traversal/symlink/secret
  rejection, evidence search with verified citations, status/rebuild/remove — all behind the
  existing operator interfaces (CLI; no new UI, no new API routes).
- **Out**: web crawling, remote parsers (LlamaParse), per-project ACLs (V1 is operator-level),
  hybrid reranking, a second database. `features/rag.py` (per-user Chroma uploads) and
  `knowledge/` (provenance-first web cache) are unchanged neighbours, not dependencies.

## 2. Boundaries and laws

The core package `knowledge_retrieval/` speaks ports only
(`KnowledgeIndexPort` in `ports.py`); the single implementation lives in
`src/nexus_ai_agent/adapters/llamaindex_retrieval.py`, and the single composition root that
wires settings → config → adapter → service is the `knowledge` sub-app in `cli.py`.

- LlamaIndex may be imported **only** by the adapter module.
- The core package may import **neither** `llama_index` nor `nexus_ai_agent.adapters`.
- Retrieval imports no orchestration/authority module (no LangGraph, no command bus).

These are fitness functions, not prose: `tests/architecture/test_knowledge_retrieval_boundaries.py`
plus the legacy-baseline sweep in `tests/architecture/test_import_boundaries.py`
(law **R22** in [`MODULE_MAP.md`](MODULE_MAP.md)).

## 3. Write path (index / rebuild)

```
scan (symlink/traversal/secret/oversize guards)
  → read bytes → sha256 → chunk (features.rag_core.chunk_text, 384 tokens / 0.15 overlap)
  → port.replace_source(id, chunks)   # index write FIRST (derived, crash-safe)
  → registry.upsert_ingested(...)     # DB commit SECOND (authority)
```

- A crash between the two steps leaves an uncommitted derived state; the next run sees the
  registry without that sha and re-ingests — convergent, never half-trusted.
- `replace_source` is delete-then-add per source id, so a modified file never leaves its old
  version retrievable. Registry rows for files that vanished are dropped (`removed` counter).
- `rebuild` = wipe index + registry rows, re-ingest everything, generation + 1. It is the
  recovery tool for every store-level corruption (§7).

## 4. Sources, formats, and the model prefetch rule

Text formats are parsed unconditionally (UTF-8, deterministic). PDF is opportunistic: if the
already-declared `pypdf` extra is importable, pages are extracted and each chunk carries a
1-based `page`; otherwise `.pdf` files count as `unsupported` — a missing extra never fails a
run. Chunk offsets are positions in the extracted text; citations slice the real content.

Embeddings default to the **hash provider** (256-dim blake2b over `rag_core` tokens): pure
arithmetic, deterministic, zero network. The opt-in `sentence-transformers` provider requires
an **existing local model directory** — a missing directory is a typed
`IndexUnavailableError` (`code="embedding"`), never an implicit download. Prefetch once:

```bash
python -c "from sentence_transformers import SentenceTransformer; \
SentenceTransformer('all-MiniLM-L6-v2').save('models/minilm')"
export NEXUS_KNOWLEDGE_EMBEDDING_PROVIDER=sentence-transformers
export NEXUS_KNOWLEDGE_EMBEDDING_MODEL_PATH=$PWD/models/minilm
```

Telemetry is disabled for the process (`OTEL_SDK_DISABLED=true` set by the adapter module),
and `llama-index-llms-*` is not a dependency: retrieval never calls an LLM.

## 5. Search semantics (no fabricated citations)

States are explicit — `ok`, `empty_query`, `empty_index`, `no_matches` — and errors
(disabled/corrupt/provider) are typed exceptions, not states.

1. A vector store always returns *some* nearest neighbour. Hits are kept only if they share
   at least one **distinctive token** (≥ 4 chars, same tokenizer as the embedder) with the
   query; zero-overlap neighbours are counted as `filtered_unrelated` and dropped. Queries
   with no distinctive token answer `no_matches`.
2. Every surviving hit is checked against the registry: missing row, non-`ingested` status,
   or sha256 mismatch ⇒ hit dropped (`dropped_stale`). Stale content is never served as
   current.
3. Evidence carries `source_id`, `path`, `root`, `sha256`, `chunk_index`, `start`/`end`,
   `page`, `score`, verbatim `excerpt`, and `citation = path@sha12#chunk`. The response has
   no trust/verification/authority field a document could fake: retrieved text is untrusted
   data, and search performs no authorization (the CLI path never reaches a command bus).

## 6. Persistence and derived state

The store lives under `NEXUS_KNOWLEDGE_INDEX_PATH` (marker `index_store.json`, generation
stamps **and the embedding-space identity** in `manifest.json`). The recorded identity
mirrors the provider's `model_name` (`nexus-hash-v1-256dim`, `sentence-transformers:<dir>`):
switching provider or model directory means the stored vectors belong to a *different
embedding space*, so inspection reports `embedding_model_changed_rebuild_required`,
`search` and plain `index` refuse with `error[rebuild_required]` (no mixed spaces, no
partial re-ingest), and `nexus knowledge rebuild --yes` is the explicit recovery. It is **derived state**: deleting the directory and running
`nexus knowledge index` (or `rebuild`) recreates it from SQLite + the source roots. SQLite
(`knowledgesource`, migration `d4e1c0a79b21`) is the authority for identity, hashes, status
and counts.

## 7. Failure taxonomy

| Error | `code` | Meaning | Operator action |
|---|---|---|---|
| `KnowledgeDisabledError` | `disabled` | `NEXUS_KNOWLEDGE_ENABLED=false` | enable if intended; `status` still answers |
| `KnowledgeConfigError` | `misconfigured` | no/invalid roots, bad limits, unknown provider | fix env, rerun |
| `IndexUnavailableError` | `index_unavailable` / `embedding` | corrupt store, missing embedder/model dir | `nexus knowledge rebuild` / prefetch model |
| `KnowledgeStoreError` | `store_error` | canonical database failure | inspect DB; registry and index converge on next run |
| `SourceRejectedError` | `source_rejected` | registration-time escape attempt | fix the path |

Per-file ingestion failures do not kill a run: they land in the report (`failed`, capped
`errors`) and the registry row becomes `error` so `status` shows them. The CLI prints
`error[code]: message` + `hint` on stderr and exits 1 — never a stack trace. `status` itself
never raises; unexpected inspection failures surface as `degraded_reason`
(`index_store_corrupt`, `index_missing_rebuild_required`, `index_inspect_failed`, provider
reasons such as `embedding_model_directory_missing`).

## 8. Security boundaries

- Roots are canonicalised; files outside them, symlinks, `.env`/`*id_rsa*`/`*.pem`-style
  names and oversize inputs are rejected during the scan (counted, listed in `errors`).
- Excerpts are never logged (only `query_chars` and counters); structlog redaction applies.
- No network in the default path: no OpenAI, no paid parsers, no model downloads, no
  telemetry — enforced by `test_no_network_and_no_llm_stack_during_operations`.

## 9. Observability

Events follow the house convention (`get_logger(__name__)`, kwargs, redacted):
`knowledge_index_run`, `knowledge_search`, `knowledge_index_rebuild`,
`knowledge_source_unregistered`. CLI stdout stays `--json`-clean; logs go to stderr.

## 10. Costs and network needs

Local-first: with the default hash provider the capability adds no external service, key or
per-query cost. The only optional network event is the operator's one-time model prefetch
(§4). Disk: the store is proportional to chunks; `NEXUS_KNOWLEDGE_MAX_FILES` bounds a run.

## 11. Honest limitations

- V1 authorization is operator-level (CLI); there is no per-project scoping for knowledge
  sources yet, and no API routes (adding one requires the fail-closed inventory test).
- The lexical-overlap guard trades recall for precision: legitimate paraphrases that share
  no distinctive token with the query are dropped (visible as `filtered_unrelated`).
- Vector search has no reranker; scores from the hash provider are coarse ordering signals,
  not calibrated probabilities.

The behaviour record (integration shape, evidence semantics, local-first constraints)
is
[`../DECISION_LOG.md`](../DECISION_LOG.md) entry **D-0030**; ADR
[`adr/0012-local-first-llamaindex-retrieval.md`](adr/0012-local-first-llamaindex-retrieval.md)
records only how this documentation is enforced (living view + law R22).
Operational procedures live in
[`../ops/KNOWLEDGE_RETRIEVAL_RUNBOOK.md`](../ops/KNOWLEDGE_RETRIEVAL_RUNBOOK.md).
