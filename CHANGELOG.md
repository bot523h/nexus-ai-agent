# Changelog

All notable changes to NEXUS AI Agent will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
