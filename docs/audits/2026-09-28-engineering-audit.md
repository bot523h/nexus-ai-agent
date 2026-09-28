# NEXUS AI Agent — engineering audit

**Audited:** `e5b326b2eaf691a638d030ad57acf1ce60016ef0`, 28 September 2026 UTC. **Verdict: not production-ready.** This is a source-and-execution audit, not a restatement of earlier reports. No application source was changed.

**Reading guide.** I/T = implemented with selected behavioral tests executed; I/U = implemented, command-level behavior unverified in this audit; P = partial/broken; S = simulated/stub; M = documented but missing. Passing mocked/component tests does not establish live-service success. Inventories and execution records are in [measured evidence](2026-09-28-engineering-evidence.md), [architecture](2026-09-28-architecture.md), and the [command ledger](2026-09-28-command-ledger.md). Large-file excerpts and omitted checks are explicitly recorded; not every function was manually read. [FILE:docs/audits/2026-09-28-engineering-evidence.md:209-231] [FILE:docs/audits/2026-09-28-engineering-evidence.md:268-271]

## 1. Repository metrics and hygiene

|Measurement|Observed result|
|---|---|
|Source Python|247 files; **52,275 physical LOC**, including comments/blanks|
|Tests|190 Python files, 185 `test_*.py`; **45,174 LOC**|
|Test functions|2,173: unit 1,906; integration 151; architecture 113; benchmark 3|
|TODO/FIXME/HACK/XXX comments|0 matching comment markers; not evidence of zero unfinished work|
|Local execution|**570 passes**, one skipped, one environment-related failure across two selected runs; not full suite|
|Coverage|Global unknown; selected pack scope 97.37–97.38% in exact-HEAD CI annotations|

Definitions, counts and execution results: [FILE:docs/audits/2026-09-28-engineering-evidence.md:1-8] [FILE:docs/audits/2026-09-28-engineering-evidence.md:209-231]

There were **33 open PRs**, all attributed to the Arena AI application: seven younger than one day and 26 between one and seven days at the frozen observation time. **69 merged** since August 29. Twelve complete UTC weeks beginning July 6 contain **0,0,0,0,0,0,0,0,1,0,123,220** commits, counting merges on HEAD ancestry. GitHub reports five contributors: four `User` accounts and one `Bot`; account type cannot establish human authorship. This is a burst of development, not twelve weeks of demonstrated operational stability. [FILE:docs/audits/2026-09-28-engineering-evidence.md:149-208]

`VERSION` and pyproject say **3.13.0**, README's banner says **3.12.0**, latest fetched version-sorted tag is **v3.5.0**. Keep README, CHANGELOG, CONTRIBUTING and AGENTS at root; relocate dated coordination/audit documents into `docs/audits/` or `docs/coordination/`, preserving links. Candidates include the September 21 coordination, September 26 baseline and September 27 truth report. [FILE:VERSION:1] [FILE:pyproject.toml:5-10] [FILE:README.md:5] [FILE:docs/audits/2026-09-28-engineering-evidence.md:208] [FILE:COORDINATION_2026-09-21.md:1] [FILE:LIVE_ENGINEERING_BASELINE_2026-09-26.md:1] [FILE:MASTER_ENGINEERING_TRUTH_REPORT_2026-09-27.md:1]

## 2. Architecture and dependencies

|Area|Purpose / Python files|Principal coupling|
|---|---|---|
|bot / api|Telegram surfaces 26; HTTP dashboard/webhooks 2|Features, storage, orchestration; Telegram/FastAPI|
|agent / agents|Maintenance/approval 5; persona/store wrappers 11|Storage, LLM, configuration|
|orchestration / llm / memory|Graph 4; providers 8; recall 4|LangGraph, HTTP, sqlite-vec/local models|
|features / knowledge / integrations|User engines 27; retrieval 4; services 3|SQLModel, HTTP, Chroma, FlashRank|
|creative|63: packs, project model, images, captions, rendering|Pydantic, Pillow, NumPy, HTTP, FFmpeg|
|storage|28: ORM, migrations, checkpoints, cloud|SQLAlchemy, LangGraph savers, psycopg, boto3|
|application / domain / adapters / jobs|9 / 6 / 6 / 6: ports, policies, implementations, jobs|Creative and infrastructure|
|Remaining|Config 2; continuum 6; core 5; i18n 2; infrastructure 3; observability 2; maintenance 3; personality 2; tools 6; CLI/presence/worker/init 4|Configuration, evidence, utilities and process composition|

The complete recursive file counts and per-subtree internal/third-party dependencies are supplied, rather than hiding smaller modules. [FILE:docs/audits/2026-09-28-engineering-evidence.md:9-39] [FILE:docs/audits/2026-09-28-architecture.md:1-45]

The resolved static import graph has **247 modules, 592 edges, three cyclic components**: storage bootstrap/migrations; API↔webhook; continuum↔snapshot. Deferred imports prevent this being proof of runtime import failure. Package initializers are mostly empty or re-exports; `bot.surface` and creative initializers aggregate substantial imports. Full adjacency and initializer classifications are attached. [FILE:docs/audits/2026-09-28-architecture.md:1-6] [FILE:docs/audits/2026-09-28-architecture.md:46-82]

Key contracts are `LLMProvider.generate(prompt, system="") -> str`, `embed(text) -> list[float]`, `build_handlers(graph, db_session_factory, settings, presence, storage, feature_engines=None)`, and `compile_graph(llm, checkpointer, long_term_memory, tool_registry)`. String-only model results discard structured usage/error information. [FILE:src/nexus_ai_agent/llm/provider.py:6-13] [FILE:src/nexus_ai_agent/bot/handlers.py:198-205] [FILE:src/nexus_ai_agent/orchestration/graph.py:170-175]

The graph has **11 nodes**. START→router selects task-memory→planner→executor, memory-chat→persona routing, or direct persona routing. Qwen/Phi/Gemma→moderation→memory-writer→END. All three wrappers receive the **same provider**, not three independently deployed models. Tool planning/execution exists; persona names alone are misleading architecture evidence. [FILE:src/nexus_ai_agent/orchestration/graph.py:48-145] [FILE:src/nexus_ai_agent/orchestration/graph.py:178-279]

All declared version constraints are transcribed in the evidence: **32 runtime dependencies**, ten exact pins, 21 lower-only constraints, one bounded range; four extras. `pypdf` duplication across pdf/dev is intentional. Direct imports of gTTS and MEGA are not declared; `langchain-community` and `duckduckgo-search` deserve removal/reclassification investigation because no corresponding direct source imports were found. Framework-consumed packages such as multipart must not be called unused merely from import absence. [FILE:pyproject.toml:13-68] [FILE:src/nexus_ai_agent/features/speech.py:57-83] [FILE:src/nexus_ai_agent/storage/providers/mega.py:21-29] [FILE:docs/audits/2026-09-28-engineering-evidence.md:9-39]

Permitted minima are unsafe: multipart 0.0.9 admits CVE-2024-53981, Pillow 10.0.0 admits CVE-2023-50447, langchain-community 0.2.0 admits CVE-2024-5998, and build setuptools 69 admits CVE-2024-6345. These are **allowed versions, not proven deployed vulnerabilities**; raise floors and audit a resolved production lock. The reduced-environment scan only found vulnerable audit tooling; the full lower-bound scan failed with HTTP 503. [FILE:pyproject.toml:2-45] [FILE:docs/audits/2026-09-28-engineering-evidence.md:232-241]

## 3. Identity, authentication and secrets

`User` has `id`, unique/indexed `telegram_id`, `username`, `created_at`, and `is_allowed=True`. Authorization instead uses settings owner/allowlist; changing the ORM flag is not an access-control operation. Telegram IDs, internal Chat IDs and `tg:<chat_id>` memory threads are distinct namespaces. [FILE:src/nexus_ai_agent/storage/models.py:8-30] [FILE:src/nexus_ai_agent/bot/middleware.py:24-40] [FILE:src/nexus_ai_agent/bot/handlers.py:179-185]

The global group-minus-one guard is genuinely deny-default for identifiable users; denial ends with `ApplicationHandlerStop`, including callbacks, and replies are throttled to three per 60 seconds. Updates with **no effective user pass the guard**, so “every update authorized” is too strong; review supported channel/system update paths. [FILE:src/nexus_ai_agent/bot/app.py:347-359] [FILE:src/nexus_ai_agent/bot/access_guard.py:61-62] [FILE:src/nexus_ai_agent/bot/access_guard.py:79-120]

Dashboard bearer comparison uses `hmac.compare_digest`, but **unset token means open API**. Compose loopback mitigates that deployment, not arbitrary public deployment. HMAC-protected job endpoints and Telegram webhook secrets separately fail closed. No JWT issuer/verifier was found; these mechanisms are not JWT authentication. [FILE:src/nexus_ai_agent/api/dashboard.py:18-43] [FILE:src/nexus_ai_agent/api/app.py:74-103] [FILE:src/nexus_ai_agent/api/app.py:414-429] [FILE:docker-compose.yml:12-20] [FILE:docs/audits/2026-09-28-engineering-evidence.md:243]

Secret-bearing settings include Telegram, Gemini, GitHub, Hugging Face, Google Drive, Dropbox, pCloud, Internxt, News, YouTube, Groq, OpenRouter, creative Gemini, webhook/HMAC/dashboard, R2 access/secret keys and MEGA email/password. They are ordinary strings/environment values, not a vault-backed identity system; database URLs can also contain passwords. MEGA retains plaintext credentials in process memory. No user password-login subsystem was identified. [FILE:src/nexus_ai_agent/config/settings.py:19-22] [FILE:src/nexus_ai_agent/config/settings.py:75-112] [FILE:src/nexus_ai_agent/config/settings.py:131-194] [FILE:src/nexus_ai_agent/config/settings.py:245-259] [FILE:src/nexus_ai_agent/config/settings.py:306-344] [FILE:src/nexus_ai_agent/config/settings.py:400-412] [FILE:src/nexus_ai_agent/storage/providers/mega.py:13-29]

## 4. Providers, memory and prompts

LiteLLM supplies configured routing, request timeouts, quota cooldown and immediate fallback rather than same-deployment retry (`num_retries=0`). The outer factory **always permits FakeLLM**, including production configuration failures; degraded fallback has a disclaimer, but standalone Fake is not environment-gated. No provider-wide token/cost ledger is exposed by the string interface. [FILE:src/nexus_ai_agent/llm/litellm_provider.py:154-174] [FILE:src/nexus_ai_agent/llm/litellm_provider.py:221-294] [FILE:src/nexus_ai_agent/llm/litellm_provider.py:297-345] [FILE:src/nexus_ai_agent/llm/fallback_provider.py:73-115]

Gemini converts HTTP errors into ordinary text, then appends that text as assistant history. Exception-driven retry cannot reliably distinguish these failures from success. `/ai` constructs a second engine without the application's configured conversation store/request queue: implementation in one composition root does not prove the command uses it. [FILE:src/nexus_ai_agent/features/ai_chat.py:160-192] [FILE:src/nexus_ai_agent/features/ai_chat.py:233-244] [FILE:src/nexus_ai_agent/features/request_queue.py:110-120] [FILE:src/nexus_ai_agent/bot/handlers.py:237-248] [FILE:src/nexus_ai_agent/bot/app.py:230-265]

Short-term windows, persisted conversation history, LangGraph checkpoints, 384-dimensional long-term vectors, and personal `UserMemory` are **different stores**. Gemini/LiteLLM hash-derived pseudo-embeddings are not semantic representations; fallback retrieval uses recency. Graph turns persist regardless of AIMemory consent. `/forget_me` deletes only the personal-memory row, not conversations, vectors or checkpoints. [FILE:src/nexus_ai_agent/memory/short_term.py:6-11] [FILE:src/nexus_ai_agent/features/conversation_store.py:21-49] [FILE:src/nexus_ai_agent/memory/long_term.py:10-116] [FILE:src/nexus_ai_agent/llm/litellm_provider.py:221-231] [FILE:src/nexus_ai_agent/orchestration/graph.py:147-166] [FILE:src/nexus_ai_agent/features/ai_memory.py:225-237]

AIMemory extraction itself correctly checks global enablement, explicit consent and minimum egress interval. That is not a global “no text leaves this machine” switch. Prompts interpolate user text and recalled content; treat those as untrusted data, not instructions. Phi's malformed moderation JSON defaults safe, making moderation fail-open. [FILE:src/nexus_ai_agent/features/ai_memory.py:123-167] [FILE:src/nexus_ai_agent/agents/phi_agent.py:22-36]

## 5. Command truth versus README

The README table expands to **86 command claims: 54 Real, 32 Simulated**; main handlers register 112 literal commands. The ledger conservatively separates **19 I/T, 48 I/U, six P, 11 S, two M**. I/U does not mean no tests exist; it means this audit does not establish command behavior from selected tests. [FILE:README.md:302-315] [FILE:docs/audits/2026-09-28-command-ledger.md:1-93]

|Surface|Actual behavior|
|---|---|
|`vision`, `leaderboard`, `newchat`|Canned image/ranking replies; history-reset acknowledgment without reset. [FILE:src/nexus_ai_agent/bot/handlers.py:325-326] [FILE:src/nexus_ai_agent/bot/handlers.py:376-385] [FILE:src/nexus_ai_agent/bot/handlers.py:829-834]|
|`ban` / `unban`|Real owner-gated Telegram calls, contrary to Simulated label. [FILE:src/nexus_ai_agent/bot/surface/channel_management.py:327-372]|
|`mod_config`, `warn`, `mute`, `unmute`, `reputation`|Canned stubs, not moderation enforcement. [FILE:src/nexus_ai_agent/bot/handlers.py:1187-1205]|
|`daily`, `xp_leaderboard`, `achievements`|Real DB-backed engine effects; targeted tests passed. Distinguish old `/leaderboard`. [FILE:tests/unit/test_surface_gamification.py:48-143]|
|`docs`, `doc_delete`, `chat_with_doc`|Real store/retriever; document chat returns retrieved quotations, not generated RAG. Retrieval searches across the user’s documents before selected-document filtering, which can reduce recall. [FILE:src/nexus_ai_agent/bot/surface/docs.py:281-310] [FILE:tests/unit/test_surface_docs.py:1-40]|
|`viral_now`|Template generation/scoring/persistence, not demonstrated viral discovery or automatic publishing. Other three viral commands are canned. [FILE:src/nexus_ai_agent/bot/handlers.py:1134-1155]|
|`ad_create` and five other ad commands|Real CRUD and stats, selected tests passed; delivery loop explicitly not wired. [FILE:src/nexus_ai_agent/bot/surface/ads.py:24-40] [FILE:tests/unit/test_surface_ads.py:83-120]|
|`image`, `tts`|Engines return `path`; handlers demand `file_path`, rejecting successful results. [FILE:src/nexus_ai_agent/features/image_gen.py:195-201] [FILE:src/nexus_ai_agent/features/speech.py:72-77] [FILE:src/nexus_ai_agent/bot/handlers.py:410-421] [FILE:src/nexus_ai_agent/bot/handlers.py:471-479]|

The other four partial entries are cloud upload/list/download/status; DB/API-contract defects are detailed below. `/language`, outside the table denominator, also calls the unsupported session method. `/companion` and `/analyze` are documented but absent from registrations. Additional non-table stubs include `/storage`, `/model`, `/story_style`, and the presence heartbeat. Simulations are not consistently labeled in the user-facing reply. [FILE:src/nexus_ai_agent/bot/handlers.py:637-667] [FILE:src/nexus_ai_agent/bot/handlers.py:752-772] [FILE:src/nexus_ai_agent/bot/handlers.py:1300-1478] [FILE:src/nexus_ai_agent/bot/handlers.py:1568-1582]

## 6. Security review

|Boundary|Finding|
|---|---|
|SQL|Reviewed raw execution sites use bound values; schema-name interpolation uses controlled identifiers. No demonstrated user-controlled SQL injection. Inventory is attached, not a claim of formal proof. [FILE:docs/audits/2026-09-28-engineering-evidence.md:104-148]|
|Paths|`safe_join` resolves and enforces containment; cloud commands sanitize local names. Remote object names remain a separate tenant-isolation issue. [FILE:src/nexus_ai_agent/bot/safe_paths.py:32-72] [FILE:src/nexus_ai_agent/bot/handlers.py:596-607]|
|SSRF|HTTP transport validates DNS/IP at connection time and redirects; tests passed. Generic HTTP responses are still buffered, with no universal download-size cap established. [FILE:src/nexus_ai_agent/core/http_client.py:127-174] [FILE:tests/unit/test_http_client_ssrf.py:1-35]|
|Command injection|Renderer uses argv, not shell interpolation. This lowers shell-injection risk; it does not establish safety of all media decoders. [FILE:src/nexus_ai_agent/creative/slideshow/ffmpeg.py:485-492]|
|Prompt injection|User/memory content is interpolated; moderation is not a security boundary for executing tool actions. Malformed classification fails open. [FILE:src/nexus_ai_agent/features/ai_memory.py:143-157] [FILE:src/nexus_ai_agent/agents/phi_agent.py:22-36]|
|Abuse controls|Denial throttling is not an authenticated-user workload budget; queue fan-out remains unbounded. HTTP 429 is not retried by generic 5xx-only logic. [FILE:src/nexus_ai_agent/bot/access_guard.py:99-120] [FILE:src/nexus_ai_agent/adapters/in_process_job_queue.py:408-414] [FILE:src/nexus_ai_agent/core/http_client.py:173-200]|

History scanning found **one high-specificity token-pattern match**, in the redaction test fixture, and **zero confirmed live credentials**; no tracked `.env` history. This was not an exhaustive entropy/credential-validity audit. Docker has no `USER`, retains build tools, and installs broad dependency ranges: run non-root, reduce runtime packages, pin/audit the artifact. [FILE:tests/unit/test_observability.py:26-28] [FILE:docs/audits/2026-09-28-engineering-evidence.md:242] [FILE:Dockerfile:1-38]

## 7. Database and migrations

**Reproduced release blocker:** `get_session()` yields SQLAlchemy `AsyncSession`, while user/chat/cloud/language handlers call SQLModel-style `.exec()`. A fresh real SQLite session produced `AttributeError`; ordinary message handling reaches the broken upsert before graph execution. Replace the session contract consistently and test production wiring, not a permissive fake. [FILE:src/nexus_ai_agent/storage/db.py:202-203] [FILE:src/nexus_ai_agent/bot/handlers.py:127-154] [FILE:src/nexus_ai_agent/bot/handlers.py:871-872] [FILE:docs/audits/2026-09-28-engineering-evidence.md:218-223]

The appendix enumerates every SQLModel field/default/index: **29 tables**, including duplicate Referral/ReferralCode definitions in the feature module. Only Message→Chat, Task→Chat and ToolRun→Task declare foreign keys; most user/chat references are unvalidated integers, with no ORM relationship graph. Sidecars add memories, conversation history, job queue and lifecycle/checkpoint schemas outside that ORM inventory. [FILE:docs/audits/2026-09-28-engineering-evidence.md:40-103] [FILE:src/nexus_ai_agent/storage/models.py:24-50] [FILE:src/nexus_ai_agent/memory/long_term.py:45-59] [FILE:src/nexus_ai_agent/features/conversation_store.py:40-59] [FILE:src/nexus_ai_agent/adapters/in_process_job_queue.py:748-775]

Three Alembic revisions form one chain: initial→lifecycle→consent. Initial downgrade drops 29 tables; later downgrades remove lifecycle/consent data. Backup-before-downgrade is mandatory. Migration locking uses host-local `flock`, not cross-host PostgreSQL coordination. [FILE:migrations/versions/47903d282ede_initial_schema.py:481-509] [FILE:migrations/versions/f4a9c2e71b08_nexus_checkpoint_lifecycle.py:41-68] [FILE:migrations/versions/7c2f9d41e8a3_ai_memory_consent.py:40-64] [FILE:src/nexus_ai_agent/storage/migrations.py:51-86]

Application DB enables SQLite WAL and uses async SQLAlchemy, but feature engines still create synchronous SQLite engines; PostgreSQL configuration therefore does not automatically move every feature. Checkpoints enable SQLite WAL or use a PostgreSQL pool of 1–10. Retention policy is 30 days plus seven-day active grace, but maintenance-adapter deletion is deliberately `NotImplemented`: do not claim automatic bounded storage. [FILE:src/nexus_ai_agent/storage/db.py:192-205] [FILE:src/nexus_ai_agent/storage/db.py:265-275] [FILE:src/nexus_ai_agent/features/channel_manager.py:65-94] [FILE:src/nexus_ai_agent/storage/langgraph_checkpoint.py:228-249] [FILE:src/nexus_ai_agent/storage/langgraph_checkpoint.py:290-297] [FILE:src/nexus_ai_agent/storage/checkpoint_lifecycle.py:59-104] [FILE:src/nexus_ai_agent/storage/checkpoint_pg_adapter.py:198-203]

## 8. Queue, concurrency and render execution

The job queue is real: SQLite persistence, unique idempotency keys, typed terminal/retryable states and attempt fencing. Recovery assumes **one owner per sidecar**; startup takes unfinished rows and shutdown resets them. That is not a distributed worker lease design. Tasks launch without a semaphore; 95 `to_thread` sites share the default executor with no explicit project pool sizing found. Bound both backlog and active work before load-testing. [FILE:src/nexus_ai_agent/adapters/in_process_job_queue.py:358-435] [FILE:src/nexus_ai_agent/adapters/in_process_job_queue.py:748-819] [FILE:docs/audits/2026-09-28-engineering-evidence.md:243]

Retryable/terminal rows provide inspection, but the queue explicitly has **no retry scheduler**; these states are not a separate dead-letter service. [FILE:src/nexus_ai_agent/adapters/in_process_job_queue.py:46-55] Channel schedules persist metadata but execute through in-memory sleeping tasks; no restart replay is shown there. Reminders separately restore pending work—do not conflate them. [FILE:src/nexus_ai_agent/adapters/in_process_job_queue.py:384-428] [FILE:src/nexus_ai_agent/features/channel_manager.py:65-96] [FILE:src/nexus_ai_agent/bot/app.py:312-323]

FFmpeg has a 900-second ceiling and staging-file cleanup. Publication rename occurs **before** probing, so probe failure can leave a published artifact. Cancelling an `await to_thread(...)` does not stop its underlying render thread; the worker's `finally` can delete inputs while rendering continues. This is a source-supported cancellation risk, not a reproduced corruption incident. [FILE:src/nexus_ai_agent/creative/slideshow/ffmpeg.py:41-42] [FILE:src/nexus_ai_agent/creative/slideshow/ffmpeg.py:629-668] [FILE:src/nexus_ai_agent/creative/slideshow/worker_adapter.py:290-303]

## 9. Cloud storage and capacity claims

The bot's unified-cloud construction supplies Dropbox, pCloud and Internxt; a separate AIStorageManager chooses GitHub below 50 MiB, rclone below 500 MiB, MEGA otherwise. R2 is a technical-blob tier, not proof of extra user-file capacity. Do not describe the alternate manager as the `/cloud` path. [FILE:src/nexus_ai_agent/bot/handlers.py:218-225] [FILE:src/nexus_ai_agent/storage/ai_storage_manager.py:96-111] [FILE:src/nexus_ai_agent/storage/providers/r2.py:21-31]

|Defect|Consequence|
|---|---|
|Upload returns `remote_key`; handler stores `remote_path`|Successful upload metadata stores an empty locator. [FILE:src/nexus_ai_agent/storage/unified_cloud.py:393-401] [FILE:src/nexus_ai_agent/bot/handlers.py:610-620]|
|Original filename used as shared remote key|Cross-user collision/incorrect retrieval risk; provider behavior determines overwrite versus rename. [FILE:src/nexus_ai_agent/bot/handlers.py:605-607]|
|Dropbox accepts 409 and ignores authoritative returned path|Conflict can become false success; autorename can break later lookup. [FILE:src/nexus_ai_agent/storage/unified_cloud.py:78-93]|
|pCloud checks HTTP status only; Internxt download unsupported|Provider-level failures can evade failover; upload/download parity absent. [FILE:src/nexus_ai_agent/storage/unified_cloud.py:171-184] [FILE:src/nexus_ai_agent/storage/unified_cloud.py:233-254]|

Round-robin selection ignores its size parameter; the file map is process-local. Declared capacities sum to **1,058 GB**, including a **999 GB Hugging Face sentinel**, not measured quotas. The advertised 57 GB is arithmetic over nominal plans, not a verified entitlement. No live account capacity was tested. [FILE:src/nexus_ai_agent/storage/unified_cloud.py:28-39] [FILE:src/nexus_ai_agent/storage/unified_cloud.py:334-344] [FILE:src/nexus_ai_agent/storage/unified_cloud.py:394] [FILE:README.md:201-211]

MEGA creates/logs in a client per operation, stores credentials as strings, and has no established persistent session or explicit operation deadline. rclone uses argv but waits without timeout. Implement provider-contract tests for errors, names, retries and restart recovery before promising durability. [FILE:src/nexus_ai_agent/storage/providers/mega.py:13-60] [FILE:src/nexus_ai_agent/storage/providers/rclone.py:29-71]

## 10. Tests and CI

Exact-HEAD CI reports **16/16 jobs successful**, including lint/type/version checks, Python parity, extras, PostgreSQL migrations, pack trust mutations and continuum evidence. Coverage threshold is **95% per selected pack**, not project-wide. Branch-protection inspection returned 403, so required merge checks remain unknown. No general dependency-vulnerability scan was established from the reviewed workflow. [FILE:docs/audits/2026-09-28-engineering-evidence.md:226-231] [FILE:.github/workflows/ci.yml:1-110] [FILE:.github/workflows/ci.yml:196-244]

Pre-commit pins Ruff **0.16.8** and mypy **1.10.0**; mypy ignores missing imports. Existing component tests meaningfully exercise ads, gamification, access control, SSRF and real FFmpeg. Conversely, handler registration tests prove registration, not real DB/engine contracts. No literal constant-true assertions were found; “no direct assert” is not a valid useless-test metric because `pytest.raises` and mock assertions count. [FILE:.pre-commit-config.yaml:9-24] [FILE:tests/unit/test_handlers.py:8-41] [FILE:tests/unit/test_slideshow_render.py:1-40] [FILE:docs/audits/2026-09-28-engineering-evidence.md:211-217] [FILE:docs/audits/2026-09-28-engineering-evidence.md:243]

**Packaging blocker:** the built wheel has **253 entries and zero JSON/locales/manifests/migrations**. Slideshow loads package-relative JSON, and migrations resolve a checkout-relative Alembic tree. Editable CI hides this; Docker uses regular `pip install .`. Add isolated wheel-install resource/startup tests before more mocked coverage. [FILE:docs/audits/2026-09-28-engineering-evidence.md:224-225] [FILE:pyproject.toml:71-75] [FILE:src/nexus_ai_agent/creative/packs/slideshow/templates.py:184-188] [FILE:src/nexus_ai_agent/storage/migrations.py:102-110] [FILE:Dockerfile:20-24]

## 11. Creative packs, images and slideshow

Six packs register slideshow, caption, edit, motion, audio and delivery. Registration/trust/schema tests are not proof every operation transforms media: trim derives metadata/hash, and beat detection fixes tempo at **120 BPM** without inspecting audio. The separate renderer/compiler is real. Describe metadata operations as such rather than “finished DSP.” [FILE:src/nexus_ai_agent/creative/packs/runtime.py:130-169] [FILE:src/nexus_ai_agent/creative/packs/edit/operations.py:60-103] [FILE:src/nexus_ai_agent/creative/packs/audio/operations.py:73-103] [FILE:src/nexus_ai_agent/creative/rendering/compiler.py:1-85]

New image adapters are substantially stronger than legacy `/image`: paid Gemini fails closed, response/image caps are 24/8 MiB, decoded-image cap 16,777,216 pixels, default three attempts, 90-second HTTP timeout, single-flight cache with 16 entries/32 MiB/one-hour TTL. Cost is operator-estimated, not billed usage. [FILE:src/nexus_ai_agent/creative/image_gen/gemini_adapter.py:34-40] [FILE:src/nexus_ai_agent/creative/image_gen/resilience.py:24-123]

Slideshow revalidates **one–five images and ≤30 seconds** at the worker boundary; upload sessions cap at 512 with lazy 30-minute expiry. Payload containment, argv execution and input/output cleanup exist. Orphan cleanup is job-triggered after 24 hours, not a guaranteed background sweeper; cancellation needs the fix described above. [FILE:src/nexus_ai_agent/creative/slideshow/worker_adapter.py:40-56] [FILE:src/nexus_ai_agent/creative/slideshow/worker_adapter.py:79-105] [FILE:src/nexus_ai_agent/bot/slideshow.py:43-105] [FILE:src/nexus_ai_agent/creative/slideshow/worker_adapter.py:263-303]

## 12. Internationalization

There are **15 locale JSONs, 79 keys each, zero missing keys against English**. Non-English files have two–four exact English-value matches, which alone does not establish bad translation. Requested language falls back to English, then the key. This measures dictionary parity, not full UX localization: many handlers hardcode English/Persian, and the wheel omits the dictionaries entirely. Translation quality was not linguistically certified. [FILE:docs/audits/2026-09-28-engineering-evidence.md:244-259] [FILE:src/nexus_ai_agent/i18n/__init__.py:90-121] [FILE:src/nexus_ai_agent/bot/handlers.py:448-469] [FILE:docs/audits/2026-09-28-engineering-evidence.md:224-225]

## 13. Observability

Structured logging, context-bound job IDs, credential-pattern redaction and an in-memory metrics registry are real. No Prometheus/StatsD/OpenTelemetry imports were found; correlation IDs are not distributed tracing or an operational metrics backend. Redaction covers common tokens, bearer/basic credentials and URL credentials, but not arbitrary sensitive conversation content: legacy image logging records prompts. Add retention/access policy and an exporter, not just more log events. [FILE:src/nexus_ai_agent/observability/logging.py:32-107] [FILE:src/nexus_ai_agent/adapters/in_process_job_queue.py:430-435] [FILE:src/nexus_ai_agent/infrastructure/observability/metrics.py:1-46] [FILE:src/nexus_ai_agent/features/image_gen.py:188-189] [FILE:docs/audits/2026-09-28-engineering-evidence.md:243]

## 14. Deployment and developer operations

Compose persists bot data/assets, binds dashboard to host loopback, but gives dashboard **no shared data volume** and defines no healthchecks. Without an external DB override, the two services do not share the intended SQLite state. Koyeb declares webhook mode and `/healthz`, but no persistent volume/shared queue configuration; scale-to-zero claims do not establish durable work or horizontal safety. [FILE:docker-compose.yml:1-20] [FILE:koyeb.yaml:1-24] [FILE:src/nexus_ai_agent/adapters/in_process_job_queue.py:367-375]

Make exposes setup, bootstrap, hooks, version-check, lint, types, test, mutations, migrate, smoke and run. Setup is repeatable package installation, **not reproducible** dependency resolution; native llama-cpp installation exceeded this audit's 180-second attempt. Migration replay is more disciplined than setup, but neither Docker build success nor a liveness endpoint proves ready providers, resources and storage. [FILE:Makefile:1-36] [FILE:pyproject.toml:13-68] [FILE:src/nexus_ai_agent/api/app.py:217-226] [FILE:docs/audits/2026-09-28-engineering-evidence.md:210]

The bootstrap wrapper reuses an existing virtual environment and offers a verification-only mode; that is useful setup idempotence, but it is not evidence that repeatedly resolving every broad runtime dependency yields identical builds. Keep the distinction between a repeatable script and a reproducible environment explicit. [FILE:scripts/bootstrap_dev.sh:1-16] [FILE:pyproject.toml:13-45]

## 15. Priorities, strengths and readiness

### Five critical-priority release blockers

These are **release priorities, not five confirmed Critical security vulnerabilities**. Evidence does not justify inventing that severity count.

|Priority|Blocker / required proof|
|---|---|
|C1|Fix real session/handler contract; run message/cloud/language integration against actual sessions. [FILE:src/nexus_ai_agent/storage/db.py:202-203] [FILE:src/nexus_ai_agent/bot/handlers.py:130-145]|
|C2|Ship resources/migrations; install wheel outside checkout and start all advertised surfaces. [FILE:docs/audits/2026-09-28-engineering-evidence.md:224-225]|
|C3|Correct cloud locator/tenant contracts; verify upload→restart→download with same-name users. [FILE:src/nexus_ai_agent/bot/handlers.py:605-620]|
|C4|Make FakeLLM explicitly development-only; propagate typed provider failures. [FILE:src/nexus_ai_agent/llm/litellm_provider.py:317-345]|
|C5|Establish durable single-owner deployment and bounded workload admission before scaling. [FILE:src/nexus_ai_agent/adapters/in_process_job_queue.py:358-414] [FILE:koyeb.yaml:1-24]|

**Release exit criteria:** test a built wheel from a clean directory, with no checkout imports or package-relative source files accidentally available. Exercise a permitted Telegram update through the real session factory; upload two identically named files for different users, restart, and download both. Inject a provider timeout and quota rejection and require explicit failure or clearly identified degradation, never ordinary successful history. Interrupt a render and verify that no process survives cleanup and no incomplete artifact is advertised. These are proposed acceptance tests for the documented contract failures, not claims that those scenarios already passed. [FILE:docs/audits/2026-09-28-engineering-evidence.md:218-225] [FILE:src/nexus_ai_agent/bot/handlers.py:605-620] [FILE:src/nexus_ai_agent/features/ai_chat.py:233-244] [FILE:src/nexus_ai_agent/creative/slideshow/worker_adapter.py:290-303]

### Five High priorities

1. Default-close public dashboard; require explicit private-development exception. [FILE:src/nexus_ai_agent/api/dashboard.py:29-35]
2. Define global memory consent/erasure boundaries and clear all promised stores. [FILE:src/nexus_ai_agent/features/ai_memory.py:225-237] [FILE:src/nexus_ai_agent/orchestration/graph.py:156-164]
3. Repair `/image` and `/tts` result contracts and dependency packaging. [FILE:src/nexus_ai_agent/bot/handlers.py:410-421] [FILE:src/nexus_ai_agent/bot/handlers.py:471-479]
4. Bound render cancellation/process lifetime; verify before publishing artifacts. [FILE:src/nexus_ai_agent/creative/slideshow/worker_adapter.py:290-303] [FILE:src/nexus_ai_agent/creative/slideshow/ffmpeg.py:666-668]
5. Raise vulnerable dependency floors, resolve/pin production dependencies and scan in CI. [FILE:pyproject.toml:13-45] [FILE:docs/audits/2026-09-28-engineering-evidence.md:232-241]

### Five Medium priorities

1. Replace hash vectors with genuine semantic embeddings. [FILE:src/nexus_ai_agent/llm/litellm_provider.py:221-231]
2. Wire recovery/delivery for schedules and campaigns. [FILE:src/nexus_ai_agent/features/channel_manager.py:80-96] [FILE:src/nexus_ai_agent/bot/surface/ads.py:24-40]
3. Reconcile README status, banner and release tags. [FILE:README.md:5] [FILE:README.md:310-315]
4. Export metrics and remove raw prompt logging. [FILE:src/nexus_ai_agent/infrastructure/observability/metrics.py:1-46] [FILE:src/nexus_ai_agent/features/image_gen.py:189]
5. Distinguish metadata simulations from rendered results. [FILE:src/nexus_ai_agent/creative/packs/audio/operations.py:89-94] [FILE:src/nexus_ai_agent/creative/packs/edit/operations.py:73-87]

**Five strengths:** deny-default Telegram guard; connection-time SSRF/path defenses; meaningful real-DB surface tests; actual FFmpeg rendering with paid-image guardrails; SHA-bound creative coverage/mutation evidence. These are concrete foundations, not compensation for broken composition. [FILE:src/nexus_ai_agent/bot/access_guard.py:79-120] [FILE:src/nexus_ai_agent/core/http_client.py:143-162] [FILE:tests/unit/test_surface_ads.py:83-120] [FILE:src/nexus_ai_agent/creative/slideshow/ffmpeg.py:655-668] [FILE:.github/workflows/ci.yml:217-233]

|Readiness question|Answer|
|---|---|
|Private development / selected components useful?|**Yes**, with deliberate configuration and known defects.|
|Production single-instance service?|**No**: reproduced session and packaging blockers.|
|Public multi-user / horizontal autoscaling?|**No**: tenant keys, queue ownership, persistence and admission unresolved.|
|Security foundation / automated quality checks?|**Partial**: strong local controls, incomplete deployment assurance.|

The scoped README status-agreement score is **59/86 = 68.6%**: 48 original Real entries have code-backed paths without an identified blocking contract defect, and 11 Simulated entries are genuinely canned. Nineteen other entries are now implemented despite being called simulated, six are partial/broken, and two are missing. **This is not “68.6% of the product works.”** The stricter, deliberately conservative executed-surface verification subset is **19/86 = 22.1%**; no defensible percentage covers every README prose claim. Fix and prove the shipped composition before expanding its advertised surface. [FILE:docs/audits/2026-09-28-command-ledger.md:1-93]
