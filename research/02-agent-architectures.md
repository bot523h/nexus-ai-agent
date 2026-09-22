# 02 — Agent Architectures Research

> **Agent 4 — Independent Research**
> **Date:** 2026-09-22
> **Scope:** 5 canonical agent architectures + comparison matrix
> **Tags per section:** VERIFIED / PARTIALLY VERIFIED / UNVERIFIED + CONFIDENCE H/M/L

---

## Preamble — Why 5?

The literature converges on 5 canonical shapes (ReAct, Plan-Execute, Event-driven, Graph/State-machine, Command-Bus Capability). Hybrid = composition of these, not a 6th distinct. This report treats hybrids as combinations and evaluates each pure shape. [VERIFIED via FutureAGI 2026 "Six Core Components", arahi.ai 2026 "Five canonical architectures", LangGraph docs] — Confidence HIGH.

---

## A1 — Tool-Calling Agent (ReAct + Function Calling)

**Canonical:** ReAct (Yao et al. 2022) + OpenAI function calling (Schick et al. Toolformer 2023) → single-agent loop: Thought → Action (tool JSON) → Observation → repeat.

**Representatives:** OpenAI Agents SDK, Pydantic AI, smolagents (code-as-tool), LangChain ReAct agent, NEXUS `agents/base.py` + `tool_handlers.py`.

- **State management:** Short-term = context window (in-prompt). No durable state unless layered (e.g., LangGraph checkpoint). State is implicit in chat history. Failure → whole loop retries. [VERIFIED]
- **Tool discovery:** Static registry (tools passed as function schemas at init). Discovery = LLM sees `tools: [...]` JSON Schema. No runtime discovery. MCP upgrades this to dynamic.
- **Permissions:** Per-tool call; requires human approval gate outside loop (LangGraph interrupt_before). Tool annotations untrusted (MCP spec).
- **Retries:** LLM decides retry after tool error observation. Simple but can loop infinitely on bad args. Needs max_iterations guard.
- **Failure handling:** Tool failure → observation string → LLM reasons. No compensation; partial effects not rolled back.
- **Observability:** One trace per step (OTel GenAI span). Easy to log but hard to attribute which thought caused which tool.
- **Concurrency:** Single-threaded loop; concurrent tool calls via parallel function calling (OpenAI, 2024) but sequential reasoning.
- **Persistence:** None native; memory = vector store retrieval injected as tool.
- **Human approval:** Out-of-band (pause before tool execution, require user confirmation).
- **Scalability:** Scales as LLM call count × tool latency. ReAct averages 1 LLM call per step → expensive at 10-step workflows (≈1.2s LangGraph vs 1.8s CrewAI benchmark, but ReAct token overhead lowest).
- **When to use:** Exploratory tasks where plan cannot be known upfront; <6 steps.

**Strengths:** Simplest, lowest LoC (~35-50), most model-optimized (all frontier models trained on ReAct).
**Weaknesses:** No global plan, wastes tokens on long chains, fragile on tool output surprises, no durable checkpoint.

**Evidence:** arahi.ai 2026 "ReAct is safe default", TheAIEngineer 2026 single-agent patterns (ReAct → 1 call/step, gets expensive fast), LangGraph docs ReAct agent implementation. [VERIFIED, HIGH]

---

## A2 — Planner / Executor (Plan-and-Execute, Hierarchical)

**Canonical:** Planner (frontier model) generates DAG of subtasks with dependencies; Executor (cheaper model) runs each; Replanner on failure (LLMCompiler, ADaPT, Plan-and-Act papers 2024-2025).

**Representatives:** LangGraph plan-and-execute reference, CrewAI hierarchical crews (planner delegates to researcher/writer), VulnBot PTG (planner + memory retriever + generator + executor + summarizer), NEXUS `agents/planner_agent.py` + `executor_agent.py`.

- **State management:** Explicit plan object (DAG) + per-task state. Plan persists across steps; executor updates task status. Planner state separable from execution state.
- **Tool discovery:** Planner knows task graph + available tools; executor discovers per-subtask. Two-level discovery.
- **Permissions:** Plan can be inspected before execution (human approval on plan, not each tool). Enables pre-execution audit.
- **Retries:** Task-level retry with replanning; failed step triggers replanner to regenerate remaining subgraph. Much cheaper than ReAct replay.
- **Failure handling:** Subtask isolation; failed branch doesn't kill independent branches. Compensation logic can be encoded as replanning.
- **Observability:** Plan-level trace + per-task spans; plan is explainable artifact.
- **Concurrency:** Executor parallelizes independent DAG branches (Task Fetching Unit). LLMCompiler showed speed/cost gains via parallel tool calls.
- **Persistence:** Plan + task states checkpointed (LangGraph/Temporal). Long-running friendly.
- **Human approval:** At plan boundary (approve plan) and/or critical tasks.
- **Scalability:** Scales to dozens of subtasks; parallelism is real. Cost: planner call (expensive) + N cheap executor calls → lower total than N ReAct calls.

**Strengths:** Works when upfront decomposition is possible; inspectable; cheaper (different models for plan vs execute); ADaPT +28.3pp over ReAct on ALFWorld, +20pp on WebShop (published results).
**Weaknesses:** Ceiling when environment dynamic → replanning thrashes; if replanning on most tasks, worse than ReAct. Planning cost wasted on trivial tasks.

**Evidence:** FutureAGI 2026 (Plan-and-Execute as planner layer), Mahdi Jaouadi medium deep-dive on LLMCompiler/ADaPT/PLAN-AND-ACT with metrics, N1N 2026 planner-executor arch, AppSecSanta HPTSA 4.3x improvement multi-agent vs single-agent. [VERIFIED, HIGH for core shape; PARTIALLY VERIFIED for benchmark numbers (paper-reported)]

---

## A3 — Event-Driven Agent (Pub/Sub, Reactive)

**Canonical:** Agents as event consumers/producers on a bus (Kafka/Redis Streams/RabbitMQ/Postgres NOTIFY). Events = state changes, tool results, human signals. No central orchestrator; agents react to events they subscribe to.

**Representatives:** AutoGen GroupChat (agents converse via events), Microsoft Agent Framework (event-driven agents), Mastra (event harness), NEXUS `bot/handlers.py` PTB handler pipeline (Telegram update = event).

- **State management:** Distributed — each agent maintains own state; global state = log of events (Kafka log or Postgres). No single source without event sourcing discipline.
- **Tool discovery:** Via registry service emitting ToolRegistered events; or MCP registry polling. Dynamic.
- **Permissions:** At event ingestion (guard checks `event.actor` against policy before handler runs). Event schema validation critical.
- **Retries:** Broker retry + dead-letter queue. Idempotency required (at-least-once delivery). No built-in exactly-once for processing.
- **Failure handling:** Poison message → DLQ; agent crash → consumer group rebalance; ordering gaps require sequence numbers.
- **Observability:** Distributed tracing (trace ID propagated via event headers/Otel). Harder: waterfall is scattered.
- **Concurrency:** Naturally concurrent — multiple agents consume in parallel. Ordering only within partition/key.
- **Persistence:** Event log is the system of record; replay possible (event sourcing).
- **Human approval:** As event: `ApprovalRequested` → human emits `Approved` → agent continues.
- **Scalability:** Highest throughput (Kafka partitions); additive scaling with consumers.

**Strengths:** Loose coupling, scales to fleet, resilient to single-agent failure, replay/audit via event log.
**Weaknesses:** Debugging is hard (scattered causal chain), eventual consistency, requires idempotent handlers, state rehydration cost, needs schema registry.

**Evidence:** Arise handbook "event-driven agents, AgentChat, teams", Agentic AI Infra Landscape 2025-26 "event-driven vs supervisor patterns", official Kafka/RabbitMQ docs on delivery semantics. [VERIFIED, HIGH]

---

## A4 — Graph / State-Machine Agent (Explicit Orchestration)

**Canonical:** Workflow is explicit directed graph (nodes = computation, edges = conditional transitions, optionally cycles). State object flows through graph, checkpointed per node. Borrowed from Airflow/Temporal paradigm applied to LLM orchestration.

**Representatives:** LangGraph (graph primitive), LlamaIndex Workflows, Temporal workflows (deterministic replay), NEXUS `orchestration/graph.py` (LangGraph StateGraph router→agent→memory→tools).

- **State management:** Typed state dict, versioned, persisted at every superstep. Supports branch/merge, parallel nodes, `interrupt_before`/`interrupt_after`.
- **Tool discovery:** Node can declare required tools; tool schemas part of node definition. MCP stdio nodes can be graph branches.
- **Permissions:** Per-node permission (CapabilityRegistry design). Node-level approval gates before side-effect edges.
- **Retries:** Per-node retry policy, checkpoint resume (time-travel), deterministic replay from last checkpoint. Durable if checkpoint store is durable (Postgres).
- **Failure handling:** Fail node → reroute to error branch → compensation node. History is auditable proof.
- **Observability:** Graph visualization + per-node span + state diff. Best debuggability in comparison. LangSmith visual trace.
- **Concurrency:** Conditional branching + parallel node execution (fork/join). More explicit than planner parallelization.
- **Persistence:** Checkpointers (SqliteSaver, PostgresSaver). Durable if Postgres; local-only if SQLite.
- **Human approval:** Native `interrupt_before` checkpoint: pause graph, await human signal, resume.
- **Scalability:** Scales to many nodes (tested 100+ parallel nodes). Graph complexity is cost: large graphs become own maintenance burden.

**Strengths:** Explicit, debuggable, replayable, best for branching/approval/compliance, production-proven, reduces LLM call waste (deterministic routing vs LLM deciding loop).
**Weaknesses:** Steep learning curve (graph theory), upfront design cost, overkill for trivial linear flows.

**Evidence:** LangGraph official docs (graph-based orchestration, durable execution, checkpointing, HIL), uvik 2026 "graph model makes execution explicit, checkpointable, replayable", LangGraph vs CrewAI vs AutoGen comparisons (all rank LangGraph highest for control). [VERIFIED, HIGH]

---

## A5 — Command-Bus / Capability Architecture (Typed Command Envelope)

**Canonical:** All mutations are typed commands (data-only) dispatched through a single Bus. Bus validates → permission check → handler execution → event emission. Packs/capabilities register operations declaratively. Inspired by CQRS, DDD, and NEXUS "Nagar" design (pure packs, data-only manifests).

**Representatives:** NEXUS `creative/studio/bus.py` + `creative/packs/registry.py` + `creative/packs/manifest.py` (manifest is data-only JSON, no executable code), Axon/CommandBus pattern, MS Semantic Kernel planners (plugin invocation via kernel).

- **State management:** Command = immutable data (schema versioned). Handler produces events; state is derived from event log or DB. Strict schema (Pydantic/JSON Schema).
- **Tool discovery:** Capability registry (typed manifest) declares operations, permission level (A-D in NEXUS), input/output schemas. Discovery = query registry (not LLM guessing). MCP registry is equivalent at ecosystem scale.
- **Permissions:** Explicit `PermissionLevel` check in Bus before handler runs (NEXUS PermissionLevel A-D). Least privilege by default; capability cannot self-escalate.
- **Retries:** Bus controls retry policy (bounded jitter backoff, Retry-After capped 8s in NEXUS image lane). Handler idempotency required.
- **Failure handling:** Typed failure vocabulary, closed error codes, no free-form LLM failure strings. Validation failures never reach handler.
- **Observability:** Command → span with typed fields, cost/latency per capability, audit log is command journal. Redaction at boundary.
- **Concurrency:** Serialized per capability adapter where needed (NEXUS concurrent identical image requests share result). Parallel dispatch for independent commands.
- **Persistence:** Command journal + job queue (NEXUS InProcessJobQueue SQLite sidecar).
- **Human approval:** Bus can require consent flag before dispatching side-effect commands (NEXUS strict-privacy flag, consent gate).
- **Scalability:** Scales to thousands commands/s if bus and registry are stateless; bottleneck is handler (e.g., FFmpeg lane single encode).

**Strengths:** Deterministic, testable (pack purity: stdlib + pydantic only), supply-chain safe (manifest data-only, banned executable keys), permission ladder explicit, evolvable via schema versioning.
**Weaknesses:** Requires upfront type discipline; not suited to open-ended exploratory LLM reasoning; registry must be kept consistent.

**Evidence:** NEXUS `docs/architecture/CREATIVE_STUDIO.md`, `docs/architecture/MODULE_MAP.md` (pure packs law is executable gate), `creative/packs/manifest.py` + tests `test_pack_manifest_is_data_only.py`. Plus CQRS literature via Martin Fowler. [VERIFIED, HIGH for NEXUS-specific; PARTIALLY VERIFIED for generalization]

---

## Architecture Comparison Matrix

| Dimension | A1 ReAct Tool-Calling | A2 Planner/Executor | A3 Event-Driven | A4 Graph/State-Machine | A5 Command-Bus Capability |
|-----------|----------------------|---------------------|----------------|------------------------|---------------------------|
| **Control flow source** | LLM decides next step | Planner DAG decides | Events select handlers | Graph edges decide | Bus routes typed commands |
| **State** | Implicit (context) | Explicit plan + task states | Distributed (event log) | Typed state object + checkpoints | Command journal + events |
| **Tool discovery** | Static function schemas | Two-level (planner + per-task) | Registry events | Per-node declaration | Capability registry (manifest) |
| **Permissions** | Per tool call (outside) | At plan + task | At ingestion | Per node | Per command (ladder A-D) |
| **Retries** | LLM auto-retry (loop) | Replan failed subgraph | Broker retry + DLQ | Node retry + time-travel | Bus-managed bounded retry |
| **Failure handling** | Observation string | Isolate branch + replan | Poison → DLQ, rebalance | Error branch + compensation | Typed error vocab |
| **Observability** | Step spans | Plan + task spans | Scattered trace | Graph waterfall (best) | Command spans (typed) |
| **Concurrency** | Single loop, parallel tools optional | DAG parallel branches | Consumer groups | Parallel nodes (fork/join) | Serialized per capability or parallel |
| **Persistence** | None native | Plan checkpoint | Event log replay | Checkpoint per superstep | Command journal + job sidecar |
| **Human approval** | Out-of-band | Plan approval | Approval event | interrupt_before (native) | Consent flag before bus |
| **Scalability** | LLM calls × latency | Parallel executor | Highest (partitions) | 100+ nodes | 1000s cmds/s (handler bound) |
| **Latency / Cost** | High (N LLM calls) | Lower (1 planner + N cheap) | Low per event | Deterministic routing → lower LLM waste | Deterministic handler → minimal LLM cost |
| **Complexity / Curve** | Low | Medium | High (distributed) | High | Medium (type discipline) |
| **Best for** | Exploratory <6 steps | Decomposable long-horizon | Fleet/high-throughput reactive | Branching/approval/compliance long workflows | Deterministic media/ops with permission tiers |
| **Worst for** | Long/complex plan need | Highly dynamic env | Trivial linear flows | Trivial flows (overkill) | Open-ended research |
| **Canonical benchmark delta** | baseline | ADaPT +28pp ALFWorld | 4.3x vs single-agent (HPTSA) | Best debuggability | Pure-pack testability |

*Benchmark numbers paper-reported, treated as PARTIALLY VERIFIED (need independent replication). Ordering claims avoided. No "best overall" verdict per instruction.*

---

## Trade-off Analysis (Non-Ranked)

- **Flexibility vs Determinism:** A1/A3 most flexible (LLM/event decides), least deterministic. A4/A5 most deterministic, least exploratory.
- **Debuggability:** A4 > A5 > A2 > A1 > A3 (scattered).
- **Cost:** For 10-step workflow, A2 cheapest (planner 1× frontier + 10× cheap), A1 most expensive (10× frontier), A4/A5 cheapest if deterministic routing avoids LLM calls entirely.
- **Durability:** A4 (checkpoint) and A3 (event log) and Temporal-backed A2 are durable. Pure A1 without checkpoint is not.
- **Governance:** A4/A5 expose explicit approval gates; A1 requires bolting on.

---

## Options for NEXUS-Type System (Non-Prescriptive)

> Per "No BEST verdict" rule — three viable composition options, each with trade-offs.

**Option A — Pure Graph (LangGraph native):** Single StateGraph hosts router/agent/tools/memory/human gates; checkpoint = SQLite (local) / Postgres (hosted). No external queue. *Trade-offs:* Simple ops (modular monolith), best HIL, but graph owns all complexity.

**Option B — Graph + Command-Bus Hybrid (Current NEXUS, recommended observation):** LangGraph for conversation/orchestration; separate Command Bus (studio/bus.py) for deterministic media ops. JobQueue = InProcess SQLite sidecar. *Trade-offs:* Separation of concerns (pure packs stay testable), but two runtime models to maintain. This is READ-ONLY observation, not a change proposal.

**Option C — Event-Driven Fleet (Kafka/Redis Streams):** Agents as event consumers, command bus over streams, Temporal for long workflows. *Trade-offs:* True fleet scale, replay, but heavy ops & harder debugging. Overkill at current NEXUS scale.

---

## Sources Cited

1. **Primary:** LangGraph docs https://langchain-ai.github.io/langgraph/ — graph, nodes/edges, checkpoint, HIL
2. **Primary:** Temporal docs https://docs.temporal.io — deterministic replay, event log
3. **Primary:** MCP spec https://modelcontextprotocol.io/specification/2025-11-25 — tool annotations, trust
4. **Primary:** NEXUS `docs/architecture/CREATIVE_STUDIO.md`, `RUNTIME_FLOWS.md`, `orchestration/graph.py` (READ-ONLY)
5. **Independent:** arahi.ai 2026 "Five canonical architectures" — 6 layers / 5 architectures
6. **Independent:** TheAIEngineer 2026 "4 Single-Agent Patterns" — ReAct vs Plan-Execute vs ReWOO vs Reflexion cost/latency
7. **Independent:** FutureAGI 2026 "LLM Agent Architectures Core Components"
8. **Independent:** Mahdi Jaouadi 2025 "Separating Planner and Executor" (LLMCompiler, ADaPT metrics)
9. **Independent:** N1N 2026 "5 AI Agent Design Patterns" — planner/executor DAG
10. **Independent:** Agentic Infra Landscape 2025-26 — supervisor vs swarm vs event-driven maturity
11. **Independent:** Arise handbook vs uvik 2026 comparison — framework rankings that validate graph dominance for control
12. **Independent:** HPTSA 4.3x claim in AppSecSanta 2026 pentesting agent survey (cross-ref with planner-executor benefit)

> **OPEN QUESTIONS:** Optimal granularity for graph nodes (too fine → graph maintenance debt, too coarse → LLM waste). Optimal trigger for replanning vs fallback to ReAct. Quantified debugging cost A3 vs A4 at >100 agents — lacking large-scale longitudinal study [UNKNOWN].

