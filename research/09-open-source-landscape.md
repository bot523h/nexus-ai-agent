# 09 — Competitive / Open-Source Landscape

> **Agent 4 — Independent Research**
> **Date:** 2026-09-22
> **Coverage:** ≥15 projects across Agent frameworks / AI assistants / Workflow systems / Tool protocols / Local AI / Memory-RAG / Orchestration
> **Status:** VERIFIED where stars/docs-cited converge across ≥2 comparison sources; numbers are point-in-time Aug 2026.

> **No "best" ranking — goal is to UNDERSTAND THE LANDSCAPE per instruction.** Strengths/weaknesses are trade-offs, not verdicts.

---

## Legend for Maturity

- **M0** Research / prototype
- **M1** Early OSS (≤5k stars) production possible with caveats
- **M2** Adopted OSS (10-30k stars) production at small/medium teams
- **M3** Battle-tested OSS (30k+ stars) with enterprise adoption + LTS path
- **M4** Foundation-backed standard (spec, not just code)

---

## A — Agent Frameworks (General Orchestration)

### 1) LangGraph

- **URL:** https://langchain-ai.github.io/langgraph/ + https://github.com/langchain-ai/langgraph
- **License:** MIT
- **Architecture:** Graph/state-machine (nodes/edges/conditional, cycles, checkpointers, time-travel, streaming, HIL `interrupt_before/after`)
- **Core abstraction:** `StateGraph` + `CheckpointSaver` (SQLite/Postgres) + `MessagesState`
- **Strength:** Most explicit control, durable execution, replayable, best debuggability + LangSmith trace depth, 700+ LangChain integrations, Python+JS.
- **Weakness:** Steepest learning curve (graph theory), overkill for trivial linear flows, state schema design up front.
- **Maturity:** M3 (v1.0 Nov 2025, enterprise: Uber/LinkedIn/Klarna, 90k+ LangChain ecosystem, ~1.2s 10-step latency best among compared).
- **Notable decision:** Made *graph*, not chain, the primitive since modern agents need branching + persistence + HIL — explains why LangChain (chains) was superseded by LangGraph.

### 2) CrewAI

- **URL:** https://crewai.com + https://github.com/crewAIInc/crewAI
- **License:** MIT (+ Enterprise cloud)
- **Architecture:** Role/goal/backstory crews + tasks + Flows (hierarchical delegation supervisor → workers; sequential pipeline)
- **Core abstraction:** `Crew(agent.role, goal, backstory)` + `Task` + `Flow`
- **Strength:** Fastest multi-agent prototype (~35 LoC), intuitive to product teams (reads like org chart), 60% Fortune 500 claimed, flows for prod hardening, memory/guardrails built-in.
- **Weakness:** Can hide multi-agent coordination cost (message passing token overhead ~18%, higher than LangGraph 5%); branching less explicit than graph.
- **Maturity:** M2 → M3 trajectory (47.7k stars, 6.39M PyPI dl/mo Sep 2026, very active).
- **Notable decision:** Optimized for *role-shaped* workloads (researcher/writer/editor) not arbitrary graphs.

### 3) AutoGen / Microsoft Agent Framework (successor)

- **URL:** https://microsoft.github.io/autogen/ + https://github.com/microsoft/autogen (+ Agent Framework unified 1.0 successor)
- **License:** MIT
- **Architecture:** Conversational — `GroupChat`, `AgentChat`, teams, event-driven agents, human input patterns; now merged with Semantic Kernel concepts.
- **Core abstraction:** `ConversableAgent` + `GroupChat(team)` + `ModelClient`
- **Strength:** Richest conversational multi-agent, .NET + Python, Enterprise backing, best code-generation debate loop.
- **Weakness:** Strategic shift to Agent Framework means AutoGen in maintenance (bug fixes, no new features) — roadmap uncertainty.
- **Maturity:** M3 (56.5k stars, 1.36M PyPI dl/mo 2026-07) but **transitioning** → evaluate Microsoft Agent Framework (1.0, CodeAct mode, MCP client built-in) for new projects.
- **Notable decision:** Microsoft chose *conversation as coordination* primitive vs graph's *state as coordination*.

### 4) OpenAI Agents SDK

- **URL:** https://platform.openai.com/docs/agents + https://github.com/openai/openai-agents-python
- **License:** MIT
- **Architecture:** Handoffs + guardrails + tracing + sessions; model-directed loops; `Agents` call tools/delegate.
- **Core abstraction:** `Agent(handoffs=[], guardrails=[])` + `Runner`
- **Strength:** Lowest ceremony for OpenAI-native routing/triage; built-in guardrails + tracing; sessions persistence.
- **Weakness:** Provider coupling (best with OpenAI models, less portable); less suited when model flexibility is primary.
- **Maturity:** M2 (newer but adoption fast for triage/assistant use cases).
- **Notable decision:** Elevated *handoff* (not tool call) as first-class routing — matches triage workflows directly.

### 5) Pydantic AI

- **URL:** https://ai.pydantic.dev + https://github.com/pydantic/pydantic-ai
- **License:** MIT
- **Architecture:** Typed single-agent (Pydantic schemas for inputs/tools/outputs), dependency injection, eval harness.
- **Core abstraction:** `Agent[DepsType, OutputType]` with typed `tool` signatures.
- **Strength:** Strongest typed contracts (FastAPI-like DX), tool error reduction via schema, testable, logfire observability.
- **Weakness:** Single-agent focused (multi-agent via composition, not native); smaller ecosystem than LangGraph.
- **Maturity:** M1 → M2 (fast growth 2025-26, highlighted as "best for typed conventional agents" in uvik 2026).
- **Notable decision:** Betting that *types* beat *prompts* for reliability — minority position but evidence shows tool error drop.

### 6) smolagents (Hugging Face)

- **URL:** https://github.com/huggingface/smolagents
- **License:** Apache 2.0
- **Architecture:** Code-as-tool (agent writes Python, executor runs it) + tool calling hybrid.
- **Core abstraction:** `CodeAgent` + sandboxed Python executor
- **Strength:** Eliminates JSON schema overhead when calling numpy/pandas/sklearn; strong for data/ML pipelines.
- **Weakness:** Code generation unsafety if not sandboxed; less governance around free-form code.
- **Maturity:** M1-M2 (adopted for data agents, Paper-Recommended by Hugging Face).
- **Notable decision:** Posits code is a better tool interface than JSON for computational tasks.

---

## B — E-Specific / Data-Heavy Frameworks

### 7) LlamaIndex (GPT Index → 2022)

- **URL:** https://developers.llamaindex.ai + https://github.com/run-llama/llama_index
- **License:** MIT (+ LlamaCloud paid)
- **Architecture:** Data connectors (50+) → query engines → retrievers → workflows → agents.
- **Core abstraction:** `VectorStoreIndex` + `QueryEngine` + `Workflows` (workflow engine)
- **Strength:** Best retrieval-centric correctness, pipeline clarity, audit trail for regulated doc QA.
- **Weakness:** Less natural for open-ended multi-agent orchestration; data-layer overkill for tool-orchestration workflows.
- **Maturity:** M3 (48.2k stars, 10.09M PyPI dl/mo, earliest RAG focus).
- **Notable decision:** RAG before agents — correctness over autonomy.

### 8) Haystack (deepset)

- **URL:** https://haystack.deepset.ai + https://github.com/deepset-ai/haystack
- **License:** Apache 2.0
- **Architecture:** Pipelines (retrievers, generators, routers, tools, agents) + evaluation components.
- **Core abstraction:** `Pipeline` + `component` DAG
- **Strength:** Pipeline contracts excellent for regulated industries needing audit trails + eval-oriented design.
- **Weakness:** Less autonomous multi-agent than CrewAI/AutoGen.
- **Maturity:** M2-M3 (mature, enterprise EU adoption).
- **Notable decision:** Pipelines before agents — governance by construction.

### 9) Semantic Kernel (Microsoft)

- **URL:** https://learn.microsoft.com/semantic-kernel + https://github.com/microsoft/semantic-kernel
- **License:** MIT
- **Architecture:** Plugins + planners + memory + connectors; kernel orchestrates plugin invocation; enterprise C#/Python/Java parity.
- **Core abstraction:** `Kernel(plugin, memory, planner)`
- **Strength:** Only framework with first-class multi-language (C#, Python, Java) + Azure AD alignment; enterprise SDLC.
- **Weakness:** Planning abstraction can obscure execution unless telemetry + tool boundaries added explicitly.
- **Maturity:** M2-M3 (27.6k stars, Microsoft maintained, merging into Agent Framework).
- **Notable decision:** Enterprise SDK model — AI as *plugin* into existing apps.

---

## C — Workflow / Durable Orchestration

### 10) Temporal

- **URL:** https://temporal.io + https://github.com/temporalio/temporal
- **License:** MIT (server + SDKs)
- **Architecture:** Durable execution (event-sourced workflows), activities with heartbeats/timeouts, signals/queries, versioning, schedules.
- **Core abstraction:** `Workflow` (deterministic) + `Activity` (non-deterministic, retryable)
- **Strength:** Only system here providing true replayable durable execution + compensation — uniquely fits hour/day human-in-loop workflows.
- **Weakness:** Deterministic sandbox restriction; ops: server+DB+ES.
- **Maturity:** M3 (Server 1.30.x Aug 2026, multi-language, enterprise fleet).
- **Notable decision:** Determinism as feature (replay requires no random/IO in workflow).

### 11) InProcessJobQueue (NEXUS, representation of embedded queue pattern)

- **URL:** Internal NEXUS `src/nexus_ai_agent/adapters/in_process_job_queue.py` (open pattern: Postgres `SKIP LOCKED` / SQLite sidecar)
- **License:** NEXUS repo license (MIT per LICENSE file)
- **Architecture:** SQLite sidecar `.jobs.sqlite3` + WAL + atomic enqueue/dequeue; ports never import adapters.
- **Core abstraction:** `JobQueuePort` (port) + `InProcessJobQueue` (adapter)
- **Strength:** Zero extra infra, matches modular monolith frozen constraint, durable file-level, easy reconciler.
- **Weakness:** Single-writer, vertical-only, not for multi-host fleet.
- **Maturity:** M1 as product-component (but pattern M3 generally).
- **Notable decision:** Deliberate rejection of Celery/Redis as failure-domain — notable in landscape where most add broker first.

---

## D — Tool Protocols / Standards

### 12) MCP (Model Context Protocol)

- **URL:** https://modelcontextprotocol.io + https://github.com/modelcontextprotocol
- **License:** Apache 2.0
- **Architecture:** Host/Client/Server over JSON-RPC; tools/resources/prompts; stdio + Streamable HTTP; auth OAuth 2.1/CIMD; registry metaregistry; extensions (Tasks, EMA, Apps, WASM roadmap).
- **Core abstraction:** `tools/list` + `tools/call` + `server.json` publishing
- **Strength:** First credible industry-wide tool integration standard (OpenAI, Google, MS, GitHub, Vercel...), solves NxM, stateless LB in 2026-07-28.
- **Weakness:** Churning spec (5 revisions in 20 months), registry moderation still community denylist not automated scan, auth complexity crept in.
- **Maturity:** M4 (foundation-backed standard, official registry 2025-09-08 in preview, aaif Linux Foundation).
- **Notable decision:** Registry is *metadata only* (code stays on npm/PyPI/Docker) — separation of authority & delivery.

### 13) A2A — Agent-to-Agent Protocol (Google, April 2025)

- **URL:** https://google-a2a.github.io/A2A/
- **License:** Apache 2.0
- **Architecture:** Inter-agent discovery, task delegation, handoff negotiation across frameworks.
- **Core abstraction:** `AgentCard` + `Task` + `Message` handoff
- **Strength:** Sidesteps framework choice — makes heterogeneous fleet interoperable; complementary to MCP (MCP = agent↔tool, A2A = agent↔agent).
- **Weakness:** New (Apr 2026 public), adoption still early; framework-specific behaviors may still diverge.
- **Maturity:** M1 → M2 transition (launched Apr 2025/2026, Google + ecosystem).
- **Notable decision:** Treats cross-agent interop as *protocol*, not framework feature.

---

## E — Local AI Runtimes

### 14) llama.cpp + GGUF + Ollama (+ MLX / vLLM) — bundled

- **URL:** https://github.com/ggerganov/llama.cpp + https://ollama.ai + https://github.com/ml-explore/mlx
- **License:** MIT across stack
- **Architecture:** GGUF packaging + C/C++ inference backends (Metal/CUDA/ROCm/Vulkan) + Ollama daemon wrapper + MLX Apple path + vLLM serving for concurrency
- **Core abstraction:** `model.gguf` + `ollama serve (:11434)` ↔ OpenAI-compat
- **Strength:** Only end-to-end offline stack converging on GGUF as lingua franca; enables zero-cost local loop today.
- **Weakness:** Ollama serializes under concurrency; KV-cache sizing; no native distributed scaling.
- **Maturity:** M3 (70k+ stars llama.cpp, Ollama v0.11+ widespread; GGUF is de-facto local format 2026).
- **Notable decision:** Pure C++ portability over Python kernel performance.

### 15) ONNX Runtime (+ WebGPU/WASM, ExecuTorch)

- **URL:** https://onnxruntime.ai + https://pytorch.org/executorch
- **License:** MIT
- **Architecture:** ONNX graph → execution providers (CPU/CUDA/NPU) + browser WebGPU / WASM + embedded ExecuTorch
- **Core abstraction:** `session.run()` on `.onnx`
- **Strength:** Widest hardware coverage (cloud → browser → micro-controller), model portability.
- **Weakness:** Op coverage gaps, conversion loss, browser auth vs native perf.
- **Maturity:** M3 for ONNX itself; WebGPU/WASM LLM M1-M2; ExecuTorch M2 (1.0 late 2025, billions of devices per Meta).
- **Notable decision:** Edge AI as *compile target* — not deployment afterthought.

---

## F — Memory / RAG / Retrieval

### 16) Chroma / Qdrant / Weaviate / sqlite-vec (Vector tier)

- **URL:** https://docs.trychroma.com, https://qdrant.tech, https://weaviate.io, https://github.com/asg017/sqlite-vec
- **License:** Apache/BSD/MIT mixed
- **Architecture:** HNSW ANN + hybrid (BM25+dense+RRF) + tenant filtering
- **Strength/Weakness:** See Stage 5 matrix: hybrid best; sqlite-vec best for single-process constraint (NEXUS) vs Qdrant for distributed.
- **Maturity:** M2-M3 (Chroma 1.x, Qdrant 1.11, Weaviate 1.28, sqlite-vec 0.1 + growing)
- **Notable decision (sqlite-vec):** Embeddings inside SQLite keeps monolith monolithic.

### 17) Mem0 / Letta / Zep (Memory layers)

- **URL:** https://mem0.ai, https://lettaproject.com, https://getzep.com
- **License:** Apache/MIT mixed
- **Architecture:** Mem0 (universal memory API), Letta (OS-paging hierarchical), Zep (temporal KG + long-term)
- **Strength:** Dedicated memory specialization vs rolling own; handle promotion/forgetting.
- **Weakness:** Memory eval immature; vendor lock risk if memory is product memory.
- **Maturity:** M1-M2 (emerging 2025-26, cited in FutureAGI "popular dedicated layers").
- **Notable decision:** Memory is a *service*, not an afterthought collection.

---

## G — Additional Notables (Beyond the 15 Minimum)

### 18) Litellm + OpenRouter (Provider routing)

- **URL:** https://docs.litellm.ai + https://openrouter.ai
- **License:** MIT (Litellm library) / commercial (OpenRouter managed)
- **Value:** Unified fallback chain across 100+ LLMs — enables NEXUS ordered chain.

### 19) OpenTelemetry + Phoenix + TruLens (Eval/OTel)

- **URL:** https://opentelemetry.io + https://docs.arize.com/phoenix + https://www.trulens.org
- **License:** Apache 2.0
- **Value:** Only vendor-neutral eval substrate for agents (genAI semconv: trace → score).

---

## Landscape-Level Observations

- **O1 [VERIFIED]:** Graph-based control is now the production orthodoxy for workflows needing durability/HIL; conversation-based (AutoGen) is transitioning into unified frameworks rather than disappearing.
- **O2 [VERIFIED]:** MCP + A2A split the interop problem cleanly (tool vs agent) — both needed to avoid framework lock-in.
- **O3 [PARTIALLY VERIFIED, MEDIUM]:** "Most agentic value in prod today is ReAct" (TheAIEngineer 2026) suggests teams over-engineer — validates NEXUS monolith simplicity vs fleet premature complexity.
- **O4 [VERIFIED]:** No framework offers native policy gate — all rely on host bus/registry enforcement (Cordum 2026 governance column blank across 6 frameworks).
- **O5 [VERIFIED]:** Offline-first local stack (GGUF + Ollama + sqlite-vec + ONNX/ExecuTorch) is M3-proven while edge WebGPU/WASM M1-M2 remains maturing — implies two-tier fallback (native first, browser second) any local-first product should adopt.

---

## Sources Cited (Stage 9)

1. **Primary:** LangChain/LangGraph docs, MCP spec docs, Temporal docs, LlamaIndex docs, Haystack docs, ONNX/ExecuTorch
2. **Independent comparisons (cross-validated):**
   - Cordum "AI Agent Frameworks Comparison 2026" — snapshot table 47.7k/56.5k/48.2k stars, 6.39M/1.36M/10.09M downloads, governance blank — [VERIFIED by triangulation]
   - Acolles "AI Agent Frameworks Compared" — layer taxonomy (SDK vs Runtime vs Harness vs Platform)
   - BrightBean "AutoGen vs CrewAI vs LangGraph 2026" — 60% F500 claimed, maintenance mode note, LangSmith best-in-class
   - Codecademy 2025 — legacy anchor
   - Spheron 2026 — decision matrix by team profile
   - Scrimba 2026 — Mastra/TypeScript-native note
   - Pekollective 2026 — head-to-head token efficiency
   - iSwift 2026 — 5-framework comparison (CodeAct, MCP built-in)
   - OpenAgents 2026 — role vs stateful vs conversational vs interop axis
   - Arsum 2026 — governance/support matrix
   - Uvik 2026 — production-fit ranking & disclosure about LangGraph maturity
3. **Adoption signals:** GitHub stars + PyPI downloads — vendor-reported via above, treated as PARTIALLY VERIFIED but convergent.
4. **NEXUS READ-ONLY:** `pyproject.toml` deps (`langgraph>=0.2`, `litellm>=1.74`, `sentence-transformers`, `llama-cpp-python`, `sqlite-vec`, `chromadb`), `docs/architecture/*` constraint docs.

