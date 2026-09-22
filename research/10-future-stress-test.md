# 10 — Future Architecture Stress Test

> **Agent 4 — Independent Research**
> **Date:** 2026-09-22
> **Method:** Compose findings Stages 1-9 ⇒ project each scenario onto NEXUS-class architecture (offline-first, modular monolith, LangGraph + Command-Bus, SQLite sidecar, liteLLM fallback chain, consent-gated egress, single FFmpeg lane)
> **Per scenario:** Expected bottleneck | Failure mode | Architecture pressure | Required capability | Potential mitigation
> **No code ownership, no Nagar modification — READ-ONLY observations only.**

*Note on scope assumption:* Stress dimensions are stated as load shapes, not demands to replace monolith with fleet; pressure maps show where monolith invariant may strain, not a mandate to violate frozen constraints without ADRs.*

---

### Scenario A — 10 Users (Current/Dev)

- **Expected bottleneck:** None (monolith warm).
- **Failure mode:** Operator misconfiguration (empty allow-list interpretation, missing env, FFmpeg not found).
- **Architecture pressure:** Near zero — SQLite + in-process queue + single worker handles tens of concurrent chats.
- **Required capability:** `nexus smoke` + `.env.example` completeness + deny-by-default guard tested.
- **Potential mitigation:** Keep local smoke gate in CI (`nexus smoke` already exists); ensure `NEXUS_ALLOWED_USER_IDS` empty ⇒ nobody (not everybody) — already correct per SECURITY.md.

**Research verdict [VERIFIED]:** At this scale all Stage 7 queues over-provisioned; SQLite sidecar is optimal — no broker added. CONFIDENCE HIGH.

---

### Scenario B — 1,000 Users (Active Telegram Community)

- **Expected bottleneck:** SQLite write serialization (job enqueue + checkpoint writes + message persistence share one disk DB), webhook concurrency (PTB + FastAPI), rate limiter + LLM provider quotas (Gemini 15 RPM/1500 req/day free tier).
- **Failure mode:** DB lock contention → job enqueue queueing; provider 429 → image/text generation starvation; cooldown cascade.
- **Architecture pressure:** Medium — modular monolith still viable but needs per-component backpressure.
- **Required capability:**
  - **Queue:** WAL + sidecar isolation (already `.jobs.sqlite3` sidecar, not main DB — correct separation) + busy timeout.
  - **Rate:** Bot-level rate limiter (`bot/rate_limiter.py`) + per-provider cooldown (Gemini 429 + jitter cap 8s pattern portable to LLM lane).
  - **Observability:** Per-provider metrics + queue depth Prometheus gauges (from Stage 8 KPI set).
- **Potential mitigation:** Keep monolith; add per-provider token bucket (code in LLM router) + job batching coalescence (identical SHA256 requests share result — already done for image adapter, extend to LLM). No Temporal/Kafka needed yet. If SQLite contention measured >100ms p95 writes, split `checkpoint` SQLite from `jobs` sidecar (already split) is sufficient.

**Research verdict [PARTIALLY VERIFIED, MEDIUM]:** Similar community bots at this scale run on SQLite—feasible but must meter; no fleet needed.

---

### Scenario C — 100,000 Users (Viral Growth, Hacker News Spike)

- **Expected bottleneck:** Modularity wall — single-process cannot horizontally fan out; DB (whether SQLite or Postgres) hot shards; webhook instance count (Koyeb scale-to-zero vs always-on polling).
- **Failure mode:** DB WAL pressure → vacuum stalls (Postgres) or `database is locked` bursts (SQLite); cold-start latency amplification on webhook mode; Telegram floods API rate.
- **Architecture pressure:** **Constitutional** — frozen constraint "modular monolith: no Celery/Redis/distributed queue" is directly stressed. This is an *architecture event* per `docs/architecture/OVERVIEW.md` §4 (changing constraint = ADR + DECISION_LOG). Not a bug, a designed boundary.
- **Required capability (to stay monolith):** Postgres/Neon for checkpoints (already topology for webhook scale-to-zero in OVERVIEW.md §7), R2 blob offload, read replicas, connection pooling (`asyncpg`, `psycopg[pool]` already pinned), sharded job claim via Postgres `SKIP LOCKED` (replace sidecar pattern with Postgres at this scale).
- **Required capability (to go fleet):** Would need ADR to lift monolith constraint → Temporal or Kafka + stateless MCP (2026-07-28 enables round-robin LB) + distributed checkpoint. This is the *policy choice*, not engineering emergency.
- **Potential mitigation (non-violating):** Vertical scale first (bigger host, PG Neon autoscale, R2 caching) + LiteLLM fallback chain shedding to cheaper providers + poll vs webhook topology switch already documented (OVERVIEW §7 deployment topologies).

**Research verdict [VERIFIED, HIGH]:** Monolith invariant explicitly trades fleet scale for deploy simplicity; stress at 100k is *by design* requiring decision event, not hidden surprise. ADR template exists (`docs/architecture/adr/`).

---

### Scenario D — 1M Tool Calls / Day (~12/s sustained, ~50/s peak)

- **Expected bottleneck:** LLM API quota/cost, SQLite `sqlite-vec` ANN latency at high QPS, render lane single encode (one FFmpeg process at a time).
- **Failure mode:** Cost blowout (est. vs invoice gap), token quota throttling, queue saturation → head-of-line blocking between chat vs render jobs, OTel trace storage saturation (KB/span × 1M = GBs/day).
- **Architecture pressure:** High on event/queue/trace side; medium on LLM.
- **Required capability:**
  - **Deduplication:** SHA256 cache + result coalescence (already in image lane 16 entries/32 MiB/1h; generalize to tool results).
  - **Queueing discipline:** Separate lanes (chat lane vs render lane already exists) with priority + backpressure — don't let image beat renders starve chat.
  - **Cost governance:** `image_generation_cost` pattern (once per successful HTTP + disclaimer) generalized to LLM meter; reconciler vs provider invoice (NEXUS lesson).
  - **Observability:** Sampling: head 100% on errors, tail 1% on success, per-session complete trace (Stage 8).
- **Potential mitigation:** Priority queues + LRU caches + provider fallback shedding (cheaper models for cheap tasks: frontier planner vs cheap executor pattern from Stage 2) + batch summarization for high-volume RAG.

**Research verdict [PARTIALLY VERIFIED]:** No marketplace benchmark found for 1M vector+LLM calls/day on SQLite single-host; estimate relies on Postgres `SKIP LOCKED` literature + queue alternatives analysis (Stage 7). Needs measured benchmark on NEXUS hardware.

---

### Scenario E — Offline-First Users (Air-gapped, Termux, Strict-Privacy)

- **Expected bottleneck:** Model fit into device memory (GGUF Q4_K_M + KV cache headroom), local translate/speech availability.
- **Failure mode:** Local model insufficient reasoning → wrong answers but no cloud allowed due to `STRICT_PRIVACY=true` ⇒ must not degrade silently to cloud (must return typed "capability unavailable").
- **Architecture pressure:** Low for constraint, high for UX (subset of features must fail closed with clear message).
- **Required capability:**
  - Capability discovery (hardware probe → model fit) + feature gate via extras (`faster-whisper` `[speech]`, `argostranslate` `[translate]`, `pypdf` `[pdf]`).
  - Consent-gated egress (existing tri-state + strict flag).
  - Packaging: Ollama air-gap tarball pattern or direct GGUF fetch with SHA256.
- **Potential mitigation:** Typed `UnavailableAdapter` pattern already implements fail-closed (observed in `creative/caption/unavailable_adapter.py`); extend to LLM — `AllProvidersUnavailable` with reason `strict_privacy` is correct failure, not fallback to cloud. Document model-size matrix in `LLM_PROVIDERS.md`.

**Research verdict [VERIFIED, HIGH]:** Offline-first is feasible end-to-end (Stage 6 F4); NEXUS design already supports it.

---

### Scenario F — Multiple AI Providers (LLM Routing, Local → Groq → Gemini → OpenRouter fallback chain)

- **Expected bottleneck:** Provider schema drift (tool JSON differences), retry storm amplification, cost attribution fragmentation.
- **Failure mode:** Provider A 5xx → retry with jitter hits Provider B → both throttle → cascading 429 → user-visible error even though one provider was healthy.
- **Architecture pressure:** Medium — routing layer adds one proxy hop.
- **Required capability:**
  - **Unified proxy:** LiteLLM-style routing (already in NEXUS via `litellm>=1.74`) with ordered chain, per-provider retry bounded + capped `Retry-After` (8s pattern).
  - **Schema normalization:** Pin tool schemas per provider; validation before dispatch (*not* after) to avoid wasted calls.
  - **Observability:** `gen_ai.system` attr per span + cost ledger once per successful HTTP (not cache hit) — already NEXUS image adapter pattern to reuse.
- **Potential mitigation:** Circuit breaker per provider + exponential backoff + fallback weight (prefer Groq fast path for cheap tasks, Gemini for large context). Keep `NEXUS_IMAGE_GEN_PROVIDER` default `pollinations` non-polluting.

**Research verdict [VERIFIED, HIGH]:** LiteLLM routing + fallback is mature pattern; provider pricing volatility (see Stage 6 residual F5) remains out of system control (estimate ≠ invoice).

---

### Scenario G — Malicious Tool (Tool Poisoning / Supply Chain / Capsule Smuggling)

- **Expected bottleneck:** Not throughput but trust evaluation — every tool description/registry entry is attacker-controllable.
- **Failure mode:** Poisoned pack registers executable code, unknown op, or escalates permission; poisoned MCP server exfils args.
- **Architecture pressure:** Supply-chain wall — monolith does not help if pack pipeline trusts packages.
- **Required capability:**
  - Pure packs law enforced by `tests/architecture/test_pack_*_boundary.py` + data-only manifest gate at *any depth* (`test_pack_manifest_is_data_only.py`) + pending-capability refusal.
  - Bus permission ladder (A-D) before handler + allow-listed FFmpeg binary + no shell.
  - Registry moderation: MCP denylist + host allow-list + treat annotations untrusted.
- **Potential mitigation:** Keep CI gates blocking merge on pure-pack violation — they already do. For external packs: quarantine lane where new pack's outputs are never trusted by next step without human approval.

**Research verdict [VERIFIED, HIGH]:** Controls exist in NEXUS for creative subtree (T11 closed); same pattern must be applied to any future non-creative tool registry (LLM tools, RAG plugins) — gap noted as observation.

---

### Scenario H — Provider Outage (LLM Image/Embedding API down 4h)

- **Expected bottleneck:** Not NEXUS itself but upstream SLA.
- **Failure mode:** Gemini/Pollinations 5xx or deployed provider unreachable → `AllProvidersUnavailable`.
- **Architecture pressure:** Resilience wall — how gracefully offline path absorbs load.
- **Required capability:**
  - Ordered chain degrades to next provider → local fallback (Ollama/llama.cpp) if configured. Never fake results (see NEXUS guarantee "HTTP failures surface as errors, not fake images").
  - Bounded jittered retry (3 attempts) with `Retry-After` cap 8s (image lane pattern portable to LLM).
  - Cache hits (SHA256 key) continue serving without network.
- **Potential mitigation:** Tiered degraded experience: inform user of outage + offer to retry with local model; `nexus jobs resume` already reconciles truncated jobs after network heal.

**Research verdict [VERIFIED, HIGH]:** Retry + fail-closed pattern correctly avoids fake content; cost is user-visible delay, not data loss.

---

### Scenario I — Database Corruption (SQLite WAL corruption / Postgres disk loss)

- **Expected bottleneck:** Persistence layer (single source of truth for memory, jobs, checkpoint index).
- **Failure mode:** WAL checkpoint failure → locked DB; storage layer may report `database disk image is malformed`; job table corruption → dequeued jobs lost or duplicated.
- **Architecture pressure:** Durability wall — sidecar model concentrates durability on one file/DB.
- **Required capability:**
  - **Checksums & probe:** Not only image lane — apply `probe_video + sha256 + staging + atomic publish` philosophy to DB: WAL integrity check + `pragma integrity_check` in `nexus smoke` / healthz (without touching hot path).
  - **Recovery:** Alembic idempotent migrations (already) + `storage/checkpoint_lifecycle/reconciler` healing unknown states (observed) + `RUNTIME` pack coverage tool `storage/checkpoint_lifecycle*.py` + R2 backup tier (`docs/ops/r2-storage.md`).
  - **Sharp rule (NEXUS):** Messages never deleted by checkpoint cleanup; unknown state ⇒ no delete; golden updates human-only — these three prevent destructive recovery.
- **Potential mitigation:** Periodic `nexus checkpoints` audit + off-host backup to R2 (already `NEON_LIFECYCLE_RUNBOOK` path) + dual-store pattern (checkpoint index decoupled from main DB) reduces blast radius.

**Research verdict [PARTIALLY VERIFIED, MEDIUM]:** Recovery logic observed; actual corruption drill success measured via `STORAGE_RESILIENCE_CHECKUP_2026-09-21.md` — read-only note, not reproduced in this research.

---

### Scenario J — Schema Evolution (Tool/Pack/Registry breaking change)

- **Expected bottleneck:** Tool schema pinned in LLM prompt vs new required field → validation failures → hallucinated args.
- **Failure mode:** Agent loops on invalid tool calls or calls wrong adapter; mid-conversation upgrade causes in-flight failures.
- **Architecture pressure:** Compatibility wall.
- **Required capability:**
  - Pin JSON Schema 2020-12 + `outputSchema` (MCP 2025-11-25) + CI diff gate requiring major bump on breaking change.
  - Atomic deploy unit: model prompt (+ cache hint TTL) + registry + validator must bump together — MCP `ttlMs` + `listChanged` notification pattern makes this explicit.
  - Backward-compat: additive optional fields with defaults in minor.
- **Potential mitigation:** Keep `pack.manifest.json` + `creative/packs/verify.py` gate + `MCP-Protocol-Version` per-request negotiation (2026-07-28) enables clients to stay on older protocol during rollout.

**Research verdict [VERIFIED, HIGH]:** Breaking change requiring major version is industry orthodoxy (OpenAPI/AsyncAPI/MCP converge). NEXUS pack verification already encodes part of this.

---

### Scenario K — Long-Running Workflow (Minutes → Hours → Days, with Human Review)

- **Expected bottleneck:** Checkpoint durability + human interrupt wait + idempotency.
- **Failure mode:** Worker restart mid-render / mid-research → lost progress; human slow to approve → workflow times out or stale.
- **Architecture pressure:** Durability vs monolith constraint.
- **Required capability:**
  - **For ≤ minutes (current Nagar 30s cap, 5 images):** InProcess queue + SQLite checkpoint + `interrupt_before` on approval node is sufficient (observed).
  - **For hours/days (future):** Needs true durable resume beyond sidecar — this is exactly the Temporal threshold (Stage 7). Keeping monolith but extending it to hours is misfit; ADR must evaluate lifting constraint vs owning durable history + activity heartbeats.
- **Potential mitigation:** Stay at minute-scale with monolith; if product needs day-scale approvals/sagas, plan for Temporal (activities with heartbeats + Saga compensation) — don't try to stretch SQLite sidecar beyond its durability shape.

**Research verdict [VERIFIED, HIGH via Markaicode/SuhasBhairav]:** Recommends Temporal for hour+ multi-service; queue alternatives (Celery/Dramatiq) explicitly not suited.

---

### Scenario L — Multi-Agent Collaboration (Planner → Executors → Reviewer, or Agent↔Agent handoff)

- **Expected bottleneck:** Orchestration complexity, token amplification (message passing ~18% overhead CrewAI), state divergence, authorization delegation.
- **Failure mode:** Delegated identity abuse (ASI03 — agent B acts with higher privilege tool using agent A's context), cross-agent prompt injection propagation (ASI09), cascading failures (ASI08), opaque reasoning.
- **Architecture pressure:** Coordination wall — from explicit graph (good) to conversation-based (harder to govern).
- **Required capability:**
  - **Framework choice:** Graph (explicit state graph, e.g., LangGraph for delegation graph) is more governable than free conversation (AutoGen) for business workflows.
  - **Protocol:** Use A2A for inter-agent discovery/handoff (Google Apr 2026) + MCP for tooling — keeps fleet interoperable without single framework lock.
  - **Governance:** Per-hop authorization (no inherited creds), trace per delegation with `agent.supervisor → agent.worker` span parent/child, memory poisoning isolation per agent (ASI06), policy gate before each tool (Cordum: no framework has native policy gate — must add host layer).
  - **Memory:** Hierarchical memory (Mem0/Letta/Zep) shared intentionally (not accidentally) — isolated writes + provenance.
- **Potential mitigation:** Keep single-agent ReAct as baseline until multi-agent proves out (TheAIEngineer 2026: "majority of prod is ReAct, not because optimal but simple and good enough"). Introduce hierarchical supervisor only where decomposition is clean (researcher/writer/reviewer).

**Research verdict [PARTIALLY VERIFIED, MEDIUM]:** Fleet maturity reports (Agentic Infra Landscape 2025-26) rank swarm/collaborative debate as less predictable for enterprise — supervisor + sequential pipelines production-proven, swarm is research.

---

## Cross-Scenario Pressure Map

| Pressure wall | Scenarios touching | Monolith viable? | Fleet needed? | Core capability to keep in monolith even if fleet later |
|---------------|--------------------|------------------|---------------|--------------------------------------------------------|
| Auth/SSRF/Injection | B, F, G, H, L | Yes | No | Deny-by-default guard + SSRF + consent gate (B1-B7) |
| Queue/scale | B, C, D, K | To ~10k users / 1k tps | Past that → ADR | SQLite sidecar WAL + reconciler + atomic publish |
| Durability (hours/days) | K | Only minutes-scale | Hours+ → Temporal | Checkpoint index + reconciler pattern |
| Interop | F, L | Partial (LiteLLM) | Add A2A | MCP client for tools, A2A for agents (no vendor lock) |
| Supply chain | G, J | Yes (gates) | Yes (same gates) | Data-only manifest + pure-pack tests |
| Observability cost | D, K, L | Needs sampling | Needs OTel collector fleet | Structured events + cost ledger (once per success) |
| Staleness/recovery | I, J, K | Yes | Yes | Reconciler + no-delete-by-unknown + human-golden |

---

## Overall Landscape Verdict (Stress-Test Level)

- **Monolith remains *optimal* at NEXUS's designed scale (10–1k users, 30s renders, <1M calls/day with sampling). Replacing with Temporal/Kafka would add operational surface without throughput justification — confirmed across Stages 7–9. [VERIFIED, HIGH]**
- **Constitutional break point is Scenario C/K:** >10k active users with durably scheduled human-in-loop sagas (hours/days) forces a deliberate *architecture event* (ADR). This is not a bug to patch — it is a constraint working as intended (OVERVIEW §4). [VERIFIED]
- **Most mitigations needed *without* breaking monolith are already present in read-only observed codebase (SIDEAR SHA256 cache, atomic publish, rate limiters, LiteLLM chain, reconciler, structlog, consent gates). Gaps are process/gov (ONE-OWNER wiring test task-124, OTel export, Postgres SKIP LOCKED upgrade path) not greenfield features.**

---

## Sources Cited

1. **Primary (NEXUS READ-ONLY):** `docs/architecture/OVERVIEW.md` §4/§7/§8 + `SECURITY.md` (B1-B7+T1-T13) + `docs/architecture/CREATIVE_STUDIO.md` (5 images/30s 1280×720, FFmpeg probe) + `docs/DECISION_LOG.md` wave 3 retries + `tests/architecture/*` 15 gates + `adapters/in_process_job_queue.py` + `storage/checkpoint_lifecycle*.py` + `creative/rendering/executor.py` + `infrastructure/observability` + `pyproject.toml` (`asyncpg`, `psycopg[pool]`, `litellm`)
2. **Primary docs:** Temporal, Celery, Kafka, Postgres SKIP LOCKED (Stage 7) — for durability semantics per scenario
3. **Stage outputs:** 01 Technology Landscape (T3/T4/T5 durability trade), 02 Architectures (A1-A5), 03 Capability Registry (stateless MCP), 05 Memory (hierarchical), 06 Local-First (fallback chain, pack coverage), 07 Jobs (exactly-once layers), 08 Observability (sampling, KPIs)
4. **Independent:** agentic infra landscape 2025-26 (fleet maturity ranks) + Markaicode/SuhasBhairav (Temporal threshold for long workflows) + OpenObserve payload cost (sampling pressure for scenario D)

