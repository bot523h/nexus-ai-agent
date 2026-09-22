# 08 — Observability + Evaluation Research

> **Agent 4 — Independent Research**
> **Date:** 2026-09-22
> **Status:** VERIFIED where spec-cited, CONFIDENCE H/M/L

---

## Preamble — Why Agents Need Different Observability Than Services

Traditional app observability measures **latency, errors, saturation**. Agent observability also measures **tokens, cost, tool success, hallucination, instruction adherence, and long-horizon goal completion**. Without both, you can be "up" (200 OK) while burning $500/day and hallucinating 30% of answers. [VERIFIED via OTel GenAI semconv + Phoenix/TruLens 2026 guides.]

---

## 1) Observability Primitives Surveyed

| Signal | LLM/Agent data it carries | Standard | Storage example | Primary use |
|--------|---------------------------|----------|-----------------|-------------|
| **Trace (tree of spans)** | End-to-end agent run: router → retriever → LLM → tool → LLM → response, scoped by `session_id` | **OpenTelemetry (GenAI semconv)** | Jaeger / Honeycomb / Grafana Tempo | Debug one slow/failed agent run |
| **Span** | Single LLM/tool/retriever operation: `gen_ai.*` attrs (model, provider, tokens, cost), latency, status | GenAI semconv (`gen_ai.system`, `gen_ai.request.model`, `gen_ai.usage.*`, `gen_ai.response.*`) | Same trace store | Inspect one operation |
| **Metric** | Aggregates: counts, latency histograms, token totals, cost/day, cache hits, success rates | OTel Metrics (OTLP) + Prometheus | Prometheus / Datadog / SigNoz | Dashboards, alerting, capacity |
| **Log** | Structured event records: inputs/outputs with PII redaction, errors | OTel Logs + structlog | Loki / Elasticsearch / SigNoz | Audit, compliance, detailed debug |

**Maturity [VERIFIED, HIGH]:** OTel graduated CNCF; GenAI semconv stable 2025; `openllmetry` (7.2k stars), `OpenLIT` (one-line), `Phoenix` (Arize), `TruLens` (Snowflake/TruEra) all emit OTel spans.

**Primary:** opentelemetry.io + GenAI semantic conventions; docs.arize.com/phoenix; docs.sigNoz.io; traceloop-sdk (openllmetry)
**Independent:** GetMaxim "OTel for LLM Observability" (span model), MintMCP "OTel for AI Agents in MCP Workflows" (MCP tool invocation tracing + 2.2x reliability claim), OpenObserve agent tracing (payload size + sampling dilemma)

---

## 2) Tracing — How to Trace an Agent End-to-End

**Model:**

```
TraceId: sess_9f31 (maps to NEXUS thread_id / checkpoint_id)
├─ Span: api/webhook.post             (B6 ingress, auth)
├─ Span: bot/guard.check              (B1 access guard)
├─ Span: agent/router.decide          (LLM call #1 — gen_ai.usage)
├─ Span: retriever/pgvector.search    (RAG, context relevance tag)
├─ Span: llm/anthropic.completion     (gen_ai.system=anthropic, tokens=6.2k, cost=$0.0186)
├─ Span: tool/search.invoke           (tool lookup_order, status OK/ERROR)
├─ Span: render/ffmpeg.exec           (lane, args hash, probe_video result, sha256)
└─ Span: bot/reply.send               (egress)
  └─ Evaluation: faithfulness=0.91, grounded=True
```

**Key instrumentation points for NEXUS-class system (READ-ONLY mapped):**

| NEXUS component | Span name | Attributes |
|-----------------|-----------|------------|
| `api/app.py` webhook | `api.webhook.post` | `http.status_code`, `telegram.secret_token_valid` (bool, never token) |
| `bot/access_guard.py` | `auth.guard` | `user.allowed`, `block_reason` |
| `orchestration/graph.py` | `agent.router` / `agent.llm` | `gen_ai.system`, `gen_ai.request.model`, `gen_ai.usage.input_tokens` |
| `adapters/in_process_job_queue.py` | `queue.enqueue` / `queue.dequeue` | `job.type`, `job.attempt`, `job.latency` |
| `creative/rendering/executor.py` | `render.ffmpeg.exec` | `render.profile`, `render.duration_s`, `render.probe_ok` |
| `llm/` via LiteLLM | `llm.complete` | `gen_ai.system`, cost, fallback provider |
| `storage/checkpoint_lifecycle*.py` | `lifecycle.reconcile` | `checkpoint.age`, `checkpoint.action` |

**Sampling dilemma [VERIFIED, HIGH]:** Full prompt+response per span is KBs. Billing by span/GB with limited retention → teams sample → then cannot diagnose tail incidents. OpenObserve explicitly documents this cost pressure. **Solution:** head-sample 100% for errors/policy violations, tail-sample 1-10% for successes, plus per-session sampling (keep whole session if one turn fails).

**MCP-specific:** MintMCP LLM Proxy shows tracing every MCP `tools/call` + `resources/read` + `bash` + `file` op — OTel extends this across backend.

---

## 3) Metrics — Six KPIs That Actually Matter for Agents

MintMCP proposes six essential agent metrics — synthesized and cross-validated:

| KPI | Definition | Instrument | Alert threshold (suggested starting point, tune via baseline) |
|-----|------------|------------|--------------------------------------------------------------|
| **Tool Call Success Rate** | `successful_tool_calls / total` | Counter `gen_ai.tool.success` | <98% sustained → page |
| **LLM Latency Distribution** | p50/p95/p99 of `llm.duration` | Histogram | p95 > SLO (e.g., 3s for chat, 30s for render) |
| **Token Usage per Run** | `input + output` tokens per session | Histogram + Sum | >budget × 2 → anomaly |
| **Cost per Run / per Day** | USD computed cost (not invoice) | Sum `gen_ai.cost.usd` | >$ threshold (estimated, with disclaimer) |
| **Agent Loop Iterations** | ReAct cycles before completion | Histogram | >6 cycles → inefficiency |
| **Context Window Utilization** | `tokens_used / window_size` | Gauge | >80% → truncation/hallucination risk |

Additional metrics for NEXUS observability (from `docs/architecture/OBSERVABILITY.md` READ-ONLY): structured events (`image_generation_cost`, `image_gen.retry`), healthz, provider cooldown counters, rate limiter denies, PII redaction events.

**Independence note:** Galileo survey "2.2x better reliability with stronger observability" [MintMCP cites] is self-reported vendor survey — treat as PARTIALLY VERIFIED, not controlled study.

---

## 4) Instrumentation — One-Line vs Gateway vs Manual

| Method | Where spans emitted | Pros | Cons |
|--------|-------------------|------|------|
| **OpenLLMetry (`traceloop-sdk`)** | App code via SDK auto-wrapping LangChain/LangGraph | One line, 40+ frameworks, collects traces/metrics/logs | App must deploy SDK, sampling in-process |
| **OpenLIT** | One-line OTel-native auto-instrumentation for 20+ LLMs/VectorDBs | Minimal code change | Less control per agent |
| **Bifrost AI Gateway (Maxim AI)** | Gateway layer (Go) before LLM | Zero app code change, Prometheus `/metrics` + OTLP natively, async no added latency | Only LLM traffic, not full agent graph |
| **Manual OTel SDK** | Framework integration (LlamaIndex OTel example) | Full control, gen_ai attrs explicit | More boilerplate |
| **LangSmith / Phoenix** | Agent framework exporters | Deep agent-specific views, eval integration | Vendor coupling |

**NEXUS mapping [READ-ONLY]:** Current observability uses `structlog` + `infrastructure/observability/` + structured events. OTel not yet adopted per inspection — opportunity for future (see mitigation in Stage 10).

---

## 5) Evaluation — From Observability to Quality

Observability answers "what happened"; **evaluation answers "was it good?"** Both need OTel trace as carrier for scores.

### 5.1 RAG & Memory Evaluation Frameworks

| Tool | Scope | Metrics emitted as span attrs/evals | OTel native? | License |
|------|-------|--------------------------------------|--------------|---------|
| **TruLens** (Snowflake/TruEra) | RAG | groundedness, context relevance, answer relevance, coherence, toxicity | Yes (OTel) | Apache 2.0 |
| **Phoenix** (Arize) | LLM + Agent + RAG | hallucinations, tool use correctness, ranking, span cost | Yes | Apache 2.0 + OSS/SaaS |
| **RAGAS** | RAG synthetic | faithfulness, answer correctness, context precision | Partial | Apache 2.0 |
| **Evidently AI** | ML+LLM monitoring | drift, data quality, regression | Yes | Apache 2.0 |
| **LangSmith** | LangChain/LangGraph agent | datasets, regression, trace scoring | Native (OTel compatible) | Commercial SaaS |
| **Future AGI (traceAI + Agent Command Center)** | Agent delib/planning/tool eval | deliberation traces, plan scoring, guardrails | Native | Commercial |

**Primary:** TruLens RAG triad (groundedness+context relevance+answer relevance), Phoenix OSS docs, Evidently LLM eval

### 5.2 Core Agent Evaluation Dimensions (Synthesis)

| Category | Metric | Formula / Judge | Threshold example* |
|----------|--------|-----------------|--------------------|
| **Correctness** | Answer relevance | Cosine(Q,A) + LLM judge | >0.80 |
| | Faithfulness / groundedness | LLM judge vs retrieved citations | >0.85, toxic → 0 |
| | Factual correctness | Exact/semantic vs gold or retrieved context | >0.75 |
| | Hallucination rate | % answers contradicted by context | <5% |
| **Tool** | Tool success rate | `ok_calls / total` | >98% |
| | Tool arg validity | Schema validation pass | 100% |
| | Excessive agency (unauthorized tool) | Policy check | 0% |
| **Performance** | LLM latency p95 | Span histogram | <3s chat, <30s render |
| | Cost per task | Sum cost meter | < budget/ task |
| **Safety** | Prompt injection blocked | Red team set | >95% |
| | PII leakage | Canary dataset | 0 |
| | SSRF/system prompt leak | Probe set | 0 |
| **Regression** | Pass rate on golden dataset | CI replay of `datasets/golden/*` | 100% on MUST-pass, >95% overall |

*Thresholds are starting points; team must set from baseline. Use SLOs, not universal absolutes.*

---

## AGENT_EVALUATION_FRAMEWORK (Deliverable)

Structure this as a runnable framework, not just a metrics list.

### F1 — Four Test Suites (Run Order)

```
1. Unit / contract (fast, <60s)
   ├─ JSON Schema valid for every tool input/output
   ├─ Pack manifest is data-only + no unknown op
   ├─ Permission ladder enforced in bus before handler
   ├─ Redaction covers every PII field (structured event tests)
   └─ Idempotency: run handler twice with same key → same result

2. Integration (medium, <10m)
   ├─ RAG golden set: 100 Q/A pairs with expected contexts (context relevance + groundedness)
   ├─ LLM golden set: 200 prompts covering chat/code/vision/summarize/translate
   ├─ Tool chain: every critical path (search→fetch→summarize→reply)
   ├─ SSRF probe set (private ranges, metadata IP, redirects)
   └─ Queue resilience: kill worker mid-job → resume via reconciler

3. Evaluation / LLM-as-judge (slow, <30m, sampled in CI)
   ├─ RAG triad (TruLens) on retriever output
   ├─ Hallucination rate on 500-sample held-out
   ├─ Cost/latency histogram regression vs baseline
   └─ Security red-team (prompt injection, tool poisoning canaries)

4. Load / soak (nightly)
   ├─ p95 latency under 10 concurrent agents × 1k tool calls/day shaped
   ├─ Memory/queue growth under 24h run (leak detection)
   └─ Cost anomaly detection (>$ budget × 2)
```

### F2 — Regression Strategy

- **Golden datasets are code.** Store in `tests/eval/golden/*.jsonl` with `Q, expected_context, expected_tool, expected_answer_citations`. PR that changes behavior must update dataset + get review.
- **Snapshot eval:** Every CI run emits OTel trace → Phoenix stores per-span eval scores → compare vs last green `main` run. Fail if any metric regresses >2σ from rolling 7-day median without explicit dataset update.
- **Provider drift guard:** LiteLLM cost meter + model version tracked per span; new model version triggers full eval suite (since tool-calling quality is model-specific).

### F3 — Acceptance Thresholds (Template — tune from observed baseline)

| Category | Metric | Green | Yellow (review) | Red (block) |
|----------|--------|-------|-----------------|-------------|
| Tool | success_rate | ≥99% | 98–99% | <98% |
| RAG | groundedness | ≥0.85 | 0.75–0.85 | <0.75 |
| Safety | injection_blocked | ≥98% | 90–98% | <90% |
| Cost | cost_per_1k_calls Δvs baseline | ≤±10% | ±10–25% | >±25% without approved estimate |
| Latency | p95 LLM | ≤SLO | SLO×1.5 | >SLO×1.5 |

### F4 — The Evaluation Pipeline (How Data Flows)

```
traceId → OTel Collector → (Traces → Phoenix / Jaeger) 
                        → (Metrics → Prometheus → Grafana)
                        → (Logs → Loki / SigNoz)
                        ↘︎ Evals (TruLens/Phoenix) attached as span events
                           → Aggregate dashboard (faithfulness histogram)
                           → Alert: hallucination_rate > 5%
                           → CI gate: eval_scores.golden_pass < 100% → block
```

### F5 — What NOT to Measure Blindly

- **Don't measure raw LLM scores without retrieval context** — inflates performance.
- **Don't benchmark "best framework" latency without same model + same tool set** — comparison articles converge on same ordering (LangGraph < CrewAI < AutoGen) because benchmark controls were uneven.
- **Don't treat cost estimate as invoice** — after provider bills, reconcile via provider dashboard (NEXUS DECISION_LOG pattern).

---

## 6) Correlation — Tying Agent, Infra, and Data

The agent was "slow" diagnosis in OpenObserve example: trace waterfall shows `postgres lock wait 3.4s (41% of 8.42s)` ⇒ bottleneck was vector DB / DB, not LLM. **Require single traceId across webhook→graph→retrieval→tool→DB→render→reply.**

NEXUS health tip: `api/app.py::healthz` is DB-free, so infra alerting distinguishes "service up but DB down" vs "service down." Dash must correlate queue depth (`InProcessJobQueue` length) with GPU saturation / pod restarts.

---

## 7) gaps & Unknowns

- **GAP1:** No standardized "agent goal completion" metric beyond human rating — still subjective [UNKNOWN].
- **GAP2:** Effective open-source cost-per-trace storage pricing at scale (OpenObserve claims up to 140× vs Elasticsearch) is vendor-benchmarked; independent replication needed [PARTIALLY VERIFIED].
- **GAP3:** Evaluator LLM bias (LLM-as-judge favors its own provider's outputs) — known confound, not solved [PARTIALLY VERIFIED].
- **GAP4:** NEXUS OTel adoption cost to ship with SQLite sidecar model — low runtime cost but SDK + collector maintenance is process cost not yet quantified [PARTIALLY VERIFIED].

---

## Sources Cited

1. **Primary:** opentelemetry.io + OTel GenAI Semantic Conventions + https://developers.llamaindex.ai/python/framework/module_guides/observability/ (LlamaIndex OTel `LlamaIndexOpenTelemetry` example)
2. **Primary:** Phoenix, TruLens (feedback functions: groundedness/context relevance/answer relevance), Evidently AI docs
3. **Primary (NEXUS READ-ONLY):** `docs/architecture/OBSERVABILITY.md` (metrics + structured logs, PII redaction), `infrastructure/observability/`, `creative/rendering/executor.py` (probe/sha256 evidence), `VERSION` + `pyproject.toml` (`structlog>=24`)
4. **Independent:** GetMaxim 2026-09-02 "OTel for LLM Observability: Traces and Metrics" (trace/span/metric/log model, Bifrost gateway OTLP+Prometheus, session-grouped traces)
5. **Independent:** MintMCP 2026-04-16 "OTel for AI Agents in MCP Workflows" (six KPIs, auto-instrumentation libraries, 2.2× reliability survey claim & distributed tracing)
6. **Independent:** Firecrawl 2025-12-02 "Best LLM Observability Tools 2026" (TruLens/Phoenix/openllmetry comparison + 7.2k stars, gateway vs eval split)
7. **Independent:** OpenObserve "LLM & Agent Observability" (payload size cost, postgres lock waterfall trace 4f2a91c8 41%, infrastructure correlation, 140× storage claim)
8. **Independent:** LangSmith / FutureAGI 2026 bottleneck analysis (agent loop iterations, context utilization as KPIs)

