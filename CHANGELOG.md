# Changelog

All notable changes to NEXUS AI Agent will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [3.5.0] — 2026-09-17

Phase D — Alembic-based schema management and PostgreSQL as a first-class
backend. See `docs/PHASE_D_ARCHITECTURE.md` for the design and
`docs/MIGRATION_GUIDE.md` for operator instructions.

### Added
- **Alembic migration chain** (D1–D5): `migrations/` with an initial revision
  covering all 30 tables (`47903d282ede`) and a pgvector revision
  (`2a1c4b6d8e9f`, current head), driven by `migrations/env.py` in async mode
- **`nexus migrate`** CLI command — backend-agnostic, idempotent
- **PostgreSQL / Neon support** via `NEXUS_DATABASE_URL` (C1), with
  `DATABASE_URL` accepted as a legacy alias; asyncpg runtime plus a Postgres
  LangGraph checkpointer
- **pgvector** enabled on PostgreSQL (`CREATE EXTENSION IF NOT EXISTS vector`)
- **Legacy SQLite adoption** (D6): a pre-Alembic file is repaired with an
  idempotent `create_all` and stamped at head instead of being overwritten
- **`nexus adopt-pg`** (D10) with `--dry-run` and `--yes`: adopts a PostgreSQL
  database that already holds NEXUS tables but has no `alembic_version`
- **Fail-fast on un-stamped PostgreSQL** (D10): `nexus migrate` and bot startup
  refuse before Alembic writes anything, naming the exact command to run
- **Token encryption at rest** (D8): Fernet, master key from `NEXUS_SECRET_KEY`
  only — a database dump alone cannot decrypt stored tokens
- **`nexus continuum show` / `verify`** and a committed
  `.nexus/continuum.json`, so cross-turn project state travels with the code
- **CI job `migrate-postgres`** (D9 groundwork): proves the whole chain against
  a real `pgvector/pgvector:pg16` service container, zero cloud credentials
- `scripts/bootstrap_env.py` — deterministic dev environment, pins read from
  `pyproject.toml` rather than duplicated
- `nexus` console entry point (`[project.scripts]`)

### Changed
- The PostgreSQL `create_all` stopgap is retired (D7): Alembic is now the single
  source of schema truth for both backends
- Startup is Alembic-first; `create_all` survives only as the adoption repair
  step, because it is idempotent and therefore cannot lose data
- `nexus migrate`'s help no longer advertises a PostgreSQL `create_all`
  fallback that no longer exists

### Fixed
- **Concurrent bootstrap no longer fails.** `MetaData.create_all(checkfirst=True)`
  has a TOCTOU race: two processes both see "does not exist", both emit
  `CREATE TABLE`, and the loser died on `table … already exists`. Conflicts are
  now retried, so racers converge; any other error still propagates immediately
- Legacy SQLite databases were at risk of the initial revision being replayed
  over existing tables; they are adopted first (D6)
- Database credentials are redacted from every migration and adoption message

### Security
- Credentials never appear in error output (`redact_url`), so a failed
  migration cannot leak `NEXUS_DATABASE_URL` into logs or CI

### Known limitations
- **D9 is CI-only**: the chain is proven on a local Postgres container; the same
  path against real Neon is pending `NEXUS_DATABASE_URL` from the project owner
- pgvector is enabled but no `vector` column exists yet
- `nexus migrate --db-path` still bootstraps via `create_all`; prefer
  `NEXUS_DB_PATH`

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
