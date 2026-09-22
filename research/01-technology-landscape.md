# 01 — Global Technology Landscape 2026

> **Agent 4 — Independent Research & Architecture Intelligence**
> **Date:** 2026-09-22 (Knowledge cutoff 2026-01-04, verified sources up to 2026-08)
> **Scope:** AI Agent, Assistant, Tool/Capability, Command Bus, Workflow, Long-running Jobs, Multimodal, Local/Cloud AI, Memory, RAG, Event-driven
> **Status:** VERIFIED (core claims) | CONFIDENCE: HIGH where 2+ primary sources converge
> **Rule:** No implementation, no Nagar modification — READ-ONLY observation only.

---

## Method

- Minimum 10 technologies evaluated against uniform dimensions: purpose, architecture, maturity, license, strengths, weaknesses, scalability, local/offline capability, integration complexity.
- Primary sources: official docs, official GitHub, spec.
- Independent sources: engineering blogs, benchmark papers, third-party comparisons.
- Verdict tags per finding: VERIFIED / PARTIALLY VERIFIED / UNVERIFIED / CONTRADICTED / UNKNOWN + CONFIDENCE H/M/L.

---

## T1 — LangGraph (LangChain Inc.)

- **Purpose:** Stateful agent/workflow orchestration as directed graph with checkpointing.
- **Architecture:** Nodes = LLM/tool/human steps, Edges = conditional transitions, State = typed dict/state object persisted via Checkpointers (SQLite, Postgres). Durable execution + interrupt/resume + time-travel.
- **Maturity:** v1.0 stable (late 2025), production at Uber/LinkedIn/Klarna per LangChain & third-party reports. 90k+ stars LangChain ecosystem, LangGraph docs declare graph-based orchestration, durable execution, persistence, HIL. [VERIFIED, HIGH]
- **License:** MIT (langgraph). LangSmith (observability) commercial.
- **Strengths:** Explicit control flow, replayable, debuggable, best-in-class HIL, streaming, deep LangChain integration (700+ connectors).
- **Weaknesses:** Steep learning curve (graph theory), latency overhead for trivial agents, Python/JS only.
- **Scalability:** Horizontal via distributed checkpoint store (Postgres); 100+ parallel nodes. Cost is checkpoint store + event log.
- **Local/offline:** Fully local if checkpoint = SQLite + LLM = Ollama/llama.cpp. No cloud required.
- **Integration complexity:** Medium-high (need to model state schema, edges, checkpoint config).

**Primary:** https://langchain-ai.github.io/langgraph/ + GitHub langchain-ai/langgraph
**Independent:** uvik.net 2026 comparison, Cordum 2026 snapshot, Arise handbook 2026 — all rank LangGraph as strongest for stateful production.

---

## T2 — Model Context Protocol (MCP) — Anthropic / Linux Foundation AAIF

- **Purpose:** Open standard for LLM ↔ tools/resources/prompts; single integration works across any compliant client.
- **Architecture:** Host (LLM app) ↔ Client SDK ↔ Server (tools/resources/prompts) over JSON-RPC 2.0. Transports: stdio (local), Streamable HTTP (2025-03-26+). Primitives: Tools (JSON Schema), Resources (URIs), Prompts (templates). Auth: OAuth 2.1, CIMD (2025-11-25), stateless core (2026-07-28), Extensions (Tasks, EMA, MCP Apps).
- **Maturity:** v2024-11-05 initial → 2025-03-26 → 2025-06-18 → 2025-11-25 Final → 2026-07-28 Current. Official Registry (registry.modelcontextprotocol.io) live preview 2025-09-08. Donated to Agentic AI Foundation (Linux Foundation) 2025. Adopted by OpenAI, Google DeepMind, Microsoft, GitHub, Vercel, Cursor. [VERIFIED, HIGH]
- **License:** Apache 2.0 (spec + SDKs).
- **Strengths:** Vendor-neutral, 75+ connectors (Claude directory), solves NxM integration, version negotiation, cache hints.
- **Weaknesses:** Spec churn (5 revisions in 20 months), security relies on server trust + client validation; tool annotations considered untrusted unless from trusted server.
- **Scalability:** Registry is metaregistry (metadata only, code on npm/PyPI/Docker). Stateless 2026 revision enables round-robin LB.
- **Local/offline:** stdio fully offline; Streamable HTTP can be local-only.
- **Integration complexity:** Low for stdio; Medium for OAuth/CIMD + Registry publishing.

**Primary:** https://modelcontextprotocol.io/specification/2025-11-25 , https://blog.modelcontextprotocol.io/posts/2026-07-28/ , https://modelcontextprotocol.info/tools/registry/
**Independent:** Descope Learn, DigitalApplied 2026 stats — platform support well documented.

---

## T3 — Temporal (Durable Execution Engine)

- **Purpose:** Durable workflow orchestration with event-sourced history, replay, timers, signals, compensation.
- **Architecture:** Temporal Server + Workers (SDKs: Python/Go/Java/.NET/PHP/TS) + Persistence (Postgres/Cassandra/MySQL) + optional Elasticsearch visibility. Workflows replay from event log; activities have heartbeats, retry policies.
- **Maturity:** Server v1.30.x (Aug 2026), large production base. Survives worker crashes via persisted event history. [VERIFIED, HIGH]
- **License:** MIT (server + SDKs).
- **Strengths:** True durable execution, built-in retries/compensation, auditable history, versioning, multi-language.
- **Weaknesses:** Extra infra (DB + server), deterministic workflow sandbox restriction, higher per-step overhead (server round-trip), steeper operational learning curve.
- **Scalability:** Horizontal via Temporal cluster; proven to millions workflows/day. Horizontal bottleneck is persistence layer.
- **Local/offline:** Fully self-hostable; no cloud dependency.
- **Integration complexity:** High initial (server + DB + worker deployment), then medium for workflow authoring.

**Primary:** docs.temporal.io
**Independent:** Markaicode Temporal vs Celery 2026, SuhasBhairav Celery vs Temporal — both describe durability guarantee clearly.

---

## T4 — Celery / RQ / Dramatiq / arq (Python Task Queues)

- **Purpose:** Background job distribution over broker (Redis/RabbitMQ).
- **Architecture:** Celery: broker + result backend + beat + workers + Canvas (chains/groups/chords). RQ: Redis-only, simple. Dramatiq: RabbitMQ-first, reliable ack, type hints. arq: asyncio-native, Redis.
- **Maturity:** Celery 5.6.3 (15+ years), RQ 1.16+, Dramatiq 1.18+, all production.
- **License:** BSD (Celery/RQ/Dramatiq), MIT (arq).
- **Strengths:** Celery — richest feature set, Canvas; RQ — 5-min setup; Dramatiq — predictable reliability, 30-40% lower memory vs Celery at 1.2k tps; arq — best for async I/O-bound.
- **Weaknesses:** At-least-once only (app must implement idempotency); Celery doc explicitly says it guarantees message delivery to broker, NOT durable task state; Celery complexity, config pitfalls.
- **Scalability:** Celery handles 950 tps/worker typical, Dramatiq ~1200 tps. Horizontal via more workers. Bottleneck = broker.
- **Local/offline:** Fully local (Redis/RabbitMQ).
- **Integration complexity:** RQ Low, Dramatiq Medium, Celery High, arq Medium-async.

**Primary:** docs.celeryproject.org, python-rq.org, dramatiq.io
**Independent:** DevProPortal 2025 benchmark, Markaicode Celery Alternatives 2026, MujtabaAlmas comparison — consistent on durability semantics & performance.

---

## T5 — llama.cpp / GGUF + Ollama + MLX

- **Purpose:** Local LLM inference stack: quantization, serving, Apple Silicon acceleration.
- **Architecture:** GGUF unified weight format (tokenizer+metadata+weights) replaces GGML. llama.cpp: portable C/C++ inference (CPU, Metal, CUDA, ROCm, Vulkan). Ollama: daemon wrapping llama.cpp with model registry, OpenAI-compatible API at :11434. MLX: Apple array framework, fastest on M1-M4. vLLM/TGI/TensorRT-LLM for throughput serving.
- **Maturity:** llama.cpp >70k stars, GGUF standard, Ollama v0.11+ (install.sh → ollama pull/run). MLX 0.x mature. Quantization 1.5–8-bit, K-quants + imatrix. [VERIFIED, HIGH]
- **License:** MIT (llama.cpp, Ollama, MLX).
- **Strengths:** Runs on CPU-only, offline, privacy-preserving, air-gappable, 7B FP16 14GB → Q4_K_M 4-5GB, single-binary deploy.
- **Weaknesses:** Lower throughput vs GPU servers, quantization quality loss below Q4, KV-cache memory dominance on long context, Ollama serializes under concurrent load.
- **Scalability:** Vertical (quantization) + partial GPU offload; not horizontal. For concurrent serving switch to vLLM.
- **Local/offline:** Native — designed for it.
- **Integration complexity:** Low (Ollama), Medium (llama.cpp build), High (vLLM sizing).

**Primary:** github.com/ggerganov/llama.cpp, ollama.ai/docs, ml-explore.github.io/mlx
**Independent:** 1337Skills 2026 edge guide, ombharatiya ai-system-design-guide, DasRoot 2026 deployment guide.

---

## T6 — ONNX Runtime / WebGPU / WASM / ExecuTorch

- **Purpose:** Cross-platform, browser/edge inference runtimes.
- **Architecture:** ONNX: portable graph + opset; Runtime executes on CPU/GPU/NPU via execution providers. WebGPU: browser GPU API for in-browser inference. WASM: CPU fallback in browser. ExecuTorch: PyTorch-native on-device (phone → micro-controller), 1.0 late 2025, billions devices.
- **Maturity:** ONNX Runtime 1.18+, WebGPU in Chrome/Edge stable, ExecuTorch 1.0 2025. [PARTIALLY VERIFIED, MEDIUM]
- **License:** MIT (ONNX Runtime, ExecuTorch), browser standards.
- **Strengths:** Widest hardware coverage, model portability, browser-native without server.
- **Weaknesses:** Operator coverage gaps, large WASM payloads, WebGPU still maturing vs CUDA, ExecuTorch limited model support.
- **Scalability:** N/A (device-bound). Packaging: ONNX quantization, Optimum exports.
- **Local/offline:** Fully offline; designed for disconnected.
- **Integration complexity:** High (conversion, opset compat, quantization tuning).

**Primary:** onnxruntime.ai, webgpu.io, pytorch.org/executorch
**Independent:** ai-system-design-guide edge deployment notes, MLC LLM compile-to-many.

---

## T7 — Vector DBs + Hybrid Search (Chroma, Qdrant, Weaviate, sqlite-vec) + FlashRank/BM25

- **Purpose:** Retrieval layer for RAG: embedding storage, ANN search, hybrid sparse+dense.
- **Architecture:** Dense embeddings (SentenceTransformers) → HNSW/IVF index → kNN. Hybrid = BM25 (sparse) + dense fused via RRF. Reranker (FlashRank/Cross-encoder) rescores. sqlite-vec = embedded SQLite vector extension for single-process.
- **Maturity:** Chroma 1.x, Qdrant 1.11+, Weaviate 1.28+, sqlite-vec 0.1+; Hybrid search production-proven.
- **License:** Apache 2.0 (Chroma, Qdrant), BSD-3 (Weaviate), MIT (sqlite-vec).
- **Strengths:** Hybrid +7.4% NDCG over pure dense (WANDS e-commerce: RRF+field boosting 0.7497 vs BM25 0.6983 / dense 0.6953), cheap to start, sqlite-vec keeps single-process constraint (NEXUS choice).
- **Weaknesses:** Vector quality depends on embedding model drift; hybrid tuning per domain; sqlite-vec lacks distributed scaling.
- **Scalability:** Chroma/Qdrant horizontal; sqlite-vec vertical only (suited to NEXUS monolith).
- **Local/offline:** All can run fully local (sqlite-vec best for NEXUS).
- **Integration complexity:** Low (sqlite-vec), Medium (Chroma), Higher (self-hosted Qdrant cluster).

**Primary:** docs.trychroma.com, qdrant.tech/documentation, github.com/asg017/sqlite-vec
**Independent:** Denser AI hybrid search guide (MTEB +4.11%), Turnbull 2025 WANDS benchmark cited therein.

---

## T8 — LiteLLM / OpenRouter (Multi-Provider Routing Chain)

- **Purpose:** Unified LLM gateway across 100+ providers (OpenAI, Anthropic, Gemini, Groq, Ollama).
- **Architecture:** OpenAI-compatible proxy: request → routing logic (fallback, retry, load balance, cost tracking) → provider adapters. LiteLLM library + proxy server. OpenRouter similar as managed aggregator.
- **Maturity:** LiteLLM 1.74+ (in NEXUS pyproject), OpenRouter production, both handle streaming, retries.
- **License:** MIT (LiteLLM), commercial (OpenRouter service).
- **Strengths:** Model portability, automatic failover, cost/latency observability, minimal code change (OpenAI API surface).
- **Weaknesses:** Adds hop latency, provider schema drift, retry storms if misconfigured, cost estimation != invoice.
- **Scalability:** Stateless proxy scales horizontally.
- **Local/offline:** LiteLLM can route to Ollama/llama.cpp locally → offline path exists.
- **Integration complexity:** Low (library), Medium (proxy + fallback policy).

**Primary:** docs.litellm.ai, openrouter.ai/docs
**Independent:** NEXUS docs/architecture/LLM_PROVIDERS.md (ordered fallback chain).

---

## T9 — OpenTelemetry + GenAI Semantic Conventions + Phoenix/TruLens/OpenLLMetry

- **Purpose:** Vendor-neutral observability for LLM/agent: traces, metrics, logs.
- **Architecture:** OTel SDK → OTLP exporter → Collector → backend (Grafana/Datadog/Honeycomb/Jaeger). GenAI conventions: span attrs for model, tokens, provider, cost. Tools like Phoenix, TruLens, OpenLLMetry auto-instrument LangChain/LangGraph.
- **Maturity:** CNCF graduated, GenAI conventions stable 2025, wide adoption.
- **License:** Apache 2.0.
- **Strengths:** One instrumentation → any backend, session-grouped multi-turn traces, cost/token per span, Evals (groundedness, context relevance).
- **Weaknesses:** Prompt/response payloads are heavy (KB/span, cost by span), sampling dilemma; LLM payloads raise PII/redaction risk.
- **Scalability:** Collector scales horizontally; sampling essential at >1M spans/day.
- **Local/offline:** Fully self-hostable (Jaeger+Prometheus+Grafana).
- **Integration complexity:** Low (one-line OpenLLMetry), Medium for custom GenAI attrs.

**Primary:** opentelemetry.io, otel GenAI semconv, docs.temporal.io (for workflow telemetry)
**Independent:** GetMaxim OTel for LLM guide, MintMCP OTel for agents, Firecrawl best LLM observability 2026, OpenObserve agent tracing.

---

## T10 — Kafka / Redis Streams / RabbitMQ / Postgres Queues (Event-Driven Substrate)

- **Purpose:** Event backbone for distributed agents: pub/sub, streams, queues.
- **Architecture:** Kafka: partitioned log, consumer groups, exactly-once via transactions/idempotent producer (processing is at-least-once unless transactional). Redis Streams: XADD/XREADGROUP, claim. RabbitMQ: exchanges/queues, ack/nack, quorum queues. Postgres: SKIP LOCKED polling, LISTEN/NOTIFY, durable via ACID.
- **Maturity:** All production-proven (Kafka 3.x, Redis 7+, RabbitMQ 4.x, Postgres 16+).
- **License:** Apache 2.0 (Kafka), BSD (Redis), MPL 2.0 (RabbitMQ), PostgreSQL License.
- **Strengths:** Kafka: high throughput + replay; Redis Streams: low latency; RabbitMQ: rich routing; Postgres: zero extra infra, transactional job claim.
- **Weaknesses:** Kafka: ops heavy; Redis: memory; RabbitMQ: broker SPOF; Postgres polling latency & vacuum pressure at high QPS.
- **Scalability:** Kafka linear with partitions; Redis clustered; Postgres vertical/bottleneck at ~few k jobs/s.
- **Local/offline:** All self-hostable.
- **Integration complexity:** Postgres Low (if DB already), Redis Medium, Kafka/RabbitMQ High.

**Primary:** kafka.apache.org, redis.io/docs/latest/commands/streams, rabbitmq.com/docs, postgresql.org/docs
**Independent:** Temporal docs (on why queues vs durable execution), Starlite/ARQ Postgres queue patterns.

---

## T11 — CrewAI / AutoGen / Semantic Kernel / LlamaIndex / Haystack (Framework Landscape Snapshot)

*Additional evaluated for Stage 9 completeness, summarized here:*

| Framework | Abstraction | Best fit | Maturity signal | Local/offline |
|-----------|-------------|----------|-----------------|---------------|
| CrewAI | Role/goal/backstory crews + Flows | Fastest multi-agent prototype | 47.7k stars, 6.39M PyPI dl/mo, 60% F500 claimed | No native local LLM, but can bind LiteLLM/Ollama |
| AutoGen (now MS Agent Framework) | Conversational GroupChat, AgentChat | Research debate, peer-to-peer | 56.5k stars, maintenance mode → Agent Framework 1.0 successor | Same |
| Semantic Kernel | Plugins + planners, C#/Python/Java | Enterprise .NET/Java | 27.6k stars, MS backed | Ollama connector exists |
| LlamaIndex | Data connectors + query engines + Workflows | RAG-heavy agents | 48.2k stars | sqlite-vec / local embeddings yes |
| Haystack | Pipelines (retrievers/generators/routers) | Regulated doc QA | mature OSS | local retrievers yes |

All above MIT/Apache 2.0, no native policy gate (see Cordum 2026). [PARTIALLY VERIFIED, MEDIUM — stars/download numbers vendor-reported, cross-checked via multiple comparison blogs which converge on ranking]

---

## T12 — Emerging / Cross-Cutting 2026 Trends

- **A2A (Agent-to-Agent) Protocol — Google:** Inter-agent discovery + collaboration, orthogonal to MCP. [VERIFIED, HIGH — announced Apr 2025]
- **Stateless MCP 2026-07-28:** Enables LB statelessness, MRTR for server→client calls. Architectural implication: MCP can front a stateless fleet, not sticky sessions. [VERIFIED]
- **ExecuTorch 1.0:** Phone/microcontroller LLM inference at scale. Implication: true edge agents without server at all. [VERIFIED]
- **GenAI OTel conventions:** Token/cost per span standardization → cost attribution is solved problem if instrumented. [VERIFIED]

---

## Summary Table (Condensed)

| # | Tech | Purpose | License | Scalability | Offline | Complexity |
|---|------|---------|---------|-------------|---------|------------|
| T1 | LangGraph | Stateful agent graphs | MIT | 100+ nodes | yes (SQLite+Ollama) | High |
| T2 | MCP | Tool integration standard | Apache 2.0 | Stateless LB | yes (stdio) | Low-Med |
| T3 | Temporal | Durable workflows | MIT | Millions wf/day | yes | High |
| T4 | Celery/RQ/Dramatiq | Task queues | BSD/MIT | ~1k tps/worker | yes | RQ Low, Celery High |
| T5 | llama.cpp/Ollama/MLX | Local inference | MIT | vertical | native | Low-Med |
| T6 | ONNX/WebGPU/WASM | Edge/browser inference | MIT | device | native | High |
| T7 | Vector DB + Hybrid | RAG retrieval | Apache 2.0/MIT | distributed | yes | Low-Med |
| T8 | LiteLLM | Provider routing | MIT | horizontal proxy | via Ollama yes | Low |
| T9 | OTel GenAI | Observability | Apache 2.0 | horizontal collector | yes | Low |
| T10 | Kafka/Redis/Rabbit/PG | Event substrate | varied | Kafka highest | yes | PG Low, Kafka High |
| T11 | CrewAI/AutoGen/SK | Frameworks | MIT/Apache | framework | via local LLM | varied |
| T12 | A2A/ExecuTorch | Inter-agent + edge | varied | fleet/device | yes | varied |

---

## Findings with Evidence Level

- **F1 [VERIFIED, HIGH]:** 2026 agent landscape is *graph + durable execution + standardized tool protocol + local inference* — all four are required for NEXUS-class offline-first system. Convergent evidence from LangGraph docs + MCP spec + Temporal docs + llama.cpp/Ollama maturity.
- **F2 [VERIFIED, HIGH]:** No marketplace consensus on "exactly once" — queue docs (Celery, Kafka docs) explicitly state *at-least-once* for task processing; exactly-once is producer or transactional narrow scope. Marketing claims to contrary are contradicted.
- **F3 [PARTIALLY VERIFIED, MEDIUM]:** Hybrid search (BM25+dense+RRF) is the default production pattern, not pure vector. Evidence from Denser AI + WANDS benchmark (+7.4% NDCG) but domain-specific tuning needed.
- **F4 [VERIFIED, HIGH]:** Offline-first is feasible end-to-end today: llama.cpp/Ollama + sqlite-vec + ONNX/ExecuTorch + stdio MCP + SQLite/Postgres queues. No cloud required for core loop.
- **F5 [UNKNOWN, LOW]:** Long-term licensing of Gemini/Claude/GPT-5 APIs remains cloud-only and pricing-volatile; reconciling estimated_cost vs invoice remains manual per NEXUS docs/DECISION_LOG.

---

## Sources Cited (Stage 1)

1. **Primary:** https://langchain-ai.github.io/langgraph/ ; GitHub langchain-ai/langgraph (MIT, v1.0, state/graph model)
2. **Primary:** https://modelcontextprotocol.io/specification/2025-11-25 + https://blog.modelcontextprotocol.io/posts/2026-07-28/ (MCP spec timeline, stateless, registry)
3. **Primary:** https://modelcontextprotocol.info/tools/registry/ + https://blog.modelcontextprotocol.io/posts/2025-09-08-mcp-registry-preview/ (registry metaregistry)
4. **Primary:** docs.temporal.io (durable execution, event history, SDKs)
5. **Primary:** docs.celeryproject.org, dramatiq.io, python-rq.org (queue semantics)
6. **Primary:** github.com/ggerganov/llama.cpp, ollama.ai/docs, ml-explore.github.io/mlx (local inference)
7. **Primary:** onnxruntime.ai, pytorch.org/executorch (edge)
8. **Primary:** docs.trychroma.com, qdrant.tech, github.com/asg017/sqlite-vec (vector)
9. **Primary:** docs.litellm.ai (routing)
10. **Primary:** opentelemetry.io (GenAI semconv)
11. **Independent:** uvik.net/blog/python-ai-agent-frameworks (LangGraph vs CrewAI vs AutoGen comparison, 2026)
12. **Independent:** Cordum 2026 AI Agent Frameworks Comparison (stars, downloads, governance gaps)
13. **Independent:** DevProPortal 2025 Celery vs RQ vs Dramatiq benchmark + Markaicode alternatives 2026
14. **Independent:** 1337Skills 2026 LLM Inference at the Edge guide + ombharatiya ai-system-design-guide edge deployment
15. **Independent:** Denser AI Hybrid Search Guide (WANDS + MTEB benchmarks)
16. **Independent:** GetMaxim + MintMCP OTel for LLM/Agents + OpenObserve tracing
17. **Independent:** HydraDB GraphRAG stats, FalkorDB benchmark (GraphRAG vs Vector)

> **OBSERVATION (NEXUS read-only):** NEXUS correctly chose modular monolith + InProcessJobQueue (SQLite sidecar) + sqlite-vec + litellm + llama.cpp for offline-first; this aligns with landscape verdict F4. No change recommended; noted for Stage 10 stress test.

