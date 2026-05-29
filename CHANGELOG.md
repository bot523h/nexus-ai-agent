# Changelog

All notable changes to NEXUS AI Agent will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
