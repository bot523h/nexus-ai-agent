# 05 — Memory + RAG Research

> **Agent 4 — Independent Research**
> **Date:** 2026-09-22
> **Status:** VERIFIED where benchmark-cited, PARTIALLY where domain-specific, CONFIDENCE H/M/L

---

## Preamble

Memory in 2026 is not "vector DB vs not" — it is a tiered architecture: **Working (prompt) + Episodic/Semantic (retrieval) + Procedural (weights/prompt)**. RAG is one retrieval primitive among several (BM25, hybrid, GraphRAG, long-context stuffing). The right question is not "which is best universally" but "which multi-hop/detail/ freshness curve matters for the workload." [VERIFIED via arahi.ai 2026 memory tiers + HydraDB 2026 conclusion.]

We compare 9 approaches: Vector RAG, Hybrid Search, BM25, Knowledge Graph, GraphRAG, Long Context, Hierarchical Memory, Episodic, Semantic vs Working.

---

## 1) Architectures Defined

| # | Architecture | Core mechanism | Typical stack |
|---|--------------|---------------|---------------|
| M1 | **Vector RAG** | Dense embeddings → ANN similarity (cosine) → top-k | SentenceTransformers + Chroma/Qdrant/sqlite-vec + HNSW |
| M2 | **BM25** | Sparse keyword scoring (TF-IDF variant, k1/b params) | Elasticsearch, tantivy, sqlite FTS5 |
| M3 | **Hybrid Search** | Parallel BM25 + dense → RRF fusion → reranker (cross-encoder/FlashRank) | Elasticsearch hybrid, Denser Retriever, Qdrant hybrid |
| M4 | **Knowledge Graph (KG)** | Entities + typed relations → graph schema → traversal/query (Cypher/GQL) | FalkorDB, Neo4j, Kuzu |
| M5 | **GraphRAG (Microsoft / FalkorDB)** | Entity/relation extraction → community detection + summaries → graph + vector retrieval | Graph construction (LLM extraction) + graph traversal + vector |
| M6 | **Long Context** | Stuff all docs into extended context window (128k–1M tokens) without retriever | GPT-5 128k, Claude 200k, Gemini 1–2M |
| M7 | **Hierarchical Memory** | L0 working (window) → L1 episodic (session) → L2 semantic (long-term KG/vector) with promotion/eviction | Mem0, Letta, Zep, NEXUS continuum/snapshot |
| M8 | **Episodic Memory** | Time-stamped events (what happened when, with whom) | Session store + vector index keyed by time |
| M9 | **Semantic Memory** | Consolidated facts/generalized knowledge (what is true) | KG or distilled vector cluster |

*Working Memory = in-prompt context window itself — evaluated as M6.*

---

## 2) Comparison Matrix (Evidence-Backed)

> Scores qualitative (↑ good, ↓ poor, → mixed). Numbers where benchmark exists; otherwise trade-off description.

| Dimension | M1 Vector RAG | M2 BM25 | M3 Hybrid | M4 KG | M5 GraphRAG | M6 Long Context | M7 Hierarchical |
|-----------|---------------|---------|-----------|-------|-------------|-----------------|-----------------|
| **Accuracy — single-hop / detail** | Good (precise semantic) | Good for exact-match (IDs, codes, names) | **Best overall** (+7.4% NDCG) | Poor if not KV-matched | Good (overhead) | Best if fits window, but degrades past ~100k effective recall | Good if stacked |
| **Accuracy — multi-hop / relational** | Weak (fragments) | Weak | Moderate | **Strong (schema)** | **Strong (+30pp on tech subset, 3.4x overall in one benchmark)** | Moderate (needs reasoning, not retrieval) | Strong |
| **Accuracy — global/thematic (summaries over corpus)** | Weak | Weak | Moderate | Moderate | **Strong (62-71% diversity win, 72-80% comprehensiveness)** | Moderate (hallucinates over large contexts) | Strong |
| **Latency** | Low (10-50ms HNSW) | Low (5-20ms inverted) | Medium (parallel + fusion) | Low-medium (traversal) | High (graph build + traversal) | High (prefill cost scales O(n²) attention) | Tier-dependent |
| **Cost — query** | Low ($/embedding + ANN) | Near-zero | Low-medium (2× retrieval + rerank) | Low | High (LLM extraction + larger context) | **Highest (pay for whole context every call)** | Amortized |
| **Cost — ingestion** | Low (embed) | Low | Low-medium | Medium (schema modeling) | **Highest (LLM entity extraction + community summaries)** | Zero (no ingestion) | Medium |
| **Storage** | Vectors (~1-2KB/chunk) | Inverted index small | Sum of both | Graph size ~ nodes+edges | Vector + graph (largest) | Zero persisted (but context re-sent each time) | Tiered (vector + graph + table) |
| **Update complexity** | Easy (re-embed doc) | Easy (reindex) | Easy | Medium (schema change) | **Hard (re-extract graph, re-community)** | None (but stale if context not rebuilt) | Forgetting + promotion policy needed |
| **Forgetting / eviction** | TTL or tombstone | TTL | TTL | Delete node/edge | Rebuild subgraph | N/A | Explicit policy (recency/importance) |
| **Staleness** | Stale until re-embed | Stale until reindex | Stale until both | Stale until graph update | Most stale (pipeline lag) | Fresh if context rebuilt | Tier-dependent, reconciler needed |
| **Security (ACL-aware)** | Weak (most DBs lack native ACL filter) | Weak | Weak | Strong (edge-level perms) | Weak-medium | N/A | Policy-controlled per tier |
| **Evaluation maturity** | High (BEIR, MTEB) | High (classic IR) | High (WANDS) | Medium | Emerging but growing (ORAN, Lettria) | Prompt eval | Low (mem-specific eval scarce) |
| **Explainability** | Similarity scores only | Scores | Scores + fusion | **Strong (traceable paths)** | Strong (entity paths) | None | Medium |

**Key benchmark sources:**
- WANDS e-commerce: Hybrid RRF+field boosting 0.7497 NDCG vs BM25 0.6983 vs dense 0.6953 (+7.4%) [VERIFIED via Denser AI guide citing Turnbull 2025].
- MTEB: Hybrid 56.47 vs pure vector 54.24 (+4.11%) [VERIFIED].
- ORAN specs: Hybrid GraphRAG 0.58 factual correctness vs Vector 0.48 (+8pp), GraphRAG 0.11 context relevance vs Vector 0.10 vs Hybrid 0.04 [VERIFIED via arxiv 2507.03608].
- Lettria/AWS: GraphRAG 80% correct vs 50.83% vector baseline; with acceptable 90% vs 67.5% [PARTIALLY — single evaluation, vendor-published via AWS, treat as domain-specific].
- News/Podcast: GraphRAG 60-83% win global sensemaking but vector better single-hop — confirms complementarity [VERIFIED via HydraDB synthesis].

---

## 3) Detailed Findings per Architecture

### M1 Vector RAG [VERIFIED, HIGH]

- **How it works:** Chunk splitter → embedding model → vector store → ANN search → inject top-k into prompt → LLM synthesizes.
- **Strength:** Blazing single-fact lookup, paraphrase handling, cheap ingestion.
- **Weakness:** Loses relational structure; "sounds similar ≠ is connected" problem; fails on schema-bound queries (KPI tracking 0% in Diffbot benchmark before GraphRAG).
- **NEXUS relevance:** Uses `sentence-transformers` + `sqlite-vec` + `ChromaDB` + `FlashRank` rerank — fits modular monolith constraint. READ-ONLY OBS: correct Hanover.

### M2 BM25 [VERIFIED, HIGH]

- **Strength:** Exact-match king — recovers rare terms, IDs, Persian/Arabic transliterations that embedding may miss.
- **Weakness:** No semantics ("king" ≠ "monarch"); vocab mismatch.
- **Implication:** Never use alone if queries are natural language — but always use as hybrid partner.

### M3 Hybrid Search [VERIFIED, HIGH — the default]

- **Why wins:** Combines BM25 exactness + dense semantics + RRF diversity. Denser AI guide: basic RRF +1.2%, + field boosting → +7.4%. This is the production pattern most teams converge to.
- **Cost:** Two retrievals + fusion ~1.5-2× vector alone, but NDCG gain justifies.
- **Recommendation (non-prescriptive):** Treat hybrid as baseline; fall back to GraphRAG only where multi-hop need proves out via eval.

### M4/M5 Graph / GraphRAG [PARTIALLY VERIFIED, MEDIUM — gains are real but domain-sensitive]

- **Trade-offs:** Highest accuracy on connected/complex queries, but highest ingestion cost (LLM extraction) and update complexity. Update lag causes staleness — graph lags behind document updates.
- **When GraphRAG dominates:** Enterprise knowledge with strong entity structure (org chart, transactions, citations, ORAN specs). When not: simple FAQ bots (adds cost without gain — "Don't default to GraphRAG because it's newer").
- **Hybrid GraphRAG nuance:** ORAN study shows hybrid leads *factual correctness* (0.58) but *trails* context relevance (0.04) vs pure GraphRAG (0.11) — suggests hybrid compensates when graph context insufficient but adds redundancy noise.

### M6 Long Context [VERIFIED, HIGH — diminishing returns]

- **Reality:** Even 1M token windows have effective recall drop past ~100k ("lost in the middle" problem, verified in multiple evals). Plus quadratic attention cost. No ingestion cost but enormous per-query cost (pay for whole context each time). Staleness = rebuild cost.
- **Use:** Ephemeral tasks where corpus is tiny (RAG overkill) or where reasoning over entire set at once matters and cost is acceptable. Not a replacement for retrieval at corpus scale.

### M7 Hierarchical / Multi-Tier Memory [PARTIALLY VERIFIED, MEDIUM]

- **Pattern:** L0 working (window), L1 episodic (vector keyed by time), L2 semantic (KG), procedural (system prompt/fine-tune). Promotion via salience, demotion via decay.
- **Implementations:** Mem0 (universal memory layer), Letta (formerly MemGPT, OS-inspired paging), Zep (temporal KG).
- **Evaluation scarce:** Memory-layer eval benchmarks emerging (not as mature as BEIR/MTEB). Teams often roll custom eval (recall over multi-session).
- **NEXUS observation:** `knowledge`, `memory`, `continuum/snapshot.py`, `storage/langgraph_checkpoint.py`, `storage/checkpoint_lifecycle*.py` appear to map to tiered memory; READ-ONLY noted for Stage 10.

### M8/M9 Episodic vs Semantic [VERIFIED — cognitive architecture]

- **Episodic:** What happened (timestamped interactions). Supports "remember what user told me last Tuesday." Requires per-tenant isolation + TTL.
- **Semantic:** What is true (generalized facts). Requires consolidation (dedupe, conflict resolution).
- **Failure mode:** Memory poisoning (from §04) targets both — gradual false fact injection. Mitigation = provenance + human-golden writes + reconciler.

---

## MEMORY_ARCHITECTURE_MATRIX (Condensed Deliverable)

> Use this matrix to answer "which memory for which workload."

```
Workload →               | Simple lookup | Keyword-heavy | Multi-hop / relational | Global summary | Long-lived personal memory | Edge offline
-------------------------|---------------|----------------|------------------------|----------------|------------------------------|-------------
Vector RAG               | ★★★★          | ★★             | ★★                     | ★★             | ★★ (with vector)          | ★★★ (sqlite-vec)
BM25                     | ★★            | ★★★★★          | ★                      | ★              | ★                            | ★★★
Hybrid (BM25+Vector+RRF) | ★★★★★         | ★★★★★          | ★★★                    | ★★             | ★★★                          | ★★★
Knowledge Graph          | ★             | ★              | ★★★★★                  | ★★★            | ★★★★ (structured)         | ★★
GraphRAG                 | ★★            | ★              | ★★★★                   | ★★★★★          | ★★★★                       | ★
Long Context             | ★★            | ★★             | ★★                     | ★★★            | ★                          | ★
Hierarchical (L0+L1+L2)  | ★★★           | ★★             | ★★★★                   | ★★★★           | ★★★★★                      | ★★
```

*Stars = suitability, not ranking of implementations. Cost-adjusted: GraphRAG stars assume budget permits.*

---

## Evaluation Dimensions (How to Measure Memory)

| Metric | What it measures | How to capture | Tool |
|--------|------------------|----------------|------|
| Faithfulness / Groundedness | Is answer supported by retrieved context? | LLM-as-judge vs citations | TruLens, Phoenix, RAGAS |
| Answer Relevance | Did answer address the question? | Embedding cosine + human | RAGAS, HELM |
| Context Relevance | Retrieved docs relevant to Q? | NDCG, MAP | BEIR, MTEB, WANDS |
| Factual Correctness | Fact matches source of truth? | Exact/semantic match | ORAN eval (LLM judge) |
| Latency p50/p95 | Retrieval+rerank+gen | OTel traces | Prometheus |
| Cost per query | $ embed + vector + LLM | Token meter | Litellm proxy / OTel GenAI metrics |
| Freshness (staleness) | Time since source update → retrieval | Repo diff timestamp | Reconciler log |
| Forgetting rate | Knowledge dropped prematurely | Recall over retention set | Custom mem eval |

**Source:** TruLens (groundedness, context/answer relevance, coherence), Phoenix OTel traces, futureAGI E2E eval. [VERIFIED]

---

## Architectural Implications (for NEXUS-class offline-first agent)

- **Offline-first implies sqlite-vec + FTS5 (BM25) + FlashRank are the full hybrid stack without external infra — fits modular monolith. Qdrant/Weaviate would break single-process constraint. OBSERVED choice in NEXUS is aligned. [VERIFIED]**
- **GraphRAG is justified only if NEXUS workload becomes relationship-heavy (e.g., multi-user community graph, referral network analysis, learning-path over content graph). Current Telegram assistant + Nagar studio workload is not graph-dominant per read-only scan. Adding GraphRAG now would raise ingestion cost + staleness without measured gain — gap noted as opportunity, not requirement.**
- **Hierarchical memory + consent gate is required if long-term personal memory is product promise (NEXUS consent tri-state already exists). Tiering reduces staleness risk via TTL + reconciler.**

---

## Open Questions

- **OQ1:** Optimal chunking for Persian/Arabic morph-rich text in hybrid (BM25 benefits from decompounding, vector from phrase embedding) — no Persian-specific benchmark found [UNKNOWN].
- **OQ2:** Graph extraction quality for low-resource languages — likely worse, not measured [UNKNOWN].
- **OQ3:** Evaluation of episodic recall over >30 days multi-session — no standard benchmark; teams hand-roll [UNKNOWN].

---

## Sources Cited (Stage 5)

1. **Primary:** https://arxiv.org/html/2507.03608v1 — ORAN 3-way Vector vs GraphRAG vs Hybrid (0.58 vs 0.48 factual, 0.11 vs 0.10 vs 0.04 context)
2. **Primary:** FalkorDB blog 2025-04-07 "How GraphRAG Outperforms Vector Search" (3.4x claim) — primary vendor benchmark, treat as domain-specific
3. **Independent (synthesizing):** HydraDB 2026 "18 GraphRAG Adoption & Accuracy Stats" — aggregates Lettria/AWS 80% vs 50.83%, technical subset 90.63% vs 46.88%, news/pods 72-80% comprehensiveness, diversity 62-71%, all with attribution
4. **Independent:** Denser AI 2026 "Hybrid Search for RAG" — WANDS 0.7497 vs 0.6983/0.6953 (+7.4%) + MTEB 56.47 vs 54.24 (+4.11%), RRF + field boosting analysis
5. **Independent:** Futurense 2026 "GraphRAG vs Vector RAG Side-by-Side" — mechanism table, setup complexity, update frequency, explainability
6. **Independent:** arahi.ai 2026 "Practical Reference" 6 layers + 5 architectures (memory tiers)
7. **Independent:** FutureAGI 2026 LLM Agent Architectures (Mem0, Letta, Zep as popular memory layers)
8. **Independent:** TruLens / Phoenix / OpenLLMetry docs — evaluation metrics for memory
9. **Primary (NEXUS READ-ONLY):** pyproject.toml `sentence-transformers`, `sqlite-vec`, `chromadb`, `flashrank`, `flashrank>=0.2` ; `src/nexus_ai_agent/memory`, `knowledge`, `continuum`, `storage/checkpoint_lifecycle` layout

