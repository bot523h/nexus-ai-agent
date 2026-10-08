# Master Architectural Assessment — NEXUS AI Agent

**Assessment date:** 2026-09-27 UTC
**Authoritative checkout:** `main` and `origin/main` at `f53923d47f4b6b362181f0c0f420d8c6797ab093`
**Session branch:** `arena/01a0e2c3-nexus-ai-agent` (the branch is intentionally unchanged except for this assessment set)
**Assessment status:** complete for Phase Zero; implementation is not included
**Decision:** NEXUS is **not production-ready as a durable, multi-worker agent service**. It is a strong, locally proven modular monolith with a particularly mature Nagar creative path, but its assistant runtime has split composition, weak plan authority, incomplete memory trust semantics, a bounded in-process LLM queue, and deployment/recovery evidence gaps.

This record is a dated audit, not a replacement for the living architecture pages. It records what was actually found at the exact SHA above, the research comparison that led to the target architecture, and the gates required before an implementation claim can be made.

---

## 1. Authority, method, and evidence boundary

### 1.1 Truth order

The assessment used this order whenever sources disagreed:

1. The exact checked-out source and tests at `f53923d47f4b6b362181f0c0f420d8c6797ab093`.
2. Git history, refs, and the live GitHub repository, pull requests, issues, releases, and Actions runs.
3. Executable tests and workflow definitions.
4. Living documentation, audits, roadmap, Continuum, and board records, labelled with their verified SHA.
5. Earlier reports, prose claims, and branch intent only as leads to investigate.

A passing CI run proves the jobs that ran on that SHA. It does not prove production traffic, a successful real backup, a live restore, a multi-worker deployment, or a candidate pull request that has not merged.

### 1.2 Excavation performed

The repository and live surface were inspected across:

- root metadata, `README.md`, `CHANGELOG.md`, `VERSION`, `ROADMAP_STATUS.md`, packaging, Docker, Compose, Koyeb, environment examples, Make targets, scripts, and all workflows;
- `src/` composition roots, bot/API surfaces, orchestration, agents, LLM providers, memory, feature engines, storage, jobs, tools, Nagar packs/studio/rendering/slideshow, observability, maintenance, and integrations;
- `tests/architecture`, unit, integration, fixtures, migrations, and mutation harnesses;
- `.nexus/continuum.json`, `.agents/board.json`, ADRs, decision records, dated audits, and operational runbooks;
- live GitHub PRs, issues, branches, tags, releases, commit history, exact-SHA Actions runs, candidate PR checks, and branch/ruleset access.

Commands used included `git rev-parse`, `git status`, `git log`, `git tag`, source/test inventory and AST test counting, `gh pr list`, `gh pr checks`, `gh run view`, `gh issue view`, repository metadata, branch/ruleset queries, and targeted source/call-site searches. No source code was changed before this plan and assessment were written.

### 1.3 Baseline facts at the anchor

| Fact | Observed truth | Consequence |
|---|---|---|
| Checkout | `HEAD` is `f53923d47f4b6b362181f0c0f420d8c6797ab093`, a merge of PR #103; working tree was clean at excavation start | All claims below are SHA-bound. |
| Product version | `VERSION` and `pyproject.toml` say `3.13.0`; the version-lockstep script passes | The README’s leading `v3.12.0` statement is stale, not authoritative. |
| Source inventory | 243 Python files under `src/nexus_ai_agent`; 46,278 source lines by the simple `cat` inventory used here | The repository is materially larger than the older architecture counts. |
| Test inventory | 173 `test_*.py` files; AST count is 1,855 test functions | `.nexus/continuum.json` expects 649 and is not current. |
| Local environment | Python 3.11.2; Alembic, SQLAlchemy, SQLModel, pytest, Ruff, and mypy are not installed in this sandbox | No local quality result is claimed. |
| Main CI | Actions run `36316279654` on the exact SHA passed: lint, format, mypy, non-slow tests, Python 3.10/3.11/3.12 parity, extras matrix, real PostgreSQL migration, release lineage, and trust mutations | Main has current CI evidence for those gates, not for production operation. |
| Backup CI | Maintenance run `36308867958` on `6624a13` failed in `backup-db`; the current open issue is #85 and reports missing R2 and database configuration | There is no observed green scheduled production backup. |
| Open work | Live GitHub reported one open issue (#85) and 31 open PRs. PR #105 (`80013e3225c3fd713c86e9d4800cde6032e8f2e7`) has passing shell/docs mutation and full matrix checks, but is not merged. PR #102 (`eb6551bbae572781afe852e8d623a0b7a7cdba4e`) is dirty; its test and Python-parity jobs failed while its candidate Continuum jobs passed. | Candidate evidence must not be promoted to main truth. |
| Releases/tags | The local tag list has no `v3.13.0`; the latest local version tag is `v3.5.0`. Live GitHub releases returned through `v3.3.0`; the current version has no observed tag/release. | Release-lineage reports the gap; do not call the version released until the chain exists. |
| Governance | The repository is public, default branch `main`, 76 remote branches were listed; ruleset query returned `[]`, while branch-protection inspection returned GitHub 403 rather than a definitive protection record | Merge governance needs an explicit, authorized verification step; absence cannot be inferred from the 403. |
| Board | `.agents/board.json` says `updated_at=2026-09-27T12:10:00Z`, has active/in-review unrelated work, and has no claim for this session | The plan must be claimed through the board before implementation, without colliding with existing zones. |

---

## 2. Executive assessment

### 2.1 What is genuinely strong

1. **Nagar’s capability and evidence plane is the strongest architectural seam.** Typed commands, a capability registry, permission levels, reference resolution, an atomic `CommandBus`, data-only manifests, a trust root, deterministic RenderIR compilation, one FFmpeg process, staging/atomic publication, measurement, artifact verification, fenced job transitions, and typed failure notifications are present in the merged tree.
2. **The pack trust root is real and fail-closed.** The canonical record is ADR 0006 and `creative/packs/trust_root.json`; the current default root has no external publisher authority, so an external pack cannot activate merely by claiming its own signature. Trust mutation evidence is on main CI run `36316279654`.
3. **The existing job lifecycle has useful primitives.** `InProcessJobQueue` persists SQLite rows, supports idempotency keys, six statuses, compare-and-set transitions, fencing attempts, verification, publication recovery, typed failure classification, and fail-safe completion hooks. These are valuable components, not disposable code.
4. **Boundary hardening is substantial.** The Telegram access guard is registered before application handlers and stops denied updates; webhook and HMAC routes fail closed; the SSRF transport re-checks connections and redirects; the workspace filesystem uses descriptor-relative, no-follow operations; the creative render lane does not use a shell; and the pack boundary is structurally guarded.
5. **The repository has unusually good evidence infrastructure.** Architecture fitness functions, extras/parity checks, release-lineage checks, real PostgreSQL migration CI, pack mutation testing, artifact verification tests, and dated audit records make regression visible when the relevant gate actually runs.

### 2.2 What prevents a production claim

The main blockers are architectural rather than cosmetic:

- The graph’s provider is not the provider used by every user-facing AI path. The bot constructs a shared request queue and Gemini engine, then `bot/handlers.py` constructs another Gemini engine without the shared queue or conversation store. Store agents, knowledge, AI-memory, and legacy feature paths can construct additional `GeminiProvider` engines.
- The live graph is not a general multi-agent planner. It uses keyword intent classification, persona substring matching, a private one-step planner, substring tool selection, and a one-step executor. The public `PlannerAgent` and `ExecutorAgent` classes are not the graph’s execution path.
- There are two materially different “memory” systems. The consent-gated `AIMemoryEngine` stores structured user fields in the application database, while `LongTermMemory` automatically stores every graph turn in a separate SQLite vector file. The latter ignores metadata, performs synchronous SQLite I/O inside async methods, falls back to recency retrieval, and injects retrieved text into the prompt without provenance or an instruction/data trust boundary.
- The Gemini request queue is an in-process priority/timestamp queue, not fair round-robin or a durable multi-worker queue. A timed-out caller leaves its request executable; the pending counter is decremented in both the timeout handler and `finally`; several direct calls bypass the queue; and one-shot methods do not consistently record limiter usage.
- The durable job queue is a single-process SQLite adapter. It is a good local adapter but not a multi-worker production scheduler. Webhook delivery returns HTTP 200 after enqueueing an update into the process queue, so a process loss after acceptance can lose the update before the durable job path sees it.
- SQLite path selection is not canonical. `Settings.db_path` is used by some composition roots, but `storage.db.get_session(db_path=None)` falls back to the literal `data/app.sqlite`. Several feature engines call it without passing the configured path.
- Koyeb configuration does not itself configure external PostgreSQL or R2. The runbook openly says local state is lost on scale-to-zero and accepted webhook updates can be lost during shutdown. The only observed maintenance run failed for missing secrets/database configuration.
- The shell controls are defense-in-depth, not a sandbox for adversarial arbitrary code. The current argument grammar permits unexamined flag forms and does not provide OS isolation or a trusted-handoff model.

**Readiness verdict:** local development and the merged Nagar creative vertical slice are credible; a production claim for the whole agent service is blocked until the P0 waves in the execution plan have exact-SHA evidence.

---

## 3. Actual architecture at the anchor SHA

### 3.1 Composition roots and request paths

#### CLI bot path

`cli.py::run_bot` currently does the following:

1. Loads `Settings`, configures logging, creates the configured DB parent, and calls startup schema preparation.
2. Calls `build_llm_provider(settings)`. With routing enabled and a configured provider, this is a LiteLLM chain wrapped by `FallbackProvider` and `FakeLLM`; otherwise it selects local server, local GGUF, or FakeLLM.
3. Builds a `ToolRegistry` and registers file tools and, optionally, the guarded shell tool.
4. Creates `LongTermMemory(settings.vector_path, llm)` and a LangGraph checkpointer.
5. Compiles the graph with that provider, checkpointer, memory object, and registry.
6. Passes the compiled graph to `bot.app.build_application`.

The important architectural fact is that the graph provider is composed here, while the Telegram application composes a separate legacy Gemini subsystem. There is no single application-wide LLM gateway object connecting both paths.

#### Telegram application path

`bot/app.py` creates:

- an `InProcessJobQueue` on `<settings.db_path>.jobs.sqlite3`, registers `worker.default_job_handlers`, and installs a completion notifier;
- a `ConversationStore` on `settings.db_path`;
- a `GeminiRequestQueue`;
- a `GeminiEngine` using that queue and conversation store;
- image, speech, summarizer, referral, cloud, and shared `FeatureEngines` objects.

It stores those objects in `application.bot_data`, installs the global access guard at handler group `-1`, and calls `build_handlers`.

`bot/handlers.py::build_handlers` then constructs a new `GeminiEngine` with only the API key and model, plus a new `SummarizerEngine`. It does not consume the application-owned `gemini_engine`, `request_queue`, or `conversation_store` for the direct `/ai`, `/ask`, `/code`, `/translate`, `/summarize`, and related paths. This is the concrete P0-8 double-wiring defect; the `FeatureEngines` container fixed only part of the feature-engine duplication.

#### Graph path

`orchestration/graph.py::compile_graph` registers these live nodes:

`router → (task memory reader → planner → executor → moderation, or chat/memory reader → persona) → memory writer`.

The router is deterministic. `classify_intent` searches normalized text for ordered keyword substrings. Task messages go through the task memory reader; explicit memory messages go through the chat memory reader; ordinary chat goes directly to persona routing and does not read long-term memory. `select_persona` also uses ordered substring sets.

The graph’s private `_planner_agent` creates exactly one pending step. If a registry exists, it selects the first tool whose name is a substring of the user message. It does not produce a typed capability ID, dependency edge, input schema, precondition, postcondition, approval requirement, evidence claim, or idempotency key. `_executor_agent` runs at most the first pending step and records a result. The registry is called with an empty policy, so guarded tools return a confirmation request; there is no durable approval state, proof of user identity at approval time, or controlled resume protocol in this graph.

`agents/planner_agent.py` and `agents/executor_agent.py` exist, but `compile_graph` uses private functions rather than those classes. They are therefore available code, not the canonical runtime.

#### Creative/Nagar path

The strongest vertical slice is:

`Telegram creative surface → JobQueuePort → InProcessJobQueue → worker handler → capability-pack registry → CommandBus → RenderIR compiler → one FFmpeg invocation → staging/atomic publication → independent verification → completion notifier`.

The job adapter carries typed failures. The queue has `pending`, `processing`, `verifying`, `completed`, `failed_retryable`, and `failed_terminal` states; execution attempts fence stale workers; artifact claims are independently re-measured; PDF publication retains and restores a previous artifact on refusal. This path should be preserved and used as the pattern for future effectful work rather than replaced by generic UI automation.

#### API and webhook path

`api/app.py` serves `/healthz`, the Telegram webhook, a dashboard router, and deprecated HMAC-protected legacy creative endpoints. The webhook checks `X-Telegram-Bot-Api-Secret-Token`, parses a Telegram update, places it on PTB’s in-process update queue, and answers 200. The route deliberately does not wait for durable job acceptance beyond PTB queue insertion.

The dashboard bearer token is optional. When unset, the code and runbook require the deployment to keep the port private; Compose binds it to `127.0.0.1` by default. This is a deployment precondition, not an authentication guarantee at a public edge.

### 3.2 Actual data-flow map

| Data or control | Current owner/path | Durability and trust meaning |
|---|---|---|
| Telegram update | PTB polling or webhook update queue | In-memory until handler execution; webhook 200 does not mean durable business acceptance. |
| Graph thread state | LangGraph checkpointer, SQLite or PostgreSQL | Workflow checkpoint, not a user-history source of truth. Lifecycle metadata is separate. |
| User-visible application records | SQLModel/Alembic application DB | Intended durable source, but no single configured SQLite path is enforced by every caller. |
| Direct Gemini transcript | `ConversationStore` when the app-owned engine is actually used | Sync SQLAlchemy SQLite store; current handler-local engine does not use the app-owned instance. |
| Graph long-term memory | `LongTermMemory` at `vector_path` | Separate SQLite, synchronous in async methods, no provenance/retention/deletion policy, automatic write of graph turns. |
| Consent-gated user memory | `AIMemoryEngine` and `UserMemory` | Consent state is persisted; extraction is gated, but storage/retrieval still lacks a provenance-aware fact model. |
| RAG/document corpus | Chroma and related feature storage | Derived corpus; not a trusted instruction source, but current graph memory does not consistently carry source identity. |
| Background job state | `<db>.jobs.sqlite3` owned by `InProcessJobQueue` | Durable only where the sidecar itself is on durable storage and there is one safe owner. |
| Creative temporary artifacts | `creative_temp_dir` and job workspaces | Ephemeral, cleaned by workers/notifiers; artifact verification is stronger than the general assistant path. |
| Backups and large blobs | R2 adapters and maintenance CLI | Intended durable external tier; no observed successful scheduled backup at this anchor. |

---

## 4. Nagar, Continuum, governance, and evidence truth

### 4.1 Nagar status

**Canonical and proven on main:**

- `creative/studio/` contains the typed project, capability registry, permissions, references, and command bus.
- `creative/packs/` manifests are data-only and checked for operation coherence.
- `creative/packs/trust.py` is governed by ADR 0006, not the nonexistent ADR 0009 named in that module’s prose.
- `creative/rendering/` compiles typed operations into deterministic FFmpeg arguments and has one process-spawn site without `shell=True`.
- `jobs/verification.py` and the Gate 5 job lifecycle enforce independent artifact measurement and typed failure states.
- Main CI run `36316279654` passed the trust mutation harness and the real PostgreSQL migration job; the creative behavior is also included in the non-slow test job.

**Not a current claim:** PR #105’s shell argument-grammar mutation evidence is green on the candidate head but is not merged into `main`. PR #102’s Continuum evidence job is also candidate evidence, and its test/parity jobs failed.

### 4.2 Continuum truth

`.nexus/continuum.json` is structurally readable and its recorded step `52329e6022a0bdd9f3f9e287da581b752204c811` is an ancestor of the current head. Its content is nevertheless stale:

- it says the next action is review of a v3.12.0 metadata PR even though the repository version is 3.13.0 and later trust work is merged;
- it expects 649 test functions while the current AST inventory finds 1,855;
- it fingerprints Python 3.11.2 correctly for this sandbox but requires Alembic and SQLAlchemy packages that are absent locally;
- it is a release/project-state snapshot, not proof that every current source claim is complete.

The snapshot must be refreshed only at a controlled release/state cut after its expected test count and environment fingerprint are measured in a real gate environment. It must not be rewritten merely to make this assessment pass.

### 4.3 Governance truth

`.agents/board.json` records a claim-before-code protocol, exclusive path zones, a single gates owner rule, acceptance criteria, and a 24-hour lease. It is useful governance infrastructure. It also contains inherited active or in-review records and old branch dispositions, so the live GitHub state must be reconciled before any future claim is made. This session had no active claim at excavation time; implementation must claim a non-overlapping zone before editing.

The open PR list is not a roadmap. A PR’s green candidate check does not make its code canonical until merged into the exact main lineage. The repository’s current ruleset query returned no rulesets, but GitHub denied the branch-protection query; branch protection therefore remains an operator-verification item rather than an inferred fact.

---

## 5. Security and trust-boundary assessment

### 5.1 Boundaries that should be preserved

| Boundary | Current control | Assessment |
|---|---|---|
| Telegram user to handler | Group `-1` `AccessGuardHandler`, synchronous PTB check, `ApplicationHandlerStop`, deny-by-default allow-list, denial rate limiter | Strong merged control; retain integration test against the real PTB dispatch loop. |
| Webhook sender to API | Secret-token constant-time comparison, 403 for mismatch, 503 before application readiness | Strong authentication gate, but durable ingress semantics remain incomplete. |
| Legacy mutating HTTP endpoint | HMAC key required, timestamp freshness, body-bound signature, upload cap | Fail-closed control; legacy route should remain segregated and eventually removed only with a recorded migration. |
| Remote URL to fetcher | URL preflight plus connection-time DNS/address checks, redirect revalidation and HTTPS enforcement | Strong SSRF defense; keep negative tests and do not replace with a preflight-only check. |
| Workspace path to filesystem | lexical validation, physical containment, directory FDs, `O_NOFOLLOW`, no-follow traversal | Strong file-operation boundary on POSIX; shell argument validation still needs its own complete grammar. |
| Nagar command to effect | typed command, capability registry, permission level, reference resolution, atomic bus, artifact verification | Strongest effect authority in the repository; future AI actions should enter through this style of seam. |
| Pack manifest to activation | data-only schema, external trust root, Ed25519 verification, empty default publisher authority, fail-closed activation | Proven trust root; canonical source is ADR 0006. |
| Render process to host | allow-listed FFmpeg path, typed argv, one process site, no shell, timeout, staging and probe | Appropriate for the known FFmpeg lane, not a general arbitrary-code sandbox. |

### 5.2 Open or conditional boundaries

1. **Prompt and memory boundary:** `LongTermMemory` content is placed in a system prompt as “Context from memory,” and active store-agent memory is concatenated into a system prompt. There is no durable source/actor/confidence/expiry record and no explicit rule that retrieved memory is untrusted data rather than instructions. An attacker who can cause a memory write can influence later tool selection or model behavior.
2. **LLM provider boundary:** normal graph requests can reach configured cloud providers by routing policy. The AI-memory consent gate protects one extraction feature, not every normal AI request. `llm_strict_privacy` removes only certain OpenRouter free deployments; it is not a universal no-egress switch.
3. **Tool approval boundary:** the registry knows `SAFE`, `GUARDED`, and `BLOCKED`, but the graph has no typed approval token, actor binding, expiry, replay protection, or durable resume state. The current behavior is safer because guarded tools refuse without `confirmed=True`, but it is not a complete approval protocol.
4. **Shell boundary:** `ShellTool` uses `shlex.split`, command names, path checks, and a timeout. Its parser does not prove the complete grammar for every allow-listed utility. For example, follow-symlink flags and attached/long option forms need explicit handling. A command allow-list is not host isolation.
5. **Trusted handoff boundary:** an agent can write files into a workspace that a later trusted process may interpret. Provenance and approval are not carried across that handoff. This is a distinct risk even when the originating process remains inside its workspace.
6. **Webhook-to-job boundary:** a verified HTTP 200 can precede durable business acceptance. A process crash after enqueueing to PTB but before the handler creates a durable job can lose the message.

### 5.3 Security conclusion

The correct posture is not to weaken the proven controls or to claim that they solve every agent risk. Keep the existing trust root, access guard, SSRF, descriptor-relative file operations, and evidence chain. Add complete mediation, typed authority, provenance, replay resistance, and an isolation boundary only where the capability is genuinely untrusted.

---

## 6. Agent runtime, memory, planner, router, and provider findings

### 6.1 Router and graph

| Concern | Current behavior | Failure class |
|---|---|---|
| Intent | Ordered keyword substring matching in `classify_intent` | Misclassification; lexical collisions; no confidence or unsupported route. |
| Persona | Ordered substring matching in `select_persona` | Non-deterministic from the user’s perspective when terms overlap; no policy distinction. |
| Chat memory read | Ordinary chat bypasses `LongTermMemory`; only explicit memory intent and task path read it | Capability semantics do not match “persistent context” expectations. |
| Task plan | Exactly one step with raw action text | No decomposition, dependencies, parallelism, typed inputs, or plan identity. |
| Tool selection | First registry tool name appearing as a substring of user text | Confused deputy and accidental capability selection. |
| Inputs | Empty/default inputs unless another layer fills them | A selected capability cannot be safely parameterized by a schema. |
| Preconditions/postconditions | None in the graph | Tool success is not independently established. |
| Approval | `ToolRegistry` can return `needs_confirmation`, but graph has no approval object or resume protocol | Human approval cannot be bound to a plan step and replay-protected. |
| Repair/replan | None | A failed step becomes a response, not a controlled repair or refusal plan. |
| Evidence | Tool result text is recorded; no structured claim/evidence chain | Output can be reported without a durable proof contract. |

### 6.2 Memory

There are three concepts that must be separated in the target design:

1. **Thread checkpoint state:** resumable LangGraph execution state, disposable and scoped to a thread.
2. **User-approved long-term facts:** structured facts with consent, provenance, confidence, policy, TTL, deletion, and audit metadata.
3. **Retrieval corpus:** documents and derived chunks with source identity and trust classification.

The current graph conflates long-term turn storage with retrieval. Its SQLite schema is only `(id, thread_id, content, embedding)`. `metadata` is accepted and discarded. Reads and writes use synchronous `sqlite3` calls inside `async def` methods. If sqlite-vec is unavailable, search returns recent rows, not semantically relevant rows. If vector search errors, it also returns recent rows. `format_context` turns strings directly into prompt context.

The separate `AIMemoryEngine` has a real consent gate and a user deletion operation, which must be preserved. It is not a substitute for provenance on graph memory: it extracts user fields through a separate `GeminiProvider`, uses the default session path, and has no source/actor/expiry model for each fact.

### 6.3 Provider composition

The target must distinguish:

- a **routing policy** that decides whether a request may leave the process and which provider class is eligible;
- a **provider client** that performs one model operation;
- a **request gateway** that owns queueing, budgets, cancellation, correlation, redaction, and usage accounting;
- a **local-only fake** used explicitly in tests or a deliberate degraded mode.

The current `LiteLLMRoutingProvider` is a useful graph adapter, but it does not own the direct `GeminiEngine` paths. `GeminiProvider` wraps `GeminiEngine` again, and `AIMemoryEngine`, knowledge managers, and store agents can instantiate it independently. Provider failure classification and privacy policy therefore cannot be proven globally by one seam.

---

## 7. Persistence, jobs, queue, and deployment assessment

### 7.1 Application persistence

The application has a credible Alembic/PostgreSQL direction and a passing real-PostgreSQL CI job. The path problem is composition, not migration syntax: `Settings.db_path` is configured in the application and some stores, while `get_session()` without an argument chooses literal `data/app.sqlite`. A deployment that sets `NEXUS_DB_PATH` can therefore write related records to different SQLite files.

The repository also has synchronous SQLAlchemy stores (`ConversationStore`, referral and other legacy engines) alongside async SQLModel sessions. Several call sites offload known sync operations with `asyncio.to_thread`, but the architecture has no single database port enforcing this for every store. This creates event-loop and transaction-boundary risk.

### 7.2 Job lifecycle

The `InProcessJobQueue` is not empty scaffolding. It already has:

- unique idempotency keys and first-payload-wins behavior;
- atomic pending claim and attempt fencing;
- explicit processing/verifying/completed/failure states;
- recovery of unfinished rows through an explicit startup/operator path;
- artifact verifier and publication contracts;
- typed retryable versus terminal failure classification;
- notifier failure isolation.

Its limitations are the production boundary:

- SQLite sidecar and in-process tasks assume one owning process; there is no database-backed competing-consumer claim for multiple workers;
- no independent lease heartbeat or automatic expired-lease recovery exists beyond explicit reset/recovery semantics;
- retryable is classified but there is no retry scheduler;
- worker cancellation and external side effects still need an end-to-end kill/restart/idempotency proof for every handler;
- webhook ingestion is not coupled to this durable queue before HTTP acceptance.

The least-complex correction is to retain this adapter for local/single-process mode and add a PostgreSQL-backed job repository with atomic claims, leases, idempotency, cancellation, retry classification, and metrics for multi-worker production. Do not introduce a broker or a workflow engine until requirements demonstrate long-running timers, human approval pauses, replay, or cross-service workflow ownership.

### 7.3 Gemini request queue

`GeminiRequestQueue` currently orders requests by `(priority, monotonic timestamp)`. It does not rotate among users within a priority tier, despite its description. A timeout cancels the waiting future but does not retract the queued request, so the provider call can still happen after the caller has given up. The timeout branch and `finally` both decrement `_pending_per_user`. The queue’s retry detection is string-based and there is no request-wide idempotency/cost budget.

Even a corrected queue would not be global until all provider paths use it. `bot/app.py` creates the shared queue, but the live handlers construct a Gemini engine without it. `translate`, `summarize`, `code`, and `vision` call the engine directly; one-shot limiter accounting is incomplete.

### 7.4 Deployment and recovery

The deployment files correctly expose a DB-free `/healthz`, a webhook mode, a webhook secret, and a local Compose volume. They do not prove a durable production topology:

- `koyeb.yaml` sets bot/webhook variables but does not require `NEXUS_DATABASE_URL`, R2 configuration, or an external durable queue;
- Koyeb local volumes are not a multi-host backup strategy;
- the runbook states accepted but unprocessed updates can be lost during shutdown;
- the scheduled backup workflow’s observed `backup-db` job failed because required R2/database secrets were not configured;
- no live restore drill, rollback drill with a migrated schema, or exact production readiness probe evidence was found in the current main history.

Production configuration must fail closed when durable state is absent, or explicitly advertise single-process ephemeral mode instead of presenting the same deployment as durable.

---

## 8. Current, target, canonical, superseded, and local-only claims

| Area | Canonical current claim | Target claim | Status label |
|---|---|---|---|
| Main identity | Exact SHA `f53923d...` and its merged source | Every assessment and release record names the exact SHA | **Canonical / proven** |
| Nagar trust root | ADR 0006, empty default external authority, Ed25519 verification, activation gate | Keep the trust root and bind every external authority to it | **Canonical / proven on main** |
| Creative artifacts | Job lifecycle plus independent verification and typed failures | Extend the same evidence contract to every effectful operation | **Canonical / proven for registered lanes** |
| Graph orchestration | LangGraph graph with keyword router, persona route, one-step task path | Typed plan/capability/execution/evidence pipeline | **Canonical current / target not implemented** |
| Planner classes | `agents/planner_agent.py` and `executor_agent.py` exist | They become adapters only if they implement the canonical plan contract | **Available but superseded by private graph helpers as live path** |
| LLM routing | LiteLLM chain is the CLI graph provider | One gateway covers graph, handlers, store, memory, and knowledge | **Canonical only for graph composition; gap elsewhere** |
| Memory consent | `AIMemoryEngine` has a consent gate for its extraction path | All persistent memory has provenance, admission, trust labels, TTL, deletion, audit | **Consent control proven; complete memory model open** |
| Long-term graph memory | Separate SQLite with synchronous async methods and fallback recency retrieval | Checkpoint/facts/corpus separation and structured evidence retrieval | **Canonical current; P0 open** |
| Jobs | SQLite in-process queue with fencing and verification | DB-backed atomic claims/leases/idempotency for multi-worker mode | **Canonical local adapter; production target open** |
| Database | Alembic and PG path are exercised in CI | One settings-aware composition root for every store | **Migration path proven; path ownership open** |
| Shell | Disabled by default, command/path allow-list, subprocess without shell | Complete grammar plus isolation for arbitrary untrusted code | **Defense-in-depth only; PR #105 is candidate evidence** |
| Deployment | Polling or webhook container, DB-free health endpoint | External durable DB/queue/blobs, ingress durability, restore and rollback proof | **Documented topology; production evidence absent** |
| CI | Main run `36316279654` green for declared current jobs | Add missing architecture, shell, Continuum, deployment, recovery, and mutation rails to main | **Current CI proven; target gates partly candidate-only** |
| Continuum | Snapshot at schema v2 with old next step and count 649 | Release-cut snapshot matching source/test/environment facts | **Readable but stale** |
| Architecture docs | Many pages claim verification at `7573249` | Rebind living docs to a current exact SHA in a dedicated docs update | **Superseded evidence, not runtime authority** |
| Version prose | `VERSION`/`pyproject` 3.13.0; README header says 3.12.0 | One lockstep release statement in all user-facing docs | **README claim stale** |
| Production backup | Maintenance exists; current observed backup run failed and issue #85 is open | Successful real backup, immutable retention where needed, restore drill | **Not proven / blocker** |

---

## 9. Gap and dependency map

### 9.1 Gap register

| ID | Gap | Root architectural cause | Depends on | Priority |
|---|---|---|---|---|
| G-01 | Multiple LLM engines and queues | No canonical runtime dependency container or request gateway | None; prerequisite for most work | P0 |
| G-02 | Configured SQLite path can be ignored | `get_session(None)` owns a literal default rather than receiving the composition-root path | G-01 | P0 |
| G-03 | Queue timeout can execute abandoned work and counts pending twice | Caller future lifecycle is not linked to queue-item cancellation; cleanup is duplicated | G-01 | P0 |
| G-04 | Direct model methods bypass shared queue/usage accounting | Legacy feature APIs predate the routing composition | G-01 | P0 |
| G-05 | Long-term graph memory has no provenance/trust/retention/delete contract | One table stores raw turn strings and embeddings; metadata is discarded | G-01, G-02 | P0 |
| G-06 | Retrieved memory can influence instructions without trust labeling | Prompt construction treats retrieval output as trusted context | G-05 | P0 |
| G-07 | One-step substring planner | State schema and graph were designed for an MVP response, not typed execution | G-01; preserved Nagar command contract | P0 |
| G-08 | Approval cannot be durably bound to a step | Registry confirmation is a boolean policy flag, not an authority token | G-07 | P0 |
| G-09 | SQLite queue is not a production competing-consumer queue | Sidecar owns local DDL and process-local scheduling | G-02; production PostgreSQL | P0 for multi-worker mode |
| G-10 | Webhook can acknowledge before durable business acceptance | PTB update queue is the first handoff | G-09 or an inbox/outbox seam | P0 for scale-to-zero |
| G-11 | Backup and restore are not evidenced on a real configured environment | Owner-side secrets and a real recovery drill are absent | G-02, G-09, external DB/R2 | P0 for production claim |
| G-12 | Shell allow-list grammar incomplete | Ad-hoc per-command scanning is not a complete parser and has no OS isolation | G-07 capability model; deployment choice | P1, P0 before arbitrary-code exposure |
| G-13 | Release/architecture docs drift | Multiple agents update living claims without a current evidence refresh | Governance and CI | P1 |
| G-14 | Current CI does not include candidate shell mutation, Continuum evidence, or deploy/restore proof | Important rails exist on open branches or in scripts but are not all main jobs | G-13 and owner decisions | P1 |
| G-15 | No global retry/cost/idempotency budget | Queue, LiteLLM, image adapters, and workflow retries have independent policies | G-01, G-09 | P1 |

### 9.2 Dependency graph

```text
Evidence baseline and ownership (this assessment + plan)
        |
        v
Canonical runtime container + one configured database path (G-01, G-02)
        |
        +--------------------+
        |                    |
        v                    v
LLM gateway/queue       Provenance-aware memory (G-03..G-06)
        |                    |
        +---------+----------+
                  v
Typed plan/capability/approval/evidence execution (G-07, G-08)
                  |
                  v
Durable job claims, leases, webhook inbox, replay/idempotency (G-09, G-10, G-15)
                  |
                  v
External state, backup/restore, rollback, deployment readiness (G-11)
                  |
                  v
Complete shell grammar and isolated untrusted execution (G-12)
                  |
                  v
Main CI/document/governance closure and production readiness decision (G-13, G-14)
```

The graph is deliberately conservative. Nagar’s existing command bus and artifact verification can be reused at every step; they are not blocked on a generic workflow engine.

---

## 10. Five-angle research comparison and architectural decisions

The research was performed separately for the queue/side effects, memory, persistence/recovery, shell isolation, and planner/runtime concerns. Each problem was evaluated from five angles: official source, mature production implementation, security/failure modes, alternatives/trade-offs, and recent research. The decisions below are the least-complex complete corrections, not a request to import every researched system.

### 10.1 Durable queues, side effects, and retries

| Angle | Evidence and finding | Design consequence |
|---|---|---|
| Official | SQLite transactions and WAL document one concurrent writer and same-host WAL behavior: `https://www.sqlite.org/lang_transaction.html`, `https://www.sqlite.org/wal.html`. PostgreSQL `SELECT ... FOR UPDATE SKIP LOCKED` documents competing claims and its intentionally inconsistent queue view: `https://www.postgresql.org/docs/current/sql-select.html`. | SQLite is suitable as a bounded local/single-writer adapter; PostgreSQL is the production claim path for competing workers. |
| Mature production | Temporal activity guidance (`https://docs.temporal.io/activity-definition`, `https://docs.temporal.io/best-practices/pre-production-testing`) treats activities as at-least-once and requires idempotency, bounded retries, restart/kill testing, and explicit failure handling. Stripe’s idempotency contract (`https://docs.stripe.com/api/idempotent_requests`) stores keyed outcomes and rejects parameter mismatch. | Add idempotency keys, payload fingerprints, retry classes, kill/restart tests, and effect records without introducing Temporal immediately. |
| Security/failure | OWASP’s agent security guidance covers excessive agency, memory poisoning, tool abuse, and approval bypass. Retry amplification can become a self-inflicted outage or “denial of wallet.” | Couple retry budgets to one job/request identity; never let a caller timeout silently authorize a later side effect. |
| Alternatives/trade-offs | Keep SQLite with a single process; use a PostgreSQL job table with leases; adopt a workflow engine such as Temporal. SQLite has the least operational cost but cannot honestly serve multiple workers; Temporal adds timers/replay/approval value only when those requirements exist. | Retain `InProcessJobQueue` locally; add a PostgreSQL repository and leases for production; defer a workflow engine. |
| Recent research | `RetryGuard` reports retry storms and cost amplification (`https://arxiv.org/abs/2511.23278`, revised 2026-08-18). Durable agent runtime research repeats the need for event history, durable step results, timers, external events, and global retry budgets: `https://zylos.ai/research/2026-04-24-durable-execution-agent-runtimes/`. | Measure provider/job retry amplification and enforce an end-to-end budget rather than stacking local retry loops. |

**Decision:** correct the current queue lifecycle now with cancellation semantics, exact accounting, idempotency, failure classification, leases/recovery, and metrics; use PostgreSQL atomic claims for multi-worker production; reserve durable workflow infrastructure for long-running approval/replay requirements.

### 10.2 Provenance-aware agent memory

| Angle | Evidence and finding | Design consequence |
|---|---|---|
| Official | LangGraph persistence distinguishes thread checkpoints from long-term stores and warns that replayed nodes must keep side effects idempotent: `https://docs.langchain.com/oss/python/langgraph/persistence` and `https://docs.langchain.com/oss/python/langgraph/interrupts`. | Do not use a vector table as both workflow state and user facts. |
| Mature production | Zep’s provenance description demonstrates source associations, temporal invalidation, scoped retrieval, and deletion propagation: `https://blog.getzep.com/how-zep-tracks-provenance-in-agent-memory/`. | Store source/actor/time/scope and make deletion a propagation operation, not just a row delete. |
| Security/failure | OWASP identifies memory poisoning. MINJA shows query-only memory injection (`https://arxiv.org/abs/2503.03704`); GhostWriter/AM-Sentry research evaluates persistent poisoning and retrieval screening (`https://arxiv.org/html/2607.06595v1`). | Admission and retrieval screens are required; retrieved content is untrusted data, never an instruction or capability grant. |
| Alternatives/trade-offs | Keep raw turn text only; add metadata to SQLite; use a relational fact/event model plus optional vector index; adopt a graph memory product. The relational/provenance model is the smallest trustworthy correction; vector/graph expansion before trust semantics would scale the wrong risk. | Implement SQL provenance and policy first; add vector retrieval as a derived index only after fact identity/deletion are correct. |
| Recent research | Zombie Agents describes persistent control through attacker-controlled content written into long-term memory (`https://arxiv.org/html/2602.15654v1`). Origin-bound authority research proposes machine-checked provenance and non-malleable memory (`https://arxiv.org/html/2606.24322`). MemPoison measures single-record and compositional poisoning (`https://arxiv.org/html/2607.14651v1`). | Test admission, retrieval, counterfactual removal, tenant isolation, and provenance before declaring memory safe. |

**Decision:** separate checkpoints, user-approved facts, and retrieval corpus; add source/actor/time/confidence/policy/TTL/deletion/audit fields; gate admission and retrieval; preserve consent; never treat memory as trusted instructions.

### 10.3 Durable state, backups, and deployment

| Angle | Evidence and finding | Design consequence |
|---|---|---|
| Official | Twelve-Factor config and disposability require environment configuration and disposable processes: `https://12factor.net/config`, `https://12factor.net/disposability`. Koyeb volumes are local/single-machine and need snapshot planning: `https://www.koyeb.com/docs/reference/volumes`. NIST recovery guidance requires tested recovery: `https://www.nist.gov/publications/guide-cybersecurity-event-recovery`; S3 Object Lock provides immutable retention semantics: `https://docs.aws.amazon.com/AmazonS3/latest/userguide/object-lock-configure.html`. | Externalize production state and make restore a tested operator capability, not a prose step. |
| Mature production | PostgreSQL plus an object-store backup tier is the mature small-service pattern already approached by this repository; immutable object retention protects against deletion/overwrite but does not prove restore. | Keep PostgreSQL/R2 direction and add object identity, hashes, retention, restore verification, and operator evidence. |
| Security/failure | Threats include backup misconfiguration, silent empty dumps, credential compromise, malicious overwrite, schema/code rollback incompatibility, and split-brain restore. The live issue #85 proves configuration failure is currently possible. | Backups must fail closed with named missing configuration, verify bytes and schema, and be restored into isolation before success. |
| Alternatives/trade-offs | Local SQLite/WAL backup is cheapest but host-bound; PostgreSQL plus R2 costs more but supports scale-to-zero and recovery; multi-region managed workflow/database adds resilience and cost. | Use local SQLite only for explicitly single-host mode; require external DB/R2 for production mode; do not claim RTO/RPO until measured. |
| Recent research | Recent recovery literature and mapping studies continue to emphasize RTO/RPO, integrity, immutable copies, and proactive restore testing; the repository’s own failed scheduled workflow is stronger local evidence than a generic green status. | Add scheduled/manual restore drills and an evidence artifact bound to the exact source SHA and backup object. |

**Decision:** one composition-root state policy; external durable PostgreSQL and object storage for production; immutable/isolated backups where policy requires; prove restore, migration compatibility, rollback, restart, and webhook recovery.

### 10.4 Shell and untrusted execution

| Angle | Evidence and finding | Design consequence |
|---|---|---|
| Official | Python `subprocess` documents argument sequences and the danger of `shell=True`: `https://docs.python.org/3/library/subprocess.html`. gVisor describes a userspace kernel boundary: `https://gvisor.dev/docs/architecture_guide/intro/`; Firecracker documents microVM isolation: `https://github.com/firecracker-microvm/firecracker/blob/main/docs/design.md`. | Keep argv/no-shell controls; use gVisor or microVMs when the capability is arbitrary/untrusted code. |
| Mature production | gVisor and Firecracker are mature isolation building blocks, but they require image, resource, filesystem, network, and lifecycle policy. A command allow-list is cheaper and suitable only for a small trusted utility set. | Do not add a heavyweight sandbox to safe read/list operations; do not call allow-lists a complete sandbox for arbitrary code. |
| Security/failure | Systems Security Foundations identifies prompt injection as dynamic code loading and shows allow-list/configuration/exfiltration failures: `https://arxiv.org/html/2512.01295v1`. Recent sandbox research documents policy fragility and trusted handoff paths: `https://arxiv.org/html/2607.05743v1` and `https://labs.cloudsecurityalliance.org/research/csa-research-note-ai-coding-agent-sandbox-escapes-20260722-c/`. | Validate full argument grammar, prevent follow-symlink/output redirection forms, constrain environment and egress, and track provenance across files consumed by trusted processes. |
| Alternatives/trade-offs | Remove shell; expose typed filesystem/media capabilities; use a strict argv parser; use containers/gVisor; use microVMs; use WASI for a narrow language runtime. | Prefer typed capabilities and no shell. Add isolation only for a separately scoped arbitrary-code capability. |
| Recent research | Fault-tolerant transactional sandboxing proposes policy interception plus rollback (`https://arxiv.org/abs/2512.12806`); survey work emphasizes isolation overhead and incomplete defenses (`https://arxiv.org/html/2510.23883v1`). | Treat rollback as a supplement, not a replacement for isolation and complete mediation. |

**Decision:** preserve workspace filesystem controls and fail-closed allow-listing; immediately complete grammar/negative tests; use gVisor/microVM isolation for genuinely adversarial arbitrary code; never infer safety from command names alone.

### 10.5 Planner, capability, and durable agent runtime

| Angle | Evidence and finding | Design consequence |
|---|---|---|
| Official | LangGraph persistence/interrupts provide typed state, checkpoints, conditional routing, and human resume but state that side effects must be idempotent: `https://docs.langchain.com/oss/python/langgraph/persistence`, `https://docs.langchain.com/oss/python/langgraph/interrupts`. | Use the existing graph/checkpointer for explicit state, not for hidden model prose; add typed plan state and approval interrupts only where needed. |
| Mature production | Temporal’s workflow/activity split and the repository’s Nagar `CommandBus` demonstrate two mature patterns: durable control flow with idempotent activities, and typed capability authorization before effectful handlers. | Reuse the lighter Nagar-style typed command/capability/evidence contract first; add workflow infrastructure only for long-running workflow needs. |
| Security/failure | AgentDojo evaluates tool use over malicious tool results with deterministic state checks (`https://arxiv.org/abs/2406.13352`). OWASP agent guidance covers excessive agency, prompt injection, confused deputy, and approval bypass. | Tool results and memories must be data, not authority; authorization and postconditions must be machine-checked outside the model. |
| Alternatives/trade-offs | Keep keyword routing; ask an LLM for free-form plans; use a typed deterministic planner; use a full workflow engine. Keyword routing is predictable but weak; free-form plans are flexible but unsafe without validation; typed plans give the smallest complete authority boundary. | Typed plans with deterministic fallback, bounded repair/replan, explicit refusal, and evidence claims. |
| Recent research | AgentRR advocates record/replay with check functions (`https://arxiv.org/html/2505.17716v1`); LogAct makes actions durable and visible before execution (`https://arxiv.org/html/2604.07988v1`); recent LangGraph workflow work highlights repair loops, approval, checkpoint recovery, and evidence gating (`https://arxiv.org/html/2607.19297v1`). | Persist plan identity and action intent before effects; verify preconditions/postconditions and bind replay to the same authority and input versions. |

**Decision:** replace substring selection with typed plans containing capability IDs, dependencies, pre/postconditions, approval requirements, idempotency/effect keys, evidence claims, bounded repair/replan, and explicit unsupported-operation refusal.

---

## 11. Risk register

Severity is the consequence if the failure reaches a user or operator; likelihood is the likelihood in the current architecture, not a probability claim. “Open” means no sufficient merged proof exists at the anchor SHA.

| ID | Risk | Sev. | Likelihood | Current control/evidence | Treatment and exit proof | Status |
|---|---|---:|---:|---|---|---|
| R-01 | Duplicate LLM engines bypass queue, privacy, history, and routing policy | Critical | High | Constructor/call-site inventory; no one-owner guard | One composition root, identity test, architecture constructor guard, all direct paths through gateway | Open |
| R-02 | Poisoned or stale memory steers later responses/tools; deletion/tenant scope is weak | Critical | High | AI-memory consent gate; basic local memory tests | Provenance schema, admission/retrieval screens, untrusted context wrapper, deletion/TTL/tenant/adversarial tests | Open |
| R-03 | Caller timeout still executes provider request and can duplicate external cost/effect | High | High | Queue timeout exists but leaves queued request executable | Cancellation state, atomic queue-item ownership, timeout negative test, idempotency and cost budget | Open |
| R-04 | Gemini rate limits are bypassed and direct calls are not globally accounted | High | High | Per-engine limiter; app creates a queue | Gateway identity guard, queue/limiter contract, one-shot usage tests, provider metrics | Open |
| R-05 | SQLite path split writes related data into different files | Critical | Medium | Settings field and tests with explicit paths | Session factory receives runtime DB handle/path; no literal default at production call sites; architecture guard | Open |
| R-06 | One-step substring planner invokes wrong or unsupported capability | Critical | High | Unsupported step returns a typed refusal; Nagar bus has authority | Typed capability resolution, schema validation, plan proof, negative/mutation tests | Open |
| R-07 | Approval can be confused, replayed, or bypassed | Critical | Medium | Registry boolean `confirmed` gate | Signed/opaque approval token bound to user, plan, step, parameters, expiry, and one use; adversarial replay tests | Open |
| R-08 | Multi-worker claim or lease split-brain duplicates side effects | Critical | Medium in single process, high if scaled | SQLite CAS/fencing and startup recovery | PostgreSQL atomic claim/lease/recovery, kill/restart/concurrent worker tests | Open for production mode |
| R-09 | Verified webhook update disappears after process loss | Critical | Medium in scale-to-zero | Secret authentication and PTB update queue | Durable inbox or 200-after-durable-acceptance semantics; crash-after-acceptance test | Open |
| R-10 | Backup job appears configured but produces no restorable evidence | Critical | Confirmed configuration failure | Verification code exists; issue #85 and failed run | Owner-configured green backup, immutable object evidence, isolated restore drill and rollback proof | Open |
| R-11 | Shell grammar bypass or trusted handoff executes outside intended authority | High | Medium | Disabled by default, path controls, shell sandbox tests | Complete parser, environment/egress limits, mutation suite, gVisor/microVM for arbitrary code | Open; candidate #105 not merged |
| R-12 | Provider retry layers amplify quota/cost and obscure causal failures | High | Medium | LiteLLM cooldowns; image retry policy | Global run budget, typed errors, metrics, retry-storm test, cost reconciliation | Open |
| R-13 | Stale docs/Continuum cause wrong implementation or release claim | Medium | High | Version lockstep; docs integrity | Exact-SHA evidence refresh, Continuum release gate, stale-SHA scan, docs CI | Open |
| R-14 | Existing artifact/job proof regresses while new work focuses on assistant path | High | Medium | Job verification registry, Gate 5 tests, trust mutation suite | Preserve and ratchet existing gates; architecture mutation tests for bypass/removal | Guarded, not closed forever |
| R-15 | Dashboard or optional deployment surface is exposed without its optional token | High | Medium | Token gate when configured; Compose loopback bind | Production preflight rejects public exposure without token; live deployment probe | Conditional |
| R-16 | Local-only green is mistaken for full quality or production evidence | High | Confirmed in this sandbox | Main CI run exists; local packages absent | Every result records environment, SHA, job URL, skip reasons, and unverified boundaries | Open process risk |

---

## 12. Verification strategy

Every implementation wave must use the following root-cause sequence:

`symptom → reproduction → root cause → architectural cause → failure class → fix → regression proof → negative proof → mutation/architecture proof → exact-SHA CI proof`.

### 12.1 Required proof families

| Family | Required proof |
|---|---|
| Unit | Pure policy, schemas, failure classification, normalization, TTL, parser grammar, provider routing decisions. |
| Integration | Real SQLite/PostgreSQL transactions, queue claims, LangGraph checkpoint/restart, real FFmpeg/artifact verifiers, Telegram/PTB handler dispatch where relevant. |
| Adversarial/negative | Unauthorized user/callback, forged approval, replayed idempotency key with altered payload, poisoned memory, cross-tenant retrieval, stale lease, timeout, cancellation, bad artifact, shell flag/path tricks, webhook crash window. |
| System/CI | Exact startup composition, migrations, extras matrix, Python parity, release lineage, deployment contract, backup/restore, metrics/log redaction. |
| Mutation/architecture | Remove the guard, bypass the gateway, turn a typed failure into completed, skip verification, allow `-L`/attached path flags, drop provenance, or change the queue claim condition; each mutant must make a relevant gate red. |

### 12.2 Minimum regression scenarios by gap

- **G-01/G-04:** inject one fake gateway and assert graph, `/ai`, `/ask`, `/code`, `/translate`, `/summarize`, store, knowledge, and memory paths use the same policy object; a constructor outside the composition root fails the architecture guard.
- **G-02:** set a temporary `NEXUS_DB_PATH`, create a user through every relevant feature, and assert there is exactly one DB file with the expected tables; assert literal `data/app.sqlite` is not touched.
- **G-03:** enqueue a slow provider call, let the client timeout, and assert the provider factory is never invoked after cancellation; assert pending count returns exactly to baseline; repeat cancellation during rate wait and retry.
- **G-05/G-06:** inject a memory record containing an instruction to call a tool, retrieve it, and assert the planner treats it as data; assert source, actor, scope, policy, confidence, and expiry survive round trip; delete the source and assert derived retrieval disappears.
- **G-07/G-08:** unsupported capability, malformed parameters, dependency cycle, failed precondition, failed postcondition, forged/replayed approval, stale approval, and changed input must all refuse without an effect; independent steps may run concurrently only when the scheduler proves no shared effect conflict.
- **G-09/G-10:** run two worker processes against one PostgreSQL job table, prove one claim, kill a worker after an external call and before commit, reclaim by lease, and prove idempotent effect/publish; crash after webhook authentication and prove redelivery or durable inbox state.
- **G-11:** create a known DB, back it up, verify hash/size/object identity, restore into an isolated DB, run integrity and schema checks, exercise a migration rollback-compatible deploy, and record measured RPO/RTO rather than a prose number.
- **G-12:** mutate `find -L`, `find -H`, attached `-fprint=...`, `grep --file=...`, `--` handling, output redirection/environment attempts, symlink swaps, timeout, and child process behavior; then run arbitrary-code tests only inside the selected gVisor/microVM boundary.
- **G-13/G-14:** stale SHA, stale version, changed test count, missing CI leg, candidate-only job, docs link, and unmerged PR claims must fail or be visibly labelled.

### 12.3 Evidence rule

No gate result may be summarized as “green” without recording:

- exact commit SHA and branch/ref;
- command or Actions job URL;
- environment/dependency fingerprint;
- pass/fail/skip counts and skip reasons;
- mutation baseline and red result where applicable;
- whether the evidence is merged main, candidate PR, local-only, owner-only, or unverified.

---

## 13. Production readiness decision

### Ready now for

- local/single-process development with shell disabled;
- the Nagar creative path under its existing typed command, artifact, and verification contracts;
- CI-backed migration and test workflows on the exact main SHA;
- explicit polling deployments where operators accept local state and do not claim multi-worker durability.

### Blocked until P0 evidence exists

- scale-to-zero/webhook production with durable message acceptance;
- more than one worker/process sharing the job queue;
- claims that every AI feature uses one provider/rate-limit/privacy policy;
- claims that graph memory is provenance-safe, user-deletable, or instruction-safe;
- arbitrary code execution, even with the current command allow-list;
- a production backup/restore or disaster-recovery claim;
- a release/architecture statement that still points at stale SHAs or stale Continuum counts.

The implementation plan below is the approval and dependency boundary for closing these gaps. No code change is implied by this assessment.
