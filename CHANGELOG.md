# Changelog

All notable changes to NEXUS AI Agent will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Nagar Phase 6, Wave 2a — the capability-pack substrate
  (`src/nexus_ai_agent/creative/packs/`)** (PR#21): the TDD rule “a pack is
  data plus pre-registered adapters, never arbitrary code” is now mechanical.
  - `manifest.py`: the strict `nexus.capability-pack.v1` manifest — artifact
    digests and relative paths, runtime/network policy, permissions, hardware
    and resource budgets, compatibility, security — with `extra="forbid"`
    everywhere, so `post_install` / `entrypoint` / `shell` / `hooks` fail
    validation instead of being ignored, and `Literal[False]` code gates.
  - `verify.py`: verification that returns **every** finding (never a bare
    boolean) — artifact paths and digests, `runtime_network` forbiddance, media
    egress only with the explicit `egress_media_optin` permission, native
    `network_required` rejection, unknown-permission warnings, signature state
    (`format_only_unverified`, never implied trust), `min_nagar_version`.
  - `registry.py`: `PackRegistry` over the studio capability registry —
    external packs may not introduce unknown operations; builtin packs register
    with *pending* capabilities and activate only once the runtime knows them
    (checked against the live registry, so Wave 2b needed no rework).
  - `slideshow/pack.manifest.json`: the eighth pack
    (`nexus.slideshow.compose`) — five capabilities, `ffmpeg` as a declared
    external binary, opt-in media egress declared truthfully,
    `min_nagar_version 3.10.0`.
  - CLI: `nexus packs list` / `nexus packs verify`; gates in
    `tests/architecture/test_pack_manifest_is_data_only.py` (data-only
    manifests at any depth, import allow-list, no package crossing).
- **Nagar Phase 6, Wave 2b — the slideshow pack plans and composes real
  footage (`nexus.slideshow.compose`)**: five pure operations, one atomic edit.
  - `packs/slideshow/`: the typed payloads (`AssetEvidence`, `BeatGrid`,
    `ImageScore`, `SlideshowAnalysis`, `ComposeInput`, `SlideshowPlan`,
    `RenderInput`), the tone library, the deterministic planning rules and the
    five operations — `slideshow.scan_assets` (B), `slideshow.score_images` (A),
    `slideshow.suggest_tone` (A), `slideshow.compose` (B), `slideshow.render`
    (C, the first level-C operation: heavy export requires `confirmed=true`).
  - `templates/tone_templates.json`: **14 tone templates** (12 primary + 2
    alternates) as *data* — rhythm, transition, motion, color, audio and render
    defaults, never a filtergraph. Plain JSON by design: no new YAML dependency.
  - Planning guarantees: the shots tile the target duration (1/2/5 minutes)
    **exactly**; beat alignment is opportunistic (a sparse grid falls back to
    arithmetic boundaries and says so in `warnings`); auto mode weights shots by
    narrative role; the whole plan is a deterministic function of pinned
    evidence and is re-checked by the plan model.
  - `creative/slideshow/` adapter — the only place that touches the world:
    content-addressed probing (Pillow), WAV decoding and an energy-flux beat
    detector (numpy; **no `librosa`**, see the decision log), local image
    scoring, and an **opt-in** hosted analysis path (Gemini) that is fail-closed
    behind `NEXUS_SLIDESHOW_ALLOW_IMAGE_UPLOAD` and uploads downscaled copies
    only.
  - State extension (additive): `Project.assets`, `Clip.effects`,
    `Track.effects`, `AssetRecord` and `EffectLayerRef` (content-hashed,
    reversible), with `state_hash` extended to cover the asset registry.
  - CLI: `nexus slideshow templates`, `nexus slideshow plan` (auto/manual) and
    `nexus packs activate`.
  - Tests: 90 new unit tests including end-to-end planning over generated
    JPEG/WAV fixtures, plus `tests/architecture/test_slideshow_adapter_boundary.py`
    (manifest ↔ code coherence, heavy imports stay behind the adapter, the
    adapter never bypasses the command bus, templates are data).

### Changed
- `CapabilityRegistry` gains the Wave 2 slideshow operations through
  `build_slideshow_registry()`; `build_wave1_registry()` stays frozen (Wave 1
  catalog unchanged, still pinned by its architecture gate).
- **litellm no longer reaches the network on first use**: the provider seam sets
  `LITELLM_LOCAL_MODEL_COST_MAP=True` before importing litellm, so the pricing
  map comes from the bundled copy. This removes a hidden network fetch and keeps
  litellm's retry warnings off stdout, which the CLI's `--json` modes depend on.

## [3.10.0] — 2026-09-20

### Added
- **Nagar Phase 6, Wave 1 — the “Green Cockpit” core
  (`src/nexus_ai_agent/creative/studio/`)**: the first implementation of the
  accepted Nagar architecture (`docs/NAGAR_70_OPERATIONS_TDD.md`). Everything
  is a typed, UI-free command surface — JSON command in, in-memory state
  update out; no React/DOM/Canvas, no torch/transformers/CV, no `storage/`
  or `llm/` imports (enforced by `tests/architecture/test_nagar_studio_isolation.py`).
  - `models.py`: project/timeline/track/clip state, `MediaRef`, `Playhead`,
    markers, the `nagar.command.v1` typed envelope, `EditTransaction`/`CommandResult`,
    and the content-derived `state_hash` contract (`state_revision` excluded and
    monotonic, so revision+hash pairs stay valid across undo cycles).
  - `capabilities.py`: `Domain > Capability > OperationSpec` registry — the
    allow-list the bus consults, the A/B/C/D permission ladder, per-operation
    typed input models and pure `(project, context) -> outcome` handlers.
  - `references.py`: semantic reference resolution (`"اینجا"`, «۵ ثانیه قبل»,
    absolute/relative/start/end, Arabic-Indic digits) pinned **once at command
    receipt** with `captured_at_command=True`.
  - `bus.py`: the single write path — envelope validation → idempotency replay →
    registry lookup → permission gate → typed input validation → reference
    pinning → optimistic-concurrency preconditions → atomic apply. A failing
    handler leaves central state exactly as it was.
  - Exactly **five** operations in the Wave 1 catalog: `media.play` and
    `media.pause` (A), `timeline.mark` and `timeline.split_at_playhead` (B,
    non-destructive — source `MediaRef` untouched), `system.undo` (A, classic
    NLE undo that skips its own `system.undo` records).
  - Tests: `tests/unit/test_nagar_wave1_green_cockpit.py` (33 tests — every
    registered operation proves validate → apply on in-memory state → undo
    restores the previous state hash) plus the three isolation/architecture
    gates.
- **D1 — Job resume CLI (`nexus jobs resume`)**: operator entry point on the
  durable in-process queue. `InProcessJobQueue.resume_pending_jobs()` requeues
  rows still sitting in `pending` and drains them in the calling process with
  the standard application handlers; `processing` rows are never claimed, so
  the command is safe to run beside a live bot (which keeps its own
  process-startup `resume_pending()` recovery).
- **D3 — real PDF extraction via `pypdf`**: `worker.extract_pdf_text()` parses
  the PDF text layer in a worker thread (`asyncio.to_thread`) and feeds the
  RAG engine; PDF bytes are never silently decoded as UTF-8 text. Without
  `pypdf` the job fails durably with an actionable install hint
  (`pip install pypdf`). `pypdf` ships as the optional `[pdf]` extra (and in
  `[dev]` so the suite exercises the real parser); enqueue job type renamed
  `pdf` → `pdf_extract` through the existing `JobQueuePort` contract
  (signature unchanged).
- **D4 — Telegram completion notification**: `InProcessJobQueue` accepts an
  optional, strictly fail-safe `on_job_finished` hook that fires when a job
  reaches `completed` or `failed` (never on cancellation). The bot injects a
  notifier that messages the origin `chat_id` carried in the job payload;
  hook exceptions are logged and swallowed and can never corrupt durable job
  state.

### Security — dashboard API & egress hardening (fresh implementation; manual review required)
- **CORS lock-down (`api/app.py`)**: the previous default
  `allow_origins=["*"]` + `allow_credentials=True` reflected *any* origin
  with credentials, voiding the browser same-origin policy for the whole
  dashboard API. CORS is now an explicit allowlist
  (`NEXUS_API_CORS_ORIGINS`, comma-separated; **empty by default** — the
  served dashboard is same-origin and needs no CORS), with
  `allow_credentials` enabled only when origins are configured and methods
  limited to `GET/POST/OPTIONS`.
- **HMAC endpoint auth (`api/app.py`)**: `POST /creative/video-edit` is
  **fail-closed** — without `NEXUS_API_HMAC_KEY` (unset or empty) it answers
  `503 Security configuration incomplete`; with a key it accepts only
  `X-NEXUS-Timestamp` (±300 s freshness) +
  `X-NEXUS-Signature: hex(HMAC-SHA256(key, "{timestamp}:{raw_body}"))`,
  compared constant-time. (Fail-open legacy behaviour was hardened to
  fail-closed per the owner's pre-merge review, 2026-09-20.)
- **SSRF/DNS-rebinding guard on the shared egress client
  (`core/http_client.py`)**: `ResilientHttpClient` (free_tools,
  web_trainer — which fetches *user-supplied* URLs —, wikipedia_trainer)
  now runs `ssrf_guard.validate_url()` (https-only; *every* resolved
  address must be public) before each request and rides the
  connect-time re-validating `SafeAsyncTransport`, closing the
  rebinding/TOCTOU window even across redirects. Opt-out:
  `ResilientHttpClient(ssrf_protect=False)`.
- **Log redaction (`observability/logging.py`)**: `configure_logging`
  installs a redaction stage into structlog and the stdlib handlers —
  Telegram bot tokens, `Bearer`/`Authorization` values and
  key/value secrets (`api_key=`, `"token":`, …) are masked as
  `[REDACTED]` before they reach the logs. `redact_secrets()` is
  reusable; disable with `configure_logging(redact=False)`.
- **Telegram webhook**: unchanged (already constant-time, fail-closed);
  regression tests added.
- `.env.example` documents the two new variables
  (`NEXUS_API_CORS_ORIGINS`, `NEXUS_API_HMAC_KEY`).

### Changed
- **Release metadata is now consistent**: `VERSION` 3.9.0 → **3.10.0** and
  `pyproject.toml` 3.8.0 → **3.10.0**. The v3.9.0 release bumped `VERSION`
  only, so the packaged distribution had been reporting 3.8.0; both sources are
  now in lock-step and are covered by `tests/unit/test_version_command.py`.
- **`/version` no longer lies**: `bot/update_handlers.py` hardcoded `v3.0.0`
  in two places. Both now read the real running version from the installed
  distribution metadata (`importlib.metadata`), falling back to the repository
  `VERSION` file only for an uninstalled checkout — the file lives at the
  repository root and is deliberately not part of a wheel.

### Removed
- **D2 — dead code**: `worker.nightly_channel_management()` (no callers) is
  gone; with it the only `telegram` import leaked out of the bot layer.

## [3.9.0] — 2026-09-20

### Added
- **Cloudflare R2 — the "technical blob" tier + scheduled maintenance (Phase 5)**
  - `storage/providers/r2.py`: `R2Provider` on the existing
    `StorageProvider` protocol — boto3 against the S3-compatible endpoint
    (`https://{account_id}.r2.cloudflarestorage.com`, `region_name="auto"`,
    pinned `boto3==1.43.98`), client always built with
    `Config(request_checksum_calculation="when_required")` (known R2
    checksum-compatibility trap). Adds `generate_presigned_url(key,
    expires_in)` with the R2 7-day TTL cap (604800 s → beyond raises) and
    batched `delete_objects` for retention pruning. boto3 client is
    injectable — unit tests run fully offline.
  - `storage/ai_storage_manager.py`: a **separate routing branch** for
    technical blobs (`is_blob_key`, `BLOB_KEY_PREFIXES = ("backups/",
    "rag-docs/")`) — database backups and heavy RAG documents go to R2
    only (`upload_blob`/`download_blob`/`blob_presigned_url`). R2 is
    deliberately **not** added to the user-file round-robin
    (`unified_cloud.py` untouched; user files keep their existing path).
  - `maintenance/` + new CLI group `nexus maintenance`:
    `backup` (pg_dump / SQLite online-backup → R2, stateless, fails loudly
    without R2) and `housekeeping` (stale creative temp files + R2 backup
    retention prune, idempotent, stays green without R2); both with
    `--dry-run`.
  - `.github/workflows/maintenance.yml`: schedule-only (plus manual
    `workflow_dispatch`) — `backup-db` nightly 03:17 UTC, `housekeeping`
    weekly Mondays 04:23 UTC (deliberately off the top-of-hour); single
    shared `concurrency` group (`cancel-in-progress: false`); secrets:
    `NEXUS_DATABASE_URL` + the four `R2_*` keys — no bot token.
  - `docs/r2-storage.md`: bucket/token setup (single-bucket token scope),
    `.env` and GitHub secrets guide.

## [3.8.0] — 2026-09-19

### Added
- **Telegram webhook run-mode — real webhook for scale-to-zero deployments (Phase 3)**
  - `bot/webhook.py`: run-mode orchestration — `resolve_run_mode()`
    (CLI `--mode` > `NEXUS_RUN_MODE` > `polling`), `build_webhook_bind()`
    (port priority `PORT` > `DASHBOARD_PORT` > `8000`), and `run_webhook()`
    (PTB application init, `set_webhook` with a shared-secret token,
    uvicorn serving, graceful SIGTERM shutdown for scale-to-zero platforms)
  - `api/app.py`: two new endpoints — `POST /webhook/telegram`
    (constant-time secret check against `NEXUS_WEBHOOK_SECRET`; 403 on
    mismatch, 400 on missing `update_id`, verified payloads pushed to
    `application.update_queue`) and `GET /healthz` (process liveness, no
    database touch — the scale-to-zero health gate)
  - `bot/app.py`: `WebhookApplicationAdapter` — converts raw webhook JSON
    into a PTB `Update`. Lives in `bot/app.py` deliberately: the frozen
    import-boundary baseline only tolerates `telegram` imports in
    grandfathered files (`bot/webhook.py` and `api/app.py` stay
    telegram-free)
  - `cli.py run-bot`: the webhook branch now actually runs webhook mode
    (was "not yet configured" + exit 1); polling path untouched
  - New settings: `NEXUS_RUN_MODE`, `NEXUS_WEBHOOK_URL`,
    `NEXUS_WEBHOOK_SECRET` (+ `.env.example` entries)
  - `koyeb.yaml`: service type `worker` → `web`, health check path
    `/healthz` (was the non-existent `/api/health`)
  - `Dockerfile`: `CMD` honors `NEXUS_RUN_MODE` (default `polling` keeps
    existing always-on deployments working unchanged)
  - New dependency: `uvicorn==0.53.0` (verified latest on PyPI at pin time)
  - Tests: `tests/unit/test_webhook_mode.py` — mode/port priority,
    `/healthz`, enqueue of valid payloads, 403/400 rejection paths
  - Docs: `docs/deployment-koyeb.md` — step-by-step Koyeb `web` deploy and
    the explicitly accepted cold-start trade-off (Telegram retries; the
    multi-second wake-up delay is not a bug)

## [3.7.0] — 2026-09-19

### Added
- **Multi-provider LLM routing chain (litellm) — end of single-provider lock-in**
  - `llm/litellm_provider.py`: new `LiteLLMRoutingProvider(LLMProvider)` built
    on `litellm.Router` with a priority chain of free-tier providers:
    **Ollama (local, unlimited) → Groq → Gemini → OpenRouter `:free`**;
    only configured providers enter the chain
  - Anti retry-storm cooldowns: providers with daily caps
    (Groq/Gemini/OpenRouter) are parked for 86,400s after the *first* 429
    (`allowed_fails=1`); Ollama keeps a short 300s cooldown
  - `NEXUS_LLM_STRICT_PRIVACY=true` removes OpenRouter `:free` deployments
    (which may train on user prompts) from the chain
  - Existing `FallbackProvider` remains the outer layer: a fully drained
    router degrades to `FakeLLM` with the usual disclaimer instead of raising
  - `LLMProvider`, `FallbackProvider`, `GeminiProvider`, `FakeLLMProvider`,
    `LocalLlamaCppProvider` untouched; legacy local-GGUF path preserved when
    routing is disabled or unconfigured
  - New settings: `NEXUS_LLM_ROUTING_ENABLED`, `NEXUS_LLM_STRICT_PRIVACY`,
    `NEXUS_LLM_CLOUD_COOLDOWN`, `NEXUS_LLM_REQUEST_TIMEOUT`,
    `NEXUS_GROQ_API_KEY`/`GROQ_API_KEY`, `NEXUS_GROQ_MODEL`,
    `NEXUS_OLLAMA_BASE_URL`, `NEXUS_OLLAMA_MODEL`,
    `NEXUS_OPENROUTER_API_KEY`/`OPENROUTER_API_KEY`, `NEXUS_OPENROUTER_MODEL`
  - New dependency: `litellm>=1.74,<2` (verified against 1.101.0)
  - Docs: `docs/architecture/LLM_PROVIDERS.md` — chain, cooldowns, privacy
    flag, and the documented (deferred) shared-provider pattern of
    `agents/{gemma,phi,qwen}`

## [3.6.0] — 2026-09-19

### Added
- **Checkpoint lifecycle on the PostgreSQL path, end-to-end (PR3, option A)**
  - `nexus_checkpoint_lifecycle` table in PostgreSQL via the isolated
    Alembic revision `f4a9c2e71b08` (single-table DDL, PostgreSQL dialect
    only — the SQLite path is unchanged)
  - Read-only `PostgresCheckpointAdapter` (server-enforced read-only
    connection) behind the shared `CheckpointReadAdapter` contract with
    the SQLite adapter
  - Runtime PG path: `LifecycleRecordingSaver` over the real
    `PostgresCheckpointer` — lifecycle metadata lands in the same
    database, so serverless (Neon) restarts no longer lose it
  - Backend-aware `nexus checkpoints inspect|reconcile` (same CLI, PG or
    SQLite)
  - `nexus checkpoints golden update` — human-triggered schema golden
    management (write requires `--yes`; CHANGES warning with old/new
    digests)
  - Committed `postgres.langgraph.json` golden (schema-v1-pg)
  - Parameterized lifecycle store contract (SQLite + PG) and Neon
    operations runbook (`docs/ops/NEON_LIFECYCLE_RUNBOOK.md`)
  - Four-way kill-switch matrix (SQLite/PG × on/off) with live-PG
    composition tests
- Read-only checkpoint lifecycle operations: `inspect` contract
  (inspect-v1), reconciler with health gate (dry-run default), retention
  policy, schema goldens (SQLite + Postgres), O1 observability
  (redaction, structured events, core-schema fingerprint), lifecycle
  kill-switch and architecture boundary tests (PR1/PR2, this line of work)

### Changed
- Lifecycle metadata on the PostgreSQL path moved from a local SQLite
  sidecar (interim option B) to the database itself (option A, owner
  decision): serverless local files are ephemeral, so the sidecar would
  lose all lifecycle history on compute restart
- `nexus adopt-pg` now treats a database stamped at head but missing
  `nexus_checkpoint_lifecycle` as drift (fail-fast — never implicit
  repair)
- Migration chain head: `47903d282ede` → `f4a9c2e71b08`;
  `postgres.langgraph.json` fingerprint regenerated via the
  human-triggered CLI (head-only change)
- Reconciler purge (lifecycle rows only, under the full guard stack)
  works uniformly on both backends

### Fixed
- `PostgresCheckpointer` subclassed itself
  (`class PostgresCheckpointer(PostgresCheckpointer)`) and failed
  `ensure_valid_checkpointer`'s `BaseCheckpointSaver` isinstance check on
  every PG boot — it is now a genuine `BaseCheckpointSaver` subclass
  delegating to the official checkpointer
- Graph smoke tests are hermetic on persistent stores (unique thread ids
  per run)
- CI `migrate-postgres` job installs the `[dev]` extra so its pytest
  steps run

## [3.5.0] — 2026-09-17

> Released as **v3.5.0**.  An earlier tag `v0.2.0-D` was cut on the same work
> while this entry was still numbered `0.2.0-D`; the project line is
> 3.x (`VERSION` and `pyproject.toml` were 3.4.1, and tags run v1.2.0 → v3.3.0),
> so a 0.2.0 tag sorted three major versions backwards.  The tag is left in
> place — tags are never rewritten — and v3.5.0 supersedes it.

### Added
- Alembic-based schema management end-to-end (D1–D5)
- `nexus migrate` CLI command (Alembic-first, legacy `create_all` fallback)
- `nexus adopt-pg` CLI command for legacy PostgreSQL (D10), dry-run by default
- PostgreSQL/Neon support via `NEXUS_DATABASE_URL` (C1)
- Token encryption at rest — Fernet (`security/crypto.py`, D8)
- pgvector integration — Alembic revision `2a1c4b6d8e9f` (D7)
- Postgres adoption / fail-fast seam (`storage/adopt_pg.py`, D10)
- Continuum snapshot (`nexus continuum show|verify`, `.nexus/continuum.json`) for
  turn-to-turn state recovery
- Cross-host safety for concurrent schema creation: `create_all_metadata`
  retries a create-conflict, complementing `migration_lock()` (which is a local
  `fcntl.flock` file and therefore cannot see a second host)

### Changed
- PostgreSQL `create_all` stopgap retired (D7); Alembic is the single source
  of schema truth for PostgreSQL
- Startup schema management is Alembic-first on both backends
- `nexus migrate --db-path` marked deprecated (removal 2026-10-01)

### Fixed
- Legacy SQLite databases are auto-adopted (+ stamped) before upgrading (D6),
  preserving user data and avoiding the initial-revision `CREATE TABLE` clash
- Un-stamped PostgreSQL with schema drift now fails fast with an actionable
  message instead of a raw DBAPI error (D10)
- **`nexus migrate` now consults the D10 decision matrix too.** `prepare_postgres`
  guarded the `get_session` path, but `run_migrations` went straight to
  `command.upgrade`, so an un-stamped Postgres still died there with a bare
  `asyncpg.DuplicateTableError: relation "adcampaign" already exists` that never
  mentioned `nexus adopt-pg`. Reproduced against a real PostgreSQL server; a
  zero-drift database is now adopted (stamped at head) and drift is refused with
  the actionable message, before Alembic writes anything.
- Concurrent `create_all` converges instead of colliding. `MetaData.create_all`
  runs `checkfirst=True`, so two processes both see "does not exist", both emit
  the DDL, and the loser died on `table … already exists` — reproduced with four
  concurrent `nexus migrate` subprocesses where all four failed.
- `VERSION` and `pyproject.toml` moved 3.4.1 → 3.5.0 to match this release.

### Known limitations
- Real Neon connectivity test (D9) pending a user-supplied `NEXUS_DATABASE_URL`;
  CI already verifies the full Alembic chain on a real Postgres + pgvector

## [3.4.1] — 2026-09-17

### Security — Phase 0
- **SSRF protection** for URL summarization (`core/ssrf_guard.py`):
  https-only fetching, pre-fetch DNS validation of every resolved
  address, and connect-time re-validation on each connection including
  redirects (closes the DNS-rebinding TOCTOU window by connecting to the
  validated IP literal); 127/8, 10/8, 172.16/12, 192.168/16,
  169.254/16 (cloud metadata), 0/8, ::1, fe80::/10, fc00::/7 blocked
- **Shell tool sandboxed to the workspace** (`tools/system_shell.py`):
  commands run with `cwd=workspace_root`; path arguments of ls/cat/
  grep/find are containment-checked (absolute paths, `..`, symlink
  escapes rejected); `find -exec/-execdir/-delete/-ok/-okdir` blocked;
  file-writing find flags (`-fprintf`, `-fprint`, `-fprint0`, `-fls`,
  `-newer`) restricted to the workspace
- **AuthMiddleware deny-by-default** (`bot/middleware.py`): only the
  owner and explicitly allowed users get in; an empty configuration no
  longer allows everyone (previously fail-open)
- **Self-update gated**: `/update` is owner-only and requires an
  explicit owner approval (`PendingApproval`, 30-minute window); fixed
  the broken `pip install -r requirements.txt` (file never existed) to
  `pip install .` (matches the Dockerfile)
- New `settings.auto_update` (default **off**): the self-update
  check/apply path (now in `agent/self_monitor.py`) only runs when the
  owner explicitly enables it via `AUTO_UPDATE=true`

### Bug fixes (surfaced by mypy strict)
- `/quiz`, `/code`, `/translate`, `/viral_now` called methods that do not
  exist (AttributeError/TypeError at runtime) — repaired and regression
  tested
- `bot/handlers.py`: `_base_state` now builds a valid `NexusState` for
  the LangGraph invocation; `on_message` uses the compiled graph
  instance instead of a non-existent module attribute
- `api/dashboard.py`: async session API (`execute/scalar_one`) and
  counts on columns that actually exist
- `None`-guards for `update.message` / `effective_user` /
  `callback_query.from_user` across bot handler modules
- Missing dependencies declared: `duckduckgo-search`, `fastapi`;
  `httpx` pinned (dependency on a private API in `core/ssrf_guard.py`)

### Maintenance
- Quality gates green: `ruff check` + `ruff format` clean,
  `mypy strict` clean (46 pre-existing errors fixed), 96 passing tests
- `build/` artifacts untracked and added to `.gitignore`
- `VERSION` and `pyproject.toml` aligned to 3.4.1

## [3.4.0] — 2026-09-17

### Added — catch-up entry for the work shipped since 2.0.0

The changelog was last updated at 2.0.0; this entry documents the
features that were implemented afterwards without being recorded.

**Multi-agent framework (`agents/`)**
- Persona agents `gemma_agent.py`, `phi_agent.py`, `qwen_agent.py` with
  distinct system prompts (note: they share one LLM provider instance —
  the difference is personality, not model)
- Core roles: `chat_agent.py`, `planner_agent.py`, `executor_agent.py`
- `agents/store/` — user-selectable specialized agents with per-user
  activation state (`/agents`, `/myagent`, `/agent_stop`)

**LangGraph orchestration (`orchestration/`)**
- `graph.py` — intent router → memory reader → persona selection →
  planner/executor → memory writer, compiled against a SQLite
  checkpointer (`storage/langgraph_checkpoint.py`)
- `router.py` — intent classification and persona routing
- `state.py` — shared `NexusState` graph state

**Tool system (`tools/`) + approvals (`agent/`)**
- `ToolRegistry` with risk levels (safe/guarded/blocked) and policy
  confirmation
- Sandboxed file tools (read/write/list) and an allowlisted shell tool
  (disabled by default; enable with `NEXUS_ENABLE_SHELL`)
- `agent/approval.py` — persistent `PendingApproval` workflow with owner
  notifications and `/approve` / `/reject` handlers

**Self-monitoring (`agent/self_monitor.py`, `bot/monitor_handlers.py`)**
- RAM/disk/uptime health checks with owner alerts (`/health`)
- Instrumented observability hooks across the pipeline

**Memory & knowledge**
- `memory/short_term.py`, `memory/long_term.py` (SQLite + sqlite-vec)
- `knowledge/` — web and Wikipedia trainers with a cached knowledge
  store (`/learn`, `/search`, `/wiki`)

**API & background workers**
- FastAPI dashboard (`api/`) with stats endpoints
- Celery worker (`worker.py`) for story rendering, PDF processing and
  nightly channel maintenance

**LLM layer (`llm/`)**
- Provider abstraction with Gemini, local llama.cpp, fallback and
  fake (test) providers
- Resilient HTTP client with retry and circuit breaker
  (`core/http_client.py`)

## [2.0.0] — 2025-06-12

### Added — Global Expansion Release 🌍🚀

The most ambitious update in NEXUS AI history, transforming the bot from a
community tool into a **globally accessible AI platform** with free cloud
storage, viral referral growth, 15-language support, and powerful AI features
— all powered by 100% free APIs and services.

**Phase 1 — Google Gemini AI Integration**
- `features/ai_chat.py` — GeminiEngine class wrapping Google Gemini 2.0 Flash API
- Rate limiting: 15 RPM, 1M TPM, 1500 requests/day (free tier)
- Conversation memory with per-user session tracking (up to 20 messages)
- `/ai <text>` — conversational AI chat with context memory
- `/ask <question>` — single-turn factual question answering
- `/vision` — image analysis via Gemini Vision (reply to photo)
- `/code <prompt>` — AI code generation with syntax highlighting
- `/translate <text>` — AI-powered translation with auto-detect
- `/summarize <text|URL>` — smart summarization with 5 modes (brief, detailed, key_points, eli5, academic)
- `summarizer_engine` with URL scraping support and structured SummaryResult output

**Phase 2 — Unified Cloud Storage (57GB+ Free)**
- `storage/unified_cloud.py` — UnifiedCloudStorage orchestrator
- 5+ free cloud providers: Dropbox (2GB), pCloud (10GB), Internxt (10GB), MEGA (20GB), GitHub Releases (unlimited)
- Round-robin upload distribution with capacity-aware routing
- Automatic failover between providers
- `/cloud` — upload file to unified cloud (reply to document)
- `/myfiles` — list all your cloud files
- `/download <filename>` — download file from cloud
- `/cloud_status` — view storage status across all providers
- `CloudFile` SQLModel table tracking uploads per user

**Phase 3 — Referral Viral Loop System**
- `features/referral.py` — ReferralEngine with 6 exponential growth tiers
- Auto-generated unique referral codes per user (NEXUS-{uid}-{hash})
- Tiered rewards: 🥉 Inviter (1) → 🥈 Networker (3) → 🥇 Star (5) → 💎 Diamond (10) → 👑 Legendary (25) → 🚀 Viral Master (50)
- Dual-reward system: both referrer and referee get prizes
- `/referral` — view your referral code, link, and current tier progress
- `/referral_board` — global leaderboard of top referrers
- `Referral` and `ReferralCode` SQLModel tables with reward tracking

**Phase 4 — i18n Multi-Language System (15 Languages)**
- `i18n/__init__.py` — I18n manager with 15 supported languages
- `i18n/loader.py` — Language loader with JSON file support
- Languages: English, Persian, Arabic, Spanish, French, German, Russian, Chinese, Japanese, Korean, Portuguese, Hindi, Turkish, Indonesian, Italian
- Per-user language preference persistence via `UserLanguage` SQLModel
- `/language` — interactive inline keyboard for language selection
- `lang_{code}` callback handlers for instant language switching

**Phase 5 — Free Image Generation via Pollinations.ai**
- `features/image_gen.py` — ImageGenEngine with 10 style presets
- Styles: realistic, anime, digital, oil, watercolor, pixel, 3d, comic, minimal, fantasy
- 5 size options: 1024×1024, 1792×1024, 1024×1792, 512×512, 1280×720
- `/image <description>` — AI image generation (e.g., `/image style:anime a cat samurai`)
- Zero API key required — Pollinations.ai is completely free

**Phase 6 — Speech-to-Text & Text-to-Speech**
- `features/speech.py` — SpeechEngine with gTTS + Gemini STT
- `/tts <text>` — convert text to voice message (100+ languages)
- `/stt` — transcribe voice/audio messages to text (reply to voice)
- gTTS for TTS (free, no API key), Gemini for STT (high accuracy)
- Automatic MIME type detection and temp file handling

**Phase 7 — Smart Summarizer**
- `features/summarizer.py` — SummarizerEngine with Gemini backend
- 5 summarization modes: brief, detailed, key_points, eli5, academic
- URL summarization with automatic content scraping
- Structured SummaryResult output with metadata
- `/summarize mode:detailed <text|URL>` — flexible summarization

**Phase 7.5 — Handlers Integration & Menu Redesign**
- All 17 new CommandHandlers registered in build_handlers()
- 7 new CallbackQueryHandlers for interactive menus
- Redesigned main menu with 6 sections: 🤖 AI, 🎨 Image, 🎤 Speech, ☁️ Cloud, 🔗 Referral, 🌐 Language
- Interactive inline keyboard navigation between menu sections
- `/start ref_<code>` deep-link support for referral tracking
- Updated `/help` command with complete v2.0.0 command documentation

### Changed
- Extended `_reply()` helper to accept `reply_markup` kwarg for inline keyboards
- `Settings` model updated with new fields: `gemini_api_key`, `gemini_model`, `gemini_max_rpm`, `gemini_max_daily`, `dropbox_token`, `pcloud_token`, `internxt_token`, `bot_username`
- `vision_cmd` now uses Gemini Vision API with `bytes` input instead of base64
- `stt_cmd` uses temp file approach for Gemini STT compatibility
- `cloud_cmd` uses file-path based upload via `unified_cloud.upload_file(local_path, remote_key)`
- `download_cmd` supports both in-memory and file-based download paths
- Referral methods are synchronous (not async) — removed incorrect `await` calls

### Fixed
- Resolved 22 syntax errors from incomplete line-based replacements in handlers.py
- Fixed missing `except` block in `vision_cmd` after API signature migration
- Fixed orphaned `user_id=user_id,` lines in `cloud_cmd` from old `upload_file` call
- Fixed dead code after `return` in `download_cmd`
- Fixed SQLAlchemy `Table already defined` errors with `extend_existing=True`
- Fixed SQLAlchemy index conflict in `ReferralEngine._ensure_tables()` with raw SQL
- All ruff linting, formatting, and mypy type checks passing
- All 23 unit tests passing

## [1.3.0] — 2025-06-12

### Added — AI Community Operating System (Phases 7–16)

**Phase 7 — Owner Control System**
- `is_owner()` check and `owner_only` decorator for admin-only access
- `/owner` dashboard, `/system` status, `/broadcast` and `/broadcast_all` commands
- `/admin_logs` for recent admin action log review
- `AdminLog` SQLModel table with sync engine CRUD

**Phase 8 — Force Join System**
- Channel membership verification before bot usage
- `ForceJoinManager` with 5-minute cached membership checks
- Anti-bypass: cache invalidation on verify, re-check on expiry
- `/forcejoin_on`, `/forcejoin_off`, `/forcejoin_status`, `/forcejoin_message` commands
- Inline verify button for non-member users
- `ForceJoinConfig` SQLModel table

**Phase 9 — AI Personality Engine**
- 10 distinct AI personalities with Persian greetings, tone, and style
- Per-group personality configuration with persistence
- `/personality list|current|set <name>` command interface
- `PersonalityConfig` SQLModel table

**Phase 10 — AI Community Engagement**
- Auto-engagement engine with ice breakers, jokes, challenges, daily questions, events
- Rate-limited content generation (minimum 60-minute intervals)
- Rich Persian content banks for each engagement type
- `/engagement_on`, `/engagement_off`, `/challenge`, `/joke`, `/event` commands
- `EngagementConfig` SQLModel table

**Phase 11 — Viral Content Engine**
- `ViralEngine` with auto viral post generation and scoring heuristics
- Viral score algorithm: length, hashtags, emojis, questions, call-to-action
- Auto-hashtag generation per category
- Content hash-based duplicate prevention
- Post scheduling with pending/posted lifecycle
- `/viral_now`, `/viral_preview`, `/viral_stats`, `/viral_post` commands
- `ViralPost` SQLModel table

**Phase 12 — Advertisement System**
- `AdManager` with full campaign CRUD lifecycle
- Scheduled ads with configurable repeat intervals and max repeats
- Auto-next-run scheduling and completion detection
- Campaign pause, resume, and delete controls
- `/ad_create`, `/ad_list`, `/ad_pause`, `/ad_resume`, `/ad_delete`, `/ad_stats` commands
- `AdCampaign` SQLModel table

**Phase 13 — Smart Moderation**
- `ModerationEngine` with multi-layer content analysis
- Anti-spam (repeated chars, uppercase abuse, emoji spam)
- Anti-flood (5 messages per 5 seconds rate limit)
- Link filter (URL and t.me link detection)
- Persian profanity regex filter
- Warning system with configurable max warnings and auto-mute
- User reputation tracking with adjustment API
- `/mod_on`, `/mod_off`, `/mod_config`, `/warn`, `/mute`, `/unmute`, `/reputation` commands
- `ModerationConfig` and `UserReputation` SQLModel tables

**Phase 14 — Gamification System**
- `GamificationEngine` with XP, leveling, streaks, daily rewards, achievements
- 16 levels with Persian titles (تازه‌وارد → افسانه‌ای)
- Cumulative XP thresholds for level progression
- Daily streak tracking with 1-day/2-day grace logic
- 8 achievements with JSON array persistence in SQLite
- `/profile`, `/daily`, `/xp_leaderboard`, `/achievements` commands
- `UserXP` SQLModel table

**Phase 15 — Analytics Engine**
- `AnalyticsEngine` with event tracking and multi-dimensional queries
- Active user counts (24h/7d), engagement rate, events per user
- Peak hours analysis by hour of day
- Day-by-day cohort retention tracking
- Command usage statistics
- Combined dashboard summary
- `/analytics`, `/analytics_active`, `/analytics_retention`, `/track` commands
- `AnalyticsEvent` SQLModel table

**Phase 16 — Advanced UI**
- Redesigned main menu with 6-row inline keyboard
- Personality submenu: list, current, set
- Gamification submenu: profile, daily, leaderboard, achievements
- Analytics submenu: dashboard, active users, retention, command usage
- Moderation submenu: on/off, config, reputation
- Admin dashboard panel with nested submenus for: owner controls, viral, ads, moderation, analytics, force join, engagement, system status
- All submenus include back navigation
- Updated help text to v1.3.0

### Changed
- Help command updated to v1.3.0 with all new feature sections
- Settings help panel updated with personality, gamification, and moderation sections
- Main menu back button shows expanded 6-row keyboard

### Technical
- All new features use sync SQLAlchemy engine (`_sync_engine()` pattern) for CRUD
- `col()` from sqlmodel used consistently for type-safe ORDER BY and WHERE clauses
- All Persian content strings include `# noqa: E501` where line length limits prevent breaking
- Ruff + mypy + pytest all green across all phases
- 10 new SQLModel tables added to models.py
- 25+ new command handlers registered
- 12+ new callback query handler patterns registered

## [1.2.0] — 2025-05-29

### Added
- Phase 1: Channel & Group Management (post, schedule, ban, unban, welcome, pin, stats)
- Phase 2: Anonymous Chat (queue-based random pairing, report system)
- Phase 3: Games & Entertainment (quiz, number guess, Persian Wordle, polls)
- Phase 4: Utility Tools (reminders, translation, unit conversion, calculator)
- Phase 5: Inline Keyboard Menu System
- Phase 6: AI Chat Integration (LangGraph routing, persona system, memory)

## [1.1.0] — 2025-05-20

### Added
- Initial Telegram bot with python-telegram-bot v21+
- SQLModel async SQLite database
- User authentication and rate limiting
- LLM provider abstraction (llama.cpp + FakeLLM)
- LangGraph orchestration graph

## [1.0.0] — 2025-05-15

### Added
- Project scaffolding and core architecture
- Configuration management with pydantic-settings
- Observability with structlog
